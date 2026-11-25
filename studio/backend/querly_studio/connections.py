"""Connection profiles, adapter discovery, and live connection management.

Only adapters whose execution path is verified in this release may be used to
run queries.  The rest remain discoverable so the UI can report honestly why
they are disabled instead of hiding them.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oq.runtime.contract import (
    Adapter,
    AdapterError,
    AdapterErrorCode,
    Connection,
    ConnectionConfig,
    EngineVersion,
    SecretProvider,
    redact_message,
)
from oq.runtime.plugin import AdapterPlugin, PluginError, discover_plugins

from .settings import connections_file

logger = logging.getLogger("querly_studio.connections")

VERIFIED_EXECUTION_ADAPTERS = frozenset({"sqlite"})
"""Adapters with a verified, non-placeholder execution path in this release."""

KEYRING_SERVICE = "Querly Studio"


@dataclass(slots=True)
class ConnectionProfile:
    id: str
    name: str
    adapter: str = "sqlite"
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    path: str | None = None
    sslmode: str | None = None
    options: dict[str, str] = field(default_factory=dict)
    read_only: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("created_at", None)
        data["createdAt"] = self.created_at
        data["updatedAt"] = self.updated_at
        data["hasSecret"] = _has_secret(self.id)
        return data

    def to_config(self) -> ConnectionConfig:
        return ConnectionConfig(
            adapter=self.adapter,
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            path=self.path,
            sslmode=self.sslmode,
            options=dict(self.options),
        )


class ProfileStore:
    """Atomic JSON persistence for non-secret connection fields."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or connections_file()
        self._lock = threading.RLock()
        self._profiles: dict[str, ConnectionProfile] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._profiles = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._profiles = {
                    item["id"]: ConnectionProfile(**item) for item in raw.get("connections", [])
                }
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("could not read connection store: %s", exc)
                self._profiles = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "connections": [asdict(profile) for profile in self._profiles.values()]}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def list(self) -> list[ConnectionProfile]:
        with self._lock:
            return list(self._profiles.values())

    def get(self, profile_id: str) -> ConnectionProfile:
        with self._lock:
            profile = self._profiles.get(profile_id)
            if profile is None:
                raise KeyError(profile_id)
            return profile

    def create(self, profile: ConnectionProfile) -> ConnectionProfile:
        with self._lock:
            self._profiles[profile.id] = profile
            self._save()
            return profile

    def update(self, profile: ConnectionProfile) -> ConnectionProfile:
        with self._lock:
            if profile.id not in self._profiles:
                raise KeyError(profile.id)
            profile.updated_at = datetime.now(UTC).isoformat()
            self._profiles[profile.id] = profile
            self._save()
            return profile

    def delete(self, profile_id: str) -> None:
        with self._lock:
            self._profiles.pop(profile_id, None)
            self._save()
            _delete_secret(profile_id)


def _keyring():
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring
    except Exception:  # pragma: no cover - optional dependency
        return None


def _has_secret(profile_id: str) -> bool:
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        return keyring.get_password(KEYRING_SERVICE, profile_id) is not None
    except Exception:
        return False


def set_secret(profile_id: str, secret: str | None) -> bool:
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        if secret:
            keyring.set_password(KEYRING_SERVICE, profile_id, secret)
        else:
            _delete_secret(profile_id)
        return True
    except Exception as exc:  # pragma: no cover - platform credential store
        logger.warning("could not store secret in the OS credential store: %s", exc)
        return False


def _delete_secret(profile_id: str) -> None:
    keyring = _keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, profile_id)
    except Exception:
        pass


class _ProfileSecretProvider(SecretProvider):
    def __init__(self, profile_id: str) -> None:
        super().__init__()
        self.profile_id = profile_id

    def get(self, key: str) -> str | None:
        keyring = _keyring()
        if keyring is None:
            return None
        try:
            return keyring.get_password(KEYRING_SERVICE, self.profile_id)
        except Exception:
            return None


def _support_to_dict(support: Any) -> dict[str, Any]:
    level = getattr(support, "support", None)
    return {
        "support": getattr(level, "value", level),
        "native": getattr(support, "native", None),
        "notes": getattr(support, "notes", None),
        "maxPrecision": getattr(support, "max_precision", None),
        "maxLength": getattr(support, "max_length", None),
        "dimension": getattr(support, "dimension", None),
        "range": getattr(support, "range", None),
    }


def capability_report(adapter: Adapter) -> dict[str, Any]:
    report = adapter.capabilities()
    return {
        "adapter": report.adapter,
        "engine": report.engine,
        "adapterVersion": report.adapter_version,
        "testedVersions": list(report.tested_versions),
        "limitations": list(report.limitations),
        "types": {name: _support_to_dict(support) for name, support in report.types.items()},
        "capabilities": {
            name: {"supported": item.supported, "notes": item.notes}
            for name, item in report.capabilities.items()
        },
        "generatedAt": report.generated_at.isoformat(),
    }


class AdapterRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plugins: dict[str, AdapterPlugin] = {}
        self._error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        with self._lock:
            try:
                self._plugins = discover_plugins()
                self._error = None
            except PluginError as exc:
                self._plugins = {}
                self._error = redact_message(str(exc))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for plugin_id, plugin in sorted(self._plugins.items()):
                adapter = plugin.adapter
                try:
                    report = capability_report(adapter)
                    error = None
                except Exception as exc:
                    report = None
                    error = redact_message(str(exc))
                verified = plugin_id in VERIFIED_EXECUTION_ADAPTERS
                result.append(
                    {
                        "id": plugin_id,
                        "displayName": getattr(adapter, "display_name", plugin.display_name),
                        "packageVersion": getattr(adapter, "package_version", ""),
                        "dataModel": plugin.data_model,
                        "distribution": plugin.distribution,
                        "irSubset": list(plugin.ir_subset),
                        "documentedLimitations": list(plugin.documented_limitations),
                        "executionVerified": verified,
                        "executionNote": (
                            None
                            if verified
                            else "Adapter is installed, but its execution path is not verified in this release."
                        ),
                        "capabilities": report,
                        "loadError": error,
                    }
                )
            return result

    def get(self, adapter_id: str) -> Adapter:
        with self._lock:
            plugin = self._plugins.get(adapter_id)
            if plugin is None:
                hint = f" ({self._error})" if self._error else ""
                raise AdapterError(
                    AdapterErrorCode.UNSUPPORTED,
                    f"adapter {adapter_id!r} is not installed{hint}",
                )
            return plugin.adapter

    def require_verified(self, adapter_id: str) -> Adapter:
        if adapter_id not in VERIFIED_EXECUTION_ADAPTERS:
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                f"adapter {adapter_id!r} is installed but not verified for execution in this release",
            )
        return self.get(adapter_id)


@dataclass(slots=True)
class LiveConnection:
    profile_id: str
    profile_name: str
    adapter_id: str
    adapter: Adapter
    connection: Connection
    version: EngineVersion | None
    read_only: bool
    opened_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_used: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def public(self) -> dict[str, Any]:
        return {
            "profileId": self.profile_id,
            "profileName": self.profile_name,
            "adapter": self.adapter_id,
            "version": str(self.version) if self.version else None,
            "readOnly": self.read_only,
            "openedAt": self.opened_at,
            "lastUsed": self.last_used,
        }


class ConnectionService:
    def __init__(
        self,
        store: ProfileStore | None = None,
        registry: AdapterRegistry | None = None,
    ) -> None:
        self.store = store or ProfileStore()
        self.registry = registry or AdapterRegistry()
        self._live: dict[str, LiveConnection] = {}
        self._lock = threading.RLock()

    # -- profiles -----------------------------------------------------

    def create_profile(self, payload: dict[str, Any]) -> ConnectionProfile:
        adapter_id = str(payload.get("adapter") or "sqlite").lower()
        self.registry.require_verified(adapter_id)
        profile_id = uuid.uuid4().hex[:12]
        path = payload.get("path")
        if adapter_id == "sqlite":
            if not path:
                raise ValueError("SQLite connections require a database file path")
            path_obj = Path(str(path)).expanduser()
            if not path_obj.is_absolute():
                raise ValueError("SQLite database path must be absolute")
            if not path_obj.exists() and not payload.get("createIfMissing"):
                raise ValueError(
                    "SQLite database file does not exist; enable 'create if missing' to create it"
                )
            if payload.get("createIfMissing") and not path_obj.exists():
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                path_obj.touch()
            path = str(path_obj.resolve())
        profile = ConnectionProfile(
            id=profile_id,
            name=str(payload.get("name") or f"{adapter_id} connection"),
            adapter=adapter_id,
            host=payload.get("host"),
            port=int(payload["port"]) if payload.get("port") else None,
            database=payload.get("database"),
            user=payload.get("user"),
            path=path,
            sslmode=payload.get("sslmode"),
            options=dict(payload.get("options") or {}),
            read_only=bool(payload.get("readOnly")),
        )
        secret = payload.get("password")
        if secret and not set_secret(profile.id, str(secret)):
            raise ValueError(
                "the OS credential store is unavailable; the profile was not created because the secret could not be stored"
            )
        return self.store.create(profile)

    def update_profile(self, profile_id: str, payload: dict[str, Any]) -> ConnectionProfile:
        profile = self.store.get(profile_id)
        if "name" in payload and payload["name"]:
            profile.name = str(payload["name"])
        if "path" in payload and payload["path"]:
            profile.path = str(Path(str(payload["path"])).expanduser().resolve())
        if "readOnly" in payload:
            profile.read_only = bool(payload["readOnly"])
            if profile.id in self._live and self._live[profile.id].read_only != profile.read_only:
                self.disconnect(profile_id)
        if "password" in payload:
            secret = payload.get("password")
            if secret:
                if not set_secret(profile.id, str(secret)):
                    raise ValueError("the OS credential store is unavailable")
            else:
                set_secret(profile.id, None)
        return self.store.update(profile)

    def delete_profile(self, profile_id: str) -> None:
        self.disconnect(profile_id)
        self.store.delete(profile_id)

    # -- live connections --------------------------------------------

    def connect(self, profile_id: str) -> LiveConnection:
        with self._lock:
            existing = self._live.get(profile_id)
            if existing is not None and not existing.connection.is_closed:
                existing.last_used = datetime.now(UTC).isoformat()
                return existing
            profile = self.store.get(profile_id)
            adapter = self.registry.require_verified(profile.adapter)
            connection = adapter.connect(
                profile.to_config(),
                secrets=_ProfileSecretProvider(profile.id),
            )
            try:
                version = adapter.detect_version(connection)
            except Exception:
                version = None
            live = LiveConnection(
                profile_id=profile.id,
                profile_name=profile.name,
                adapter_id=profile.adapter,
                adapter=adapter,
                connection=connection,
                version=version,
                read_only=profile.read_only,
            )
            self._live[profile_id] = live
            return live

    def disconnect(self, profile_id: str) -> None:
        with self._lock:
            live = self._live.pop(profile_id, None)
            if live is not None:
                try:
                    live.adapter.close(live.connection)
                except Exception:
                    pass

    def get_live(self, profile_id: str) -> LiveConnection:
        live = self._live.get(profile_id)
        if live is None or live.connection.is_closed:
            live = self.connect(profile_id)
        live.last_used = datetime.now(UTC).isoformat()
        return live

    def list_live(self) -> list[dict[str, Any]]:
        with self._lock:
            return [live.public() for live in self._live.values()]

    def test(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        adapter = self.registry.require_verified(profile.adapter)
        connection = adapter.connect(
            profile.to_config(),
            secrets=_ProfileSecretProvider(profile.id),
        )
        try:
            version = adapter.detect_version(connection)
            alive = connection.ping()
            return {
                "ok": alive,
                "engine": str(version) if version else None,
                "version": version.version if version else None,
            }
        finally:
            try:
                adapter.close(connection)
            except Exception:
                pass

    def close_all(self) -> None:
        with self._lock:
            for profile_id in list(self._live):
                self.disconnect(profile_id)

    def capabilities_for(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        adapter = self.registry.get(profile.adapter)
        return capability_report(adapter)
