"""Public adapter plugin interface for Querly.

Third-party adapters are ordinary Python distributions that publish an entry
point in the ``oq.adapters`` group::

    [project.entry-points."oq.adapters"]
    mydb = "oq_adapters.mydb:MyDBAdapter"

The entry point must resolve to an :class:`oq.runtime.contract.Adapter`
subclass, a :class:`~oq.runtime.contract.DocumentAdapter` subclass, or a
:class:`~oq.runtime.contract.KeyValueAdapter` subclass. Loading a plugin must
never open a connection or import a database driver at module import time; a
driver may be imported lazily inside ``connect``.

This module is deliberately small. It is the stable surface external adapter
authors program against; the database packages in ``adapters/`` use the same
surface.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from oq.runtime.contract import (
    CAP_ARRAY_TYPE,
    CAP_CHECK_CONSTRAINTS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_INDEXES,
    CAP_JSON,
    CAP_MATERIALIZED_VIEWS,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_TYPE,
    Adapter,
    CapabilityReport,
    DocumentAdapter,
    KeyValueAdapter,
    TypeSupportLevel,
)

ADAPTER_ENTRY_POINT_GROUP = "oq.adapters"
"""Entry point group read by the CLI and by :func:`discover_plugins`."""

REQUIRED_METHODS = (
    "id",
    "display_name",
    "package_version",
    "capabilities",
    "connect",
    "inspect_schema",
    "compile_schema",
    "compile_query",
    "execute",
    "begin",
    "commit",
    "rollback",
    "detect_version",
    "close",
)

CANONICAL_TYPE_KINDS = frozenset(
    {
        "integer",
        "float",
        "decimal",
        "text",
        "boolean",
        "date",
        "time",
        "timestamp",
        "duration",
        "uuid",
        "email",
        "url",
        "json",
        "binary",
        "geo",
        "list",
        "set",
        "map",
        "vector",
        "reference",
    }
)

STANDARD_CAPABILITY_KEYS = frozenset(
    {
        CAP_TRANSACTIONS,
        CAP_TRANSACTIONAL_DDL,
        CAP_RETURNING,
        CAP_ORDER_BY_NULLS,
        CAP_PARTIAL_INDEXES,
        CAP_VECTOR_TYPE,
        CAP_JSON,
        CAP_ARRAY_TYPE,
        CAP_UUID_TYPE,
        CAP_GEO_TYPE,
        CAP_DURATION_TYPE,
        CAP_EMAIL_TYPE,
        CAP_URL_TYPE,
        CAP_DECIMAL_UNBOUNDED,
        CAP_CHECK_CONSTRAINTS,
        CAP_FOREIGN_KEYS,
        CAP_UNIQUE_CONSTRAINTS,
        CAP_INDEXES,
        CAP_FULL_TEXT_SEARCH,
        CAP_MATERIALIZED_VIEWS,
        CAP_PARTITIONING,
        CAP_REPLICATION,
    }
)


class PluginError(Exception):
    """A plugin failed discovery or contract validation."""

    def __init__(self, message: str, *, plugin_id: str | None = None) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id


@dataclass(frozen=True, slots=True)
class AdapterPlugin:
    """An adapter implementation plus the metadata developers must publish."""

    adapter: Any
    """An Adapter, DocumentAdapter, or KeyValueAdapter instance."""

    distribution: str
    """Distribution name, for example ``oq-adapter-neo4j``."""

    data_model: str
    """One of ``relational``, ``document``, ``key-value``, ``wide-column``,
    ``graph``, ``search``, ``vector``, ``analytical``."""

    ir_subset: tuple[str, ...]
    """IR operations the adapter actually implements, for example
    ``("schema", "select")``. Empty means schema-only."""

    documented_limitations: tuple[str, ...] = ()
    """Limitations that are not visible in the capability report."""

    repository: str | None = None
    """Optional upstream repository or documentation URL."""

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return str(self.adapter.id)

    @property
    def display_name(self) -> str:
        return str(self.adapter.display_name)

    def __post_init__(self) -> None:
        if self.data_model not in {
            "relational",
            "document",
            "key-value",
            "wide-column",
            "graph",
            "search",
            "vector",
            "analytical",
        }:
            raise PluginError(
                f"unknown data model {self.data_model!r}",
                plugin_id=self.id,
            )


def _adapter_classes() -> tuple[type[Any], ...]:
    return (Adapter, DocumentAdapter, KeyValueAdapter)


def validate_adapter(adapter: Any, *, require_tests_manifest: bool = True) -> CapabilityReport:
    """Validate an adapter instance and return its capability report.

    The checks are intentionally about *honesty* and *contract shape*, not about
    a particular engine. They are cheap and never connect to a database.

    Raises:
        PluginError: if a required method is missing, ids disagree, a canonical
            type is absent from the report, or a standard capability key is
            missing.
    """

    missing = [name for name in REQUIRED_METHODS if not hasattr(adapter, name)]
    if missing:
        raise PluginError(
            f"adapter is missing required members: {', '.join(missing)}",
            plugin_id=getattr(adapter, "id", None),
        )

    adapter_id = getattr(adapter, "id", None)
    if not isinstance(adapter_id, str) or not adapter_id:
        raise PluginError("adapter id must be a non-empty string")
    if (
        adapter_id != adapter_id.lower()
        or not adapter_id.replace("_", "").replace("-", "").isalnum()
    ):
        raise PluginError(
            "adapter id must be lower-case ASCII, digits, '-' or '_'",
            plugin_id=adapter_id,
        )

    if not isinstance(adapter, _adapter_classes()):
        # Structural adapters from other implementations are allowed, but they
        # must not silently bypass the runtime's contract classes.
        raise PluginError(
            "adapter must subclass Adapter, DocumentAdapter, or KeyValueAdapter",
            plugin_id=adapter_id,
        )

    report = adapter.capabilities()
    if not isinstance(report, CapabilityReport):
        raise PluginError("capabilities() must return CapabilityReport", plugin_id=adapter_id)
    if report.adapter != adapter_id:
        raise PluginError(
            f"capability report adapter id {report.adapter!r} != adapter id {adapter_id!r}",
            plugin_id=adapter_id,
        )

    absent_types = sorted(CANONICAL_TYPE_KINDS - set(report.types))
    if absent_types:
        raise PluginError(
            "capability report omits universal types: " + ", ".join(absent_types),
            plugin_id=adapter_id,
        )

    absent_capabilities = sorted(STANDARD_CAPABILITY_KEYS - set(report.capabilities))
    if absent_capabilities:
        raise PluginError(
            "capability report omits standard capabilities: " + ", ".join(absent_capabilities),
            plugin_id=adapter_id,
        )

    for kind, support in report.types.items():
        if support.support is TypeSupportLevel.UNSUPPORTED and not support.notes:
            raise PluginError(
                f"type {kind!r} is unsupported but has no explanatory note",
                plugin_id=adapter_id,
            )
        if support.support is not TypeSupportLevel.UNSUPPORTED and not support.native:
            raise PluginError(
                f"type {kind!r} is supported but has no native type name",
                plugin_id=adapter_id,
            )

    # Tested versions are a claim. The caller can only enforce shape here;
    # the conformance suite checks that the manifest and report agree.
    if (
        require_tests_manifest
        and report.tested_versions
        and any(not version.strip() for version in report.tested_versions)
    ):
        raise PluginError(
            "tested_versions contains an empty entry",
            plugin_id=adapter_id,
        )
    return report


def discover_plugins(
    group: str = ADAPTER_ENTRY_POINT_GROUP,
) -> dict[str, AdapterPlugin]:
    """Load installed adapters without connecting to anything.

    Duplicate ids are an error. Broken distributions are reported instead of
    being silently ignored, because a missing adapter must not look like an
    unsupported database.
    """

    found: dict[str, AdapterPlugin] = {}
    for entry in _entry_points(group):
        try:
            loaded = entry.load()
        except Exception as exc:  # pragma: no cover - import failures are env-specific
            raise PluginError(
                f"could not load entry point {entry.name!r}: {exc}",
                plugin_id=entry.name,
            ) from exc

        adapter = _instantiate(loaded)
        plugin = _plugin_from_adapter(adapter, distribution=entry.value.split(":", 1)[0])
        if plugin.id in found:
            raise PluginError(
                f"duplicate adapter id {plugin.id!r} from {plugin.distribution!r} and "
                f"{found[plugin.id].distribution!r}",
                plugin_id=plugin.id,
            )
        found[plugin.id] = plugin
    return found


def _entry_points(group: str) -> Iterable[EntryPoint]:
    selected = entry_points(group=group)
    return sorted(selected, key=lambda entry: entry.name)


def _instantiate(loaded: Callable[[], Any] | type[Any] | Any) -> Any:
    if isinstance(loaded, type):
        return loaded()
    if callable(loaded) and not hasattr(loaded, "capabilities"):
        return loaded()
    return loaded


def _plugin_from_adapter(adapter: Any, *, distribution: str) -> AdapterPlugin:
    report = validate_adapter(adapter)
    data_model = _infer_data_model(adapter)
    return AdapterPlugin(
        adapter=adapter,
        distribution=distribution,
        data_model=data_model,
        ir_subset=tuple(getattr(adapter, "ir_subset", ())),
        documented_limitations=tuple(report.limitations),
    )


def _infer_data_model(adapter: Any) -> str:
    declared = getattr(adapter, "data_model", None)
    if isinstance(declared, str):
        return declared
    if isinstance(adapter, DocumentAdapter):
        return "document"
    if isinstance(adapter, KeyValueAdapter):
        return "key-value"
    return "relational"


def register_plugin(registry: dict[str, AdapterPlugin], plugin: AdapterPlugin) -> None:
    """Register a plugin in a caller-owned registry, rejecting duplicates."""

    existing = registry.get(plugin.id)
    if existing is not None:
        raise PluginError(
            f"duplicate adapter id {plugin.id!r} from {plugin.distribution!r} and "
            f"{existing.distribution!r}",
            plugin_id=plugin.id,
        )
    registry[plugin.id] = plugin


__all__ = [
    "ADAPTER_ENTRY_POINT_GROUP",
    "CANONICAL_TYPE_KINDS",
    "REQUIRED_METHODS",
    "STANDARD_CAPABILITY_KEYS",
    "AdapterPlugin",
    "PluginError",
    "discover_plugins",
    "register_plugin",
    "validate_adapter",
]
