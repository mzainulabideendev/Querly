"""Redis adapter for Querly.

This adapter uses redis-py to connect to a real Redis database. It implements
the KeyValueAdapter protocol for Phase 5 NoSQL support.

Architecture note: Key-value stores adapt the universal IR to a key-space data
model — keys with data types instead of tables, commands instead of queries,
TTLs instead of timestamps. Document stores are not key-value stores.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import redis
from redis.exceptions import AuthenticationError, ConnectionError as RedisConnectionError, RedisError

from oq.runtime.contract import (
    Adapter,
    Capability,
    CapabilityReport,
    Connection,
    ConnectionConfig,
    CompileOptions,
    DDLArtifact,
    EngineVersion,
    KeyValueAdapter,
    KeyValueQuery,
    KeyValueResult,
    KeySpaceSchema,
    PlanDocument,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    AdapterError,
    AdapterErrorCode,
    redact_message,
    CAP_KEY_EXPIRATION,
    CAP_KEY_SPACE_NOTIFICATIONS,
)

from oq.ir import KeySpec, KeyValueOperation

__all__ = ["RedisAdapter", "RedisConnection", "RedisTransaction"]


# ---------------------------------------------------------------------------
# Type mapping: Redis types -> universal types
# ---------------------------------------------------------------------------

REDIS_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="string (integer encoding)",
        range="-9223372036854775808..9223372036854775807",
        notes="Redis stores integers as strings; INCR/DECR operations provide atomic integer semantics",
    ),
    "float": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="string (float encoding)",
        notes="Redis stores floats as strings; no native float arithmetic",
    ),
    "decimal": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string",
        notes="Store as string; use Lua scripts for precise decimal arithmetic",
    ),
    "text": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="string",
    ),
    "boolean": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string ('true'/'false')",
        notes="No native boolean; store as string",
    ),
    "date": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string (ISO 8601)",
        notes="Store as ISO 8601 string; no native date type",
    ),
    "time": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string (ISO 8601)",
        notes="Store as ISO 8601 string",
    ),
    "timestamp": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string (ISO 8601) or integer (Unix epoch)",
        notes="Store as string or integer milliseconds since epoch",
    ),
    "duration": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native duration type; store as integer milliseconds",
    ),
    "uuid": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string",
        notes="Store as string UUID; no native generation",
    ),
    "email": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native email type",
    ),
    "url": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native URL type",
    ),
    "json": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="string (JSON text) or Redis JSON module (RediSearch/Redis Stack)",
        notes="Native string JSON; Redis Stack adds JSON module with query support",
    ),
    "binary": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="string (binary safe)",
        notes="Redis strings are binary-safe; can store arbitrary bytes",
    ),
    "geo": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native geo type in core Redis; Redis Stack adds GEO commands",
    ),
    "list": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="list",
        notes="Native LPUSH/RPUSH/LPOP/RPOP/LRANGE",
    ),
    "set": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="set",
        notes="Native SADD/SREM/SMEMBERS/SINTER/SUNION",
    ),
    "map": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="hash",
        notes="Native HSET/HGET/HGETALL/HDEL",
    ),
    "vector": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native vector type in core Redis; Redis Stack adds vector similarity",
    ),
    "reference": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native reference type; keys are strings",
    ),
}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

REDIS_CAPABILITIES: dict[str, Capability] = {
    CAP_KEY_EXPIRATION: Capability(
        supported=True,
        notes="EXPIRE, PEXPIRE, EXPIREAT, TTL, PTTL commands; keys evicted automatically",
    ),
    CAP_KEY_SPACE_NOTIFICATIONS: Capability(
        supported=True,
        notes="Keyspace notifications via Pub/Sub; requires CONFIG SET notify-keyspace-events",
    ),
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class RedisConnection(Connection):
    """Redis connection wrapper."""

    def __init__(self, config: ConnectionConfig, client: redis.Redis) -> None:
        self._config = config
        self._client = client
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    def ping(self) -> bool:
        try:
            return self._client.ping()
        except RedisError:
            return False

    @property
    def client(self) -> redis.Redis:
        return self._client

    def close(self) -> None:
        if not self._closed:
            self._client.close()
            self._closed = True


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class RedisTransaction(Transaction):
    """Redis transaction wrapper using MULTI/EXEC."""

    def __init__(self, connection: RedisConnection, options: TransactionOptions) -> None:
        self._connection = connection
        self._options = options
        self._pipeline = connection.client.pipeline(transaction=True)
        self._active = True

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def is_active(self) -> bool:
        return self._active and not self._connection.is_closed

    def commit(self) -> None:
        if self._active:
            self._pipeline.execute()
            self._active = False

    def rollback(self) -> None:
        if self._active:
            # Redis uses DISCARD to abort a transaction
            try:
                self._pipeline.reset()
            except RedisError:
                pass  # rollback must not raise
            finally:
                self._active = False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RedisAdapter(KeyValueAdapter):
    """Redis adapter for Querly."""

    @property
    def id(self) -> str:
        return "redis"

    @property
    def display_name(self) -> str:
        return "Redis"

    @property
    def package_version(self) -> str:
        from oq_adapters.redis import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.redis import __version__

        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="Redis",
            tested_versions=["7.4"],
            capabilities=REDIS_CAPABILITIES,
            types=REDIS_TYPE_MAP,
            limitations=[
                "No native multi-key transactions with rollback (MULTI/EXEC is optimistic)",
                "No joins or cross-key queries",
                "All values are strings; type interpretation is application-level",
                "Lua scripts are the only way to do atomic read-modify-write",
                "Sorted sets (ZSET) provide ordered maps with scores",
                "Streams provide append-only log with consumer groups",
            ],
            generated_at=datetime.now(timezone.utc),
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "redis":
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Expected redis adapter, got {config.adapter}",
            )

        host = config.host or "localhost"
        port = config.port or 6379
        user = config.user

        password = secrets.get("password") if secrets else None

        try:
            client = redis.Redis(
                host=host,
                port=port,
                username=user or None,
                password=password or None,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            # Force a connection to validate credentials
            client.ping()
            return RedisConnection(config, client)
        except AuthenticationError as e:
            raise AdapterError(
                AdapterErrorCode.AUTH,
                "Redis authentication failed",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )
        except RedisConnectionError as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Failed to connect to Redis at {host}:{port}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )
        except RedisError as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Redis connection error: {redact_message(str(e))}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )

    # ------------------------------------------------------------------
    # Schema inspection
    # ------------------------------------------------------------------

    def inspect_schema(self, connection: Connection) -> KeySpaceSchema:
        if not isinstance(connection, RedisConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for Redis adapter",
            )

        client = connection.client
        key_specs: list[KeySpec] = []

        # Sample keys using SCAN (not KEYS, which blocks)
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, count=100)
            for key in keys:
                key_type = client.type(key)
                ttl = client.ttl(key)
                ttl_seconds = ttl if ttl > 0 else None

                key_specs.append(
                    KeySpec(
                        pattern=key,
                        data_type=key_type.lower(),
                        ttl_seconds=ttl_seconds,
                    )
                )
            if cursor == 0:
                break

        return KeySpaceSchema(key_specs=key_specs, extensions=[])

    # ------------------------------------------------------------------
    # Schema compilation
    # ------------------------------------------------------------------

    def compile_schema(
        self, schema: KeySpaceSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        """Redis has no schema DDL - this is a no-op or creates key specs for documentation."""
        artifacts: list[DDLArtifact] = []

        for key_spec in schema.key_specs:
            # Generate a comment artifact documenting the expected key
            sql = f"-- Key: {key_spec.pattern}, Type: {key_spec.data_type}"
            if key_spec.ttl_seconds:
                sql += f", TTL: {key_spec.ttl_seconds}s"
            artifacts.append(
                DDLArtifact(
                    sql=sql,
                    statement_type="key_spec",
                    entity=key_spec.pattern,
                )
            )

        return artifacts

    # ------------------------------------------------------------------
    # Query compilation
    # ------------------------------------------------------------------

    def compile_query(
        self, query: KeyValueQuery, schema: KeySpaceSchema
    ) -> PlanDocument:
        from oq.ir import Artifact

        # Build Redis command artifact
        commands: list[str] = []
        for op in query.operations:
            cmd_parts = [op.command]
            if op.key:
                cmd_parts.append(op.key)
            cmd_parts.extend(op.args or [])
            commands.append(" ".join(cmd_parts))

        artifact_text = "; ".join(commands)

        return PlanDocument(
            target_adapter=self.id,
            operation="keyvalue",
            artifact=Artifact(
                kind="redis",
                text=artifact_text,
                dialect="redis",
            ),
            parameters=[
                {"name": p.name, "type": p.type.kind if p.type else "text"}
                for p in query.parameters
            ],
            required_capabilities=[],
            safety=query.safety,
            result_columns=[],
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        connection: Connection,
        plan: PlanDocument,
        params: Mapping[str, Any],
    ) -> KeyValueResult:
        if not isinstance(connection, RedisConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for Redis adapter",
            )

        client = connection.client
        results: list[dict[str, Any]] = []
        total_affected = 0

        try:
            # Parse and execute each operation from the artifact
            # The artifact text is a semicolon-separated list of Redis commands
            commands = self._parse_artifact(plan.artifact.text, params)

            for cmd in commands:
                result = self._execute_redis_command(client, cmd)
                results.append(result)
                if result.get("affected"):
                    total_affected += result["affected"]

            return KeyValueResult(
                results=results,
                affected_count=total_affected,
                warnings=[],
            )
        except RedisError as e:
            raise AdapterError(
                AdapterErrorCode.DATA,
                f"Redis operation failed: {redact_message(str(e))}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )

    def _parse_artifact(self, text: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Parse the artifact text into a list of Redis commands with bound parameters."""
        commands: list[dict[str, Any]] = []
        for part in text.split(";"):
            part = part.strip()
            if not part:
                continue
            parts = part.split()
            if not parts:
                continue

            # Substitute parameter placeholders
            resolved_parts = []
            for p in parts:
                if p.startswith("{") and p.endswith("}"):
                    param_name = p[1:-1]
                    if param_name in params:
                        resolved_parts.append(str(params[param_name]))
                    else:
                        resolved_parts.append(p)
                else:
                    resolved_parts.append(p)

            commands.append({
                "command": resolved_parts[0].upper(),
                "key": resolved_parts[1] if len(resolved_parts) > 1 else None,
                "args": resolved_parts[2:] if len(resolved_parts) > 2 else [],
            })

        return commands

    def _execute_redis_command(self, client: redis.Redis, cmd: dict[str, Any]) -> dict[str, Any]:
        """Execute a single Redis command and return normalized result."""
        command = cmd["command"]
        key = cmd["key"]
        args = cmd["args"]

        method = getattr(client, command.lower(), None)
        if method is None:
            return {
                "command": command,
                "key": key,
                "error": f"Unknown Redis command: {command}",
                "affected": 0,
            }

        try:
            if command in ("SET", "GET", "GETSET", "INCR", "DECR", "INCRBY", "DECRBY"):
                result = method(key, *args)
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": 1 if result is not None else 0,
                }
            elif command in ("HSET", "HGET", "HGETALL", "HDEL", "HEXISTS", "HLEN", "HKEYS", "HVALS"):
                result = method(key, *args)
                if command == "HGETALL" and isinstance(result, dict):
                    return {
                        "command": command,
                        "key": key,
                        "value": result,
                        "affected": len(result),
                    }
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": 1 if result is not None and result != 0 else 0,
                }
            elif command in ("LPUSH", "RPUSH", "LPOP", "RPOP", "LLEN", "LRANGE", "LINDEX", "LSET", "LTRIM"):
                result = method(key, *args)
                if command == "LRANGE" and isinstance(result, list):
                    return {
                        "command": command,
                        "key": key,
                        "value": result,
                        "affected": len(result),
                    }
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": 1 if result is not None and result != 0 else 0,
                }
            elif command in ("SADD", "SREM", "SMEMBERS", "SISMEMBER", "SCARD", "SINTER", "SUNION", "SDIFF", "SPOP", "SRANDMEMBER"):
                result = method(key, *args)
                if command in ("SMEMBERS", "SINTER", "SUNION", "SDIFF", "SRANDMEMBER") and isinstance(result, set):
                    return {
                        "command": command,
                        "key": key,
                        "value": list(result),
                        "affected": len(result),
                    }
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": result if isinstance(result, int) else (1 if result else 0),
                }
            elif command in ("ZADD", "ZREM", "ZRANGE", "ZREVRANGE", "ZRANGEBYSCORE", "ZREVRANGEBYSCORE", "ZCARD", "ZSCORE", "ZRANK", "ZREVRANK", "ZINCRBY"):
                result = method(key, *args)
                if command in ("ZRANGE", "ZREVRANGE", "ZRANGEBYSCORE", "ZREVRANGEBYSCORE") and isinstance(result, list):
                    return {
                        "command": command,
                        "key": key,
                        "value": result,
                        "affected": len(result),
                    }
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": result if isinstance(result, int) else (1 if result else 0),
                }
            elif command in ("EXPIRE", "PEXPIRE", "EXPIREAT", "PEXPIREAT", "TTL", "PTTL", "PERSIST"):
                result = method(key, *args)
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": 1 if result else 0,
                }
            elif command in ("DEL", "UNLINK", "EXISTS", "TYPE", "RENAME", "RENAMENX", "KEYS", "SCAN", "DBSIZE"):
                result = method(key, *args)
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": result if isinstance(result, int) else (1 if result else 0),
                }
            else:
                # Generic fallback
                result = method(key, *args)
                return {
                    "command": command,
                    "key": key,
                    "value": result,
                    "affected": 1 if result else 0,
                }
        except RedisError as e:
            return {
                "command": command,
                "key": key,
                "error": redact_message(str(e)),
                "affected": 0,
            }

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        if not isinstance(connection, RedisConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for Redis adapter",
            )
        return RedisTransaction(connection, options)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, RedisTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for Redis adapter",
            )
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if not isinstance(transaction, RedisTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for Redis adapter",
            )
        transaction.rollback()

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------

    def detect_version(self, connection: Connection) -> EngineVersion:
        if not isinstance(connection, RedisConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for Redis adapter",
            )

        info = connection.client.info("server")
        version_str = info.get("redis_version", "0.0.0")
        parts = version_str.split(".")
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0

        return EngineVersion(
            product="Redis",
            version=version_str,
            major=major,
            minor=minor,
            patch=patch,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, RedisConnection):
            connection.close()