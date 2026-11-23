"""Redis adapter for Querly.

This module implements the Phase 5 key-value adapter contract against a real
Redis server through redis-py. It represents Redis' own data model: a keyspace
whose values are strings, hashes, lists, sets, and sorted sets, with optional
key expiry. A key is not a relational table and a Redis command is not a SQL
statement; this adapter never pretends otherwise.

Safety model
------------
Redis command names are data, so accepting an arbitrary command string and
calling ``getattr(client, command)`` would be a remote-code/administration
hazard. Commands are validated against an explicit allowlist. Administrative
and dangerous commands (``FLUSHALL``, ``CONFIG``, ``EVAL``, ``SCRIPT``,
``SHUTDOWN``, ``KEYS``, ...) are refused with a structured diagnostic.

Compiled plans carry a JSON-safe native plan in ``PlanDocument.engine_plan``.
Values are always passed as separate arguments to redis-py, never concatenated
into a command string.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from oq.ir import (
    AdapterDiagnostic,
    Artifact,
    KeySpaceSchema,
    KeySpec,
    KeyValueOperation,
    KeyValueQuery,
    Safety,
)
from oq.runtime.contract import (
    CAP_DOCUMENT_CRUD,
    CAP_KEY_EXPIRATION,
    CAP_KEY_INSPECTION,
    CAP_KEY_SPACE_NOTIFICATIONS,
    CAP_KEY_VALUE_HASHES,
    CAP_KEY_VALUE_LISTS,
    CAP_KEY_VALUE_SETS,
    CAP_KEY_VALUE_SORTED_SETS,
    CAP_KEY_VALUE_STRINGS,
    CAP_SAFE_COMMAND_EXECUTION,
    CAP_TRANSACTIONS,
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    EngineVersion,
    KeyValueAdapter,
    KeyValueResult,
    PlanDocument,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)

import redis
from redis.exceptions import (
    AuthenticationError,
    RedisError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

__all__ = ["RedisAdapter", "RedisConnection", "RedisTransaction"]


# ---------------------------------------------------------------------------
# Type mapping: Redis native structures -> universal types
# ---------------------------------------------------------------------------

REDIS_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "string (64-bit signed integer encoding)",
        range="-9223372036854775808..9223372036854775807",
        notes="INCR/DECR give atomic integer semantics; Redis validates the encoding.",
    ),
    "float": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "string (double)",
        notes="INCRBYFLOAT uses long double internally; store floats as strings otherwise.",
    ),
    "decimal": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "string",
        notes="No native decimal arithmetic; store as string and compute application-side.",
    ),
    "text": TypeSupport(TypeSupportLevel.EXACT, "string"),
    "boolean": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "string",
        notes="No native boolean; adapters encode it in a string.",
    ),
    "date": TypeSupport(TypeSupportLevel.APPROXIMATE, "string", notes="No native date type."),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "string", notes="No native time type."),
    "timestamp": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "string or integer",
        notes="Unix seconds/milliseconds or an ISO 8601 string, by convention.",
    ),
    "duration": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "integer", notes="Milliseconds by convention."
    ),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "string", notes="Textual UUID."),
    "email": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native email type."),
    "url": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native URL type."),
    "json": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "string (JSON text)",
        notes="Core Redis has no JSON document type; Redis Stack adds a JSON module.",
    ),
    "binary": TypeSupport(
        TypeSupportLevel.EXACT,
        "string (binary-safe)",
        notes="Redis strings can hold arbitrary bytes.",
    ),
    "geo": TypeSupport(
        TypeSupportLevel.UNSUPPORTED,
        None,
        notes="Core Redis has no geo type; Redis Stack adds GEO commands.",
    ),
    "list": TypeSupport(
        TypeSupportLevel.EXACT,
        "list",
        notes="Native list with O(1) pushes/pops at both ends.",
    ),
    "set": TypeSupport(TypeSupportLevel.EXACT, "set", notes="Native unordered set."),
    "map": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "hash",
        notes="Hashes are flat string-to-string maps; nesting must be encoded.",
    ),
    "vector": TypeSupport(
        TypeSupportLevel.UNSUPPORTED, None, notes="Core Redis has no vector type."
    ),
    "reference": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "string", notes="Keys are strings; no integrity."
    ),
    "sorted_set": TypeSupport(
        TypeSupportLevel.EXACT,
        "zset",
        notes="Native sorted set of unique members ordered by a floating score.",
    ),
}


REDIS_CAPABILITIES: dict[str, Capability] = {
    CAP_KEY_VALUE_STRINGS: Capability(True, "SET, GET, MGET, INCR, APPEND, ..."),
    CAP_KEY_VALUE_HASHES: Capability(True, "HSET, HGET, HGETALL, HDEL, HINCRBY, ..."),
    CAP_KEY_VALUE_LISTS: Capability(True, "LPUSH, RPUSH, LPOP, LRANGE, LMOVE, ..."),
    CAP_KEY_VALUE_SETS: Capability(True, "SADD, SREM, SMEMBERS, SINTER, SUNION, ..."),
    CAP_KEY_VALUE_SORTED_SETS: Capability(True, "ZADD, ZRANGE, ZSCORE, ZINCRBY, ..."),
    CAP_KEY_EXPIRATION: Capability(
        True,
        "EXPIRE, PEXPIRE, EXPIREAT, PEXPIREAT, TTL, PTTL, PERSIST, EXPIRETIME",
    ),
    CAP_KEY_INSPECTION: Capability(True, "TYPE, TTL, OBJECT ENCODING, MEMORY USAGE, SCAN"),
    CAP_SAFE_COMMAND_EXECUTION: Capability(
        True, "Explicit command allowlist; administrative and script commands are refused"
    ),
    CAP_KEY_SPACE_NOTIFICATIONS: Capability(
        True, "Pub/Sub notifications after CONFIG SET notify-keyspace-events"
    ),
    CAP_TRANSACTIONS: Capability(
        True,
        "MULTI/EXEC is atomic queuing but has no rollback: a failing queued command "
        "does not undo the others.",
    ),
    CAP_DOCUMENT_CRUD: Capability(
        False, "Redis is a key-value store; document queries are not emulated."
    ),
}


# ---------------------------------------------------------------------------
# Command classification
# ---------------------------------------------------------------------------

READ_COMMANDS = {
    "GET",
    "GETDEL",
    "GETEX",
    "GETSET",
    "MGET",
    "STRLEN",
    "HGET",
    "HMGET",
    "HGETALL",
    "HEXISTS",
    "HLEN",
    "HKEYS",
    "HVALS",
    "HRANDFIELD",
    "HSCAN",
    "LLEN",
    "LRANGE",
    "LINDEX",
    "LPOS",
    "SMEMBERS",
    "SISMEMBER",
    "SMISMEMBER",
    "SCARD",
    "SINTER",
    "SUNION",
    "SDIFF",
    "SRANDMEMBER",
    "SSCAN",
    "ZSCORE",
    "ZMSCORE",
    "ZCARD",
    "ZCOUNT",
    "ZLEXCOUNT",
    "ZRANGE",
    "ZREVRANGE",
    "ZRANGEBYSCORE",
    "ZREVRANGEBYSCORE",
    "ZRANGEBYLEX",
    "ZREVRANGEBYLEX",
    "ZRANK",
    "ZREVRANK",
    "ZSCAN",
    "TTL",
    "PTTL",
    "EXPIRETIME",
    "PEXPIRETIME",
    "TYPE",
    "EXISTS",
    "SCAN",
    "DBSIZE",
    "RANDOMKEY",
    "OBJECT",
    "MEMORY_USAGE",
}

WRITE_COMMANDS = {
    "SET",
    "SETNX",
    "MSET",
    "MSETNX",
    "APPEND",
    "INCR",
    "DECR",
    "INCRBY",
    "DECRBY",
    "INCRBYFLOAT",
    "HSET",
    "HSETNX",
    "HDEL",
    "HINCRBY",
    "HINCRBYFLOAT",
    "LPUSH",
    "RPUSH",
    "LPOP",
    "RPOP",
    "LSET",
    "LTRIM",
    "LINSERT",
    "LMOVE",
    "RPOPLPUSH",
    "SADD",
    "SREM",
    "SMOVE",
    "SPOP",
    "ZADD",
    "ZREM",
    "ZINCRBY",
    "ZPOPMIN",
    "ZPOPMAX",
    "EXPIRE",
    "PEXPIRE",
    "EXPIREAT",
    "PEXPIREAT",
    "PERSIST",
    "RENAME",
    "RENAMENX",
    "TOUCH",
}

DESTRUCTIVE_COMMANDS = {"DEL", "UNLINK"}

BLOCKED_COMMANDS: dict[str, str] = {
    "FLUSHDB": "flushes the whole logical database",
    "FLUSHALL": "flushes every database on the server",
    "CONFIG": "changes server configuration",
    "ACL": "changes access control",
    "SHUTDOWN": "stops the server",
    "DEBUG": "exposes server internals",
    "EVAL": "executes arbitrary Lua on the server",
    "EVALSHA": "executes arbitrary cached Lua on the server",
    "EVAL_RO": "executes arbitrary Lua on the server",
    "FCALL": "executes arbitrary server functions",
    "FCALL_RO": "executes arbitrary server functions",
    "SCRIPT": "manages server-side scripts",
    "FUNCTION": "manages server-side functions",
    "MONITOR": "streams every command executed on the server",
    "CLIENT": "inspects or disconnects other clients",
    "SLAVEOF": "reconfigures replication",
    "REPLICAOF": "reconfigures replication",
    "MIGRATE": "copies keys to another server",
    "MODULE": "loads server modules",
    "SAVE": "writes a blocking server snapshot",
    "BGSAVE": "starts a server snapshot",
    "BGREWRITEAOF": "rewrites the server append-only file",
    "KEYS": "can block the server on large keyspaces; use SCAN instead",
    "COMMAND": "exposes server command metadata",
    "INFO": "exposes server internals and configuration",
    "LATENCY": "exposes server internals",
    "SLOWLOG": "exposes command history",
    "SUBSCRIBE": "enters Pub/Sub mode and changes the connection state",
    "PSUBSCRIBE": "enters Pub/Sub mode and changes the connection state",
    "MONITOR_": "reserved",
}

SUPPORTED_COMMANDS = READ_COMMANDS | WRITE_COMMANDS | DESTRUCTIVE_COMMANDS

EXPIRATION_COMMANDS = {
    "EXPIRE",
    "PEXPIRE",
    "EXPIREAT",
    "PEXPIREAT",
    "TTL",
    "PTTL",
    "EXPIRETIME",
    "PEXPIRETIME",
    "PERSIST",
}

MULTI_KEY_COMMANDS = {
    "MGET",
    "MSET",
    "MSETNX",
    "SINTER",
    "SUNION",
    "SDIFF",
    "DEL",
    "UNLINK",
    "EXISTS",
}


# ---------------------------------------------------------------------------
# Value codec
# ---------------------------------------------------------------------------


def _encode_value(value: Any) -> Any:
    """Encode a Python value as JSON-safe data, preserving binary values."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "$binary": {
                "base64": base64.b64encode(bytes(value)).decode("ascii"),
                "subType": "00",
            }
        }
    if isinstance(value, Mapping):
        return {str(key): _encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = [_encode_value(item) for item in value]
        if isinstance(value, set):
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return items
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$binary"}:
            info = value["$binary"]
            return base64.b64decode(info["base64"])
        if set(value) == {"$date"}:
            return datetime.fromisoformat(str(value["$date"]))
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def _text(value: Any) -> Any:
    """Normalize a redis-py reply to JSON-safe values without losing bytes."""

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "$binary": {
                    "base64": base64.b64encode(value).decode("ascii"),
                    "subType": "00",
                }
            }
    if isinstance(value, dict):
        return {_text(key): _text(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_text(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


# ---------------------------------------------------------------------------
# Connection & transaction
# ---------------------------------------------------------------------------


class RedisConnection(Connection):
    """An open Redis connection."""

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
            return bool(self._client.ping())
        except RedisError:
            return False

    @property
    def client(self) -> redis.Redis:
        return self._client

    def close(self) -> None:
        if not self._closed:
            self._client.close()
            self._closed = True


class RedisTransaction(Transaction):
    """Redis MULTI/EXEC transaction.

    Redis queues commands and executes them atomically, but it cannot roll back
    a command that fails at execution time. ``rollback`` discards a queue that
    has not been executed; it cannot undo committed writes.
    """

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

    @property
    def pipeline(self) -> Any:
        return self._pipeline

    def commit(self) -> None:
        if self._active:
            self._pipeline.execute()
            self._active = False

    def rollback(self) -> None:
        if self._active:
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
    """Key-value adapter for Redis."""

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

    # -- capability reporting -------------------------------------------------

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.redis import __version__

        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="Redis",
            tested_versions=["8.10.3"],
            capabilities=dict(REDIS_CAPABILITIES),
            types=dict(REDIS_TYPE_MAP),
            limitations=[
                "No relational tables, joins, or foreign keys.",
                "Keys have no declared schema; the value type is discovered at runtime.",
                "MULTI/EXEC has no rollback of execution-time failures.",
                "Core Redis has no JSON, document, vector, or geo type.",
                "Server-side Lua and administrative commands are refused by policy.",
                "KEYS is refused because it blocks the server; SCAN is supported.",
            ],
            generated_at=datetime.now(UTC),
        )

    # -- connection -----------------------------------------------------------

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "redis":
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Expected adapter 'redis', got {config.adapter!r}",
            )

        host = config.host or "localhost"
        port = config.port or 6379
        password = secrets.get("password") if secrets is not None else None
        options = dict(config.options)
        ssl = options.get("ssl", "false").lower() in {"1", "true", "yes"}

        try:
            client = redis.Redis(
                host=host,
                port=port,
                db=int(config.database or options.get("db", "0")),
                username=config.user or None,
                password=password or None,
                decode_responses=False,
                socket_connect_timeout=float(options.get("connect_timeout", "5")),
                socket_timeout=float(options.get("socket_timeout", "5")),
                health_check_interval=int(options.get("health_check_interval", "0")),
                ssl=ssl,
            )
            client.ping()
            return RedisConnection(config, client)
        except AuthenticationError as exc:
            raise AdapterError(
                AdapterErrorCode.AUTH,
                "Redis authentication failed.",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc
        except RedisConnectionError as exc:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Failed to connect to Redis at {host}:{port}.",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc
        except RedisError as exc:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Redis connection error.",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc

    # -- inspection -----------------------------------------------------------

    def inspect_schema(self, connection: Connection) -> KeySpaceSchema:
        conn = self._require_connection(connection)
        client = conn.client
        max_keys = int(conn.config.options.get("inspect_max_keys", "10000"))
        key_specs: list[KeySpec] = []
        cursor = 0

        while True:
            cursor, keys = client.scan(cursor=cursor, count=100)
            for raw_key in keys:
                key = _text(raw_key)
                data_type = _text(client.type(raw_key))
                assert isinstance(data_type, str)
                ttl = int(client.ttl(raw_key))
                ttl_seconds = ttl if ttl > 0 else None
                encoding = None
                try:
                    encoding = _text(client.object("encoding", raw_key))
                except RedisError:
                    encoding = None
                size = self._key_size(client, raw_key, data_type)
                memory = None
                try:
                    memory = client.memory_usage(raw_key)
                except RedisError:
                    memory = None
                key_specs.append(
                    KeySpec(
                        pattern=str(key),
                        data_type=data_type,
                        ttl_seconds=ttl_seconds,
                        encoding=str(encoding) if encoding is not None else None,
                        size=size,
                        memory_bytes=int(memory) if memory is not None else None,
                    )
                )
                if len(key_specs) >= max_keys:
                    break
            if cursor == 0 or len(key_specs) >= max_keys:
                break

        key_specs.sort(key=lambda spec: spec.pattern)
        return KeySpaceSchema(key_specs=key_specs)

    @staticmethod
    def _key_size(client: redis.Redis, key: bytes, data_type: str) -> int | None:
        if data_type == "string":
            return int(client.strlen(key))
        if data_type == "hash":
            return int(client.hlen(key))
        if data_type == "list":
            return int(client.llen(key))
        if data_type == "set":
            return int(client.scard(key))
        if data_type == "zset":
            return int(client.zcard(key))
        return None

    # -- schema compilation ---------------------------------------------------

    def schema_diagnostics(self, schema: KeySpaceSchema) -> list[AdapterDiagnostic]:
        """Explain why Redis cannot accept schema DDL.

        Redis has no catalog and no ``CREATE`` statements. Key specs are
        descriptive conventions, not enforceable schema. Returning [] from
        :meth:`compile_schema` is therefore honest, and this method gives callers
        the structured reason rather than an empty list with no explanation.
        """

        return [
            AdapterDiagnostic(
                code=AdapterErrorCode.UNSUPPORTED.value,
                message=(
                    "Redis has no schema DDL. Key specs describe conventions; they are not "
                    "created, validated, or enforced by the server."
                ),
                operation="compile_schema",
                feature="schema",
                hint="Create keys with write commands (SET, HSET, LPUSH, SADD, ZADD).",
                details={"key_spec_count": len(schema.key_specs)},
            )
        ]

    def compile_schema(self, schema: KeySpaceSchema, options: CompileOptions) -> list[DDLArtifact]:
        # Deliberately empty: emitting pseudo-DDL would imply Redis enforces a
        # schema it does not have. Callers can query schema_diagnostics().
        return []

    # -- query compilation ----------------------------------------------------

    def compile_query(self, query: KeyValueQuery, schema: KeySpaceSchema) -> PlanDocument:
        diagnostics: list[AdapterDiagnostic] = []
        if not query.operations:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.SYNTAX.value,
                    message="A Redis plan requires at least one operation.",
                    operation="keyvalue",
                    feature="operations",
                )
            )
            return self._unsupported_plan(query, diagnostics)

        for position, operation in enumerate(query.operations):
            diagnostics.extend(self._validate_operation(operation, position))

        if diagnostics:
            return self._unsupported_plan(query, diagnostics)

        plan_operations = [self._encode_operation(operation) for operation in query.operations]
        engine_plan = {"adapter": self.id, "operations": plan_operations}
        artifact = json.dumps(
            engine_plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        required = (
            [CAP_KEY_EXPIRATION]
            if any(op.command.upper() in EXPIRATION_COMMANDS for op in query.operations)
            else []
        )

        return PlanDocument(
            target_adapter=self.id,
            operation="keyvalue",
            artifact=Artifact(kind="redis", text=artifact, dialect="redis"),
            parameters=[
                {"name": parameter.name, "type": parameter.type.kind}
                for parameter in query.parameters
            ],
            required_capabilities=required,
            safety=self._safety(query),
            result_columns=[],
            engine_plan=engine_plan,
            diagnostics=[],
            supported=True,
        )

    def _unsupported_plan(
        self, query: KeyValueQuery, diagnostics: list[AdapterDiagnostic]
    ) -> PlanDocument:
        return PlanDocument(
            target_adapter=self.id,
            operation="keyvalue",
            artifact=Artifact(kind="redis", text="", dialect="redis"),
            parameters=[
                {"name": parameter.name, "type": parameter.type.kind}
                for parameter in query.parameters
            ],
            safety=Safety(classification="read"),
            engine_plan=None,
            diagnostics=diagnostics,
            supported=False,
        )

    def _validate_operation(
        self, operation: KeyValueOperation, position: int
    ) -> list[AdapterDiagnostic]:
        command = operation.command.strip().upper()
        diagnostics: list[AdapterDiagnostic] = []
        if not command:
            return [
                AdapterDiagnostic(
                    code=AdapterErrorCode.SYNTAX.value,
                    message="A Redis operation requires a command.",
                    operation="keyvalue",
                    path=f"operations[{position}]",
                )
            ]
        if command in BLOCKED_COMMANDS:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.UNSUPPORTED.value,
                    message=(
                        f"Redis command {command} is refused because it "
                        f"{BLOCKED_COMMANDS[command]}."
                    ),
                    operation=command,
                    feature=command,
                    path=f"operations[{position}]",
                    hint="Use SCAN for key iteration and the documented data commands.",
                )
            )
            return diagnostics
        if command == "OBJECT":
            subcommand = str(operation.args[0]).upper() if operation.args else ""
            if subcommand not in {"ENCODING", "IDLETIME", "REFCOUNT", "FREQ"}:
                diagnostics.append(
                    AdapterDiagnostic(
                        code=AdapterErrorCode.UNSUPPORTED.value,
                        message=f"Unsupported OBJECT subcommand {subcommand!r}.",
                        operation=command,
                        feature=subcommand or "OBJECT",
                        path=f"operations[{position}]",
                    )
                )
            return diagnostics
        if command not in SUPPORTED_COMMANDS:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.UNSUPPORTED.value,
                    message=f"Redis command {command!r} is not in the safe command allowlist.",
                    operation=command,
                    feature=command,
                    path=f"operations[{position}]",
                    hint="Relational SQL is not executable on Redis.",
                )
            )
            return diagnostics
        if not operation.key and command not in {
            "SCAN",
            "DBSIZE",
            "RANDOMKEY",
            "MGET",
            "MSET",
            "MSETNX",
        }:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.SYNTAX.value,
                    message=f"Redis command {command} requires a key.",
                    operation=command,
                    feature="key",
                    path=f"operations[{position}]",
                )
            )
        if operation.ttl_seconds is not None and command not in {"SET", "SETNX"}:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.UNSUPPORTED.value,
                    message=(
                        "ttl_seconds is only accepted on SET/SETNX. Use EXPIRE/PEXPIRE "
                        "as an explicit operation for an existing key."
                    ),
                    operation=command,
                    feature="ttl_seconds",
                    path=f"operations[{position}]",
                )
            )
        return diagnostics

    def _encode_operation(self, operation: KeyValueOperation) -> dict[str, Any]:
        command = operation.command.strip().upper()
        options = {str(key): _encode_value(value) for key, value in operation.options.items()}
        if operation.ttl_seconds is not None:
            options["ex"] = int(operation.ttl_seconds)
        return {
            "command": command,
            "key": operation.key,
            "args": [_encode_value(arg) for arg in operation.args],
            "options": options,
        }

    def _safety(self, query: KeyValueQuery) -> Safety:
        commands = {operation.command.strip().upper() for operation in query.operations}
        if commands & DESTRUCTIVE_COMMANDS:
            return Safety(classification="destructive_write", destructive=True)
        if commands & WRITE_COMMANDS:
            return Safety(classification="write")
        return Safety(classification="read")

    # -- execution ------------------------------------------------------------

    def execute(
        self,
        connection: Connection,
        plan: PlanDocument,
        params: Mapping[str, Any],
    ) -> KeyValueResult:
        if plan.target_adapter != self.id:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                f"Plan targets {plan.target_adapter!r}, not 'redis'.",
            )
        if not plan.supported:
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                "The plan contains unsupported operations.",
                diagnostics=[diagnostic.model_dump(mode="json") for diagnostic in plan.diagnostics],
            )
        if plan.engine_plan is None:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Redis plan has no native operations.")

        conn = self._require_connection(connection)
        operations = self._bind(plan.engine_plan, params).get("operations", [])
        client = conn.client
        results: list[dict[str, Any]] = []
        affected_total = 0

        for position, operation in enumerate(operations):
            command = str(operation["command"]).upper()
            if command in BLOCKED_COMMANDS or command not in SUPPORTED_COMMANDS:
                raise AdapterError(
                    AdapterErrorCode.UNSUPPORTED,
                    f"Redis command {command!r} is not executable under the safe policy.",
                    diagnostics=[
                        {
                            "code": AdapterErrorCode.UNSUPPORTED.value,
                            "message": f"Command {command!r} is not in the allowlist.",
                            "operation": command,
                            "path": f"operations[{position}]",
                        }
                    ],
                )
            try:
                value, affected = self._execute_operation(client, command, operation)
            except ResponseError as exc:
                raise AdapterError(
                    AdapterErrorCode.DATA,
                    f"Redis rejected {command}.",
                    cause=exc,
                    redacted_cause=redact_message(str(exc)),
                ) from exc
            except RedisTimeoutError as exc:
                raise AdapterError(
                    AdapterErrorCode.TIMEOUT,
                    f"Redis command {command} timed out.",
                    retryable=True,
                    cause=exc,
                    redacted_cause=redact_message(str(exc)),
                ) from exc
            except AuthenticationError as exc:
                raise AdapterError(
                    AdapterErrorCode.AUTH,
                    "Redis authentication failed.",
                    cause=exc,
                    redacted_cause=redact_message(str(exc)),
                ) from exc
            except RedisConnectionError as exc:
                raise AdapterError(
                    AdapterErrorCode.CONNECTION,
                    "Redis connection was lost.",
                    retryable=True,
                    cause=exc,
                    redacted_cause=redact_message(str(exc)),
                ) from exc
            except RedisError as exc:
                raise AdapterError(
                    AdapterErrorCode.DATA,
                    f"Redis command {command} failed.",
                    cause=exc,
                    redacted_cause=redact_message(str(exc)),
                ) from exc

            results.append(
                {
                    "command": command,
                    "key": operation.get("key"),
                    "value": value,
                    "affected": affected,
                }
            )
            affected_total += affected

        return KeyValueResult(results=results, affected_count=affected_total, warnings=[])

    def _bind(
        self, plan: Mapping[str, Any], params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        safe_params = {name: _encode_value(value) for name, value in (params or {}).items()}

        def substitute(value: Any, path: str) -> Any:
            if isinstance(value, dict):
                if set(value) == {"$param"}:
                    name = value["$param"]
                    if name not in safe_params:
                        raise AdapterError(
                            AdapterErrorCode.DATA,
                            f"Missing bound parameter {name!r}.",
                        )
                    return safe_params[name]
                return {key: substitute(item, f"{path}.{key}") for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item, f"{path}[{index}]") for index, item in enumerate(value)]
            if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                name = value[1:-1]
                if name in safe_params:
                    return safe_params[name]
            return value

        return _decode_value(substitute(dict(plan), "plan"))

    # -- native command dispatch ---------------------------------------------

    @staticmethod
    def _mapping(args: list[Any]) -> dict[Any, Any]:
        if len(args) == 1 and isinstance(args[0], Mapping):
            return dict(args[0])
        if len(args) % 2 != 0:
            raise AdapterError(
                AdapterErrorCode.SYNTAX,
                "Expected field/value or member/score pairs.",
            )
        return {args[index]: args[index + 1] for index in range(0, len(args), 2)}

    @staticmethod
    def _zadd_mapping(args: list[Any]) -> dict[Any, Any]:
        if len(args) == 1 and isinstance(args[0], Mapping):
            return {member: score for member, score in args[0].items()}
        if (
            len(args) == 1
            and isinstance(args[0], list)
            and args[0]
            and isinstance(args[0][0], (list, tuple))
        ):
            return {pair[1]: pair[0] for pair in args[0]}
        if len(args) % 2 != 0:
            raise AdapterError(
                AdapterErrorCode.SYNTAX,
                "ZADD expects score/member pairs or a member-to-score mapping.",
            )
        return {args[index + 1]: args[index] for index in range(0, len(args), 2)}

    @staticmethod
    def _option(options: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in options:
                return options[name]
        return None

    @staticmethod
    def _affected(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        return 1 if value is not None else 0

    def _execute_operation(
        self, client: redis.Redis, command: str, operation: Mapping[str, Any]
    ) -> tuple[Any, int]:
        key = operation.get("key") or ""
        args: list[Any] = list(operation.get("args") or [])
        options: Mapping[str, Any] = dict(operation.get("options") or {})

        if command == "SET":
            value = args[0] if args else None
            kwargs = {
                name: options[name]
                for name in ("ex", "px", "exat", "pxat", "keepttl", "nx", "xx", "get")
                if name in options
            }
            result = client.set(key, value, **kwargs)
            return _text(result), self._affected(result)

        if command == "SETNX":
            if "ex" in options or "px" in options:
                kwargs = {name: options[name] for name in ("ex", "px") if name in options}
                result = client.set(key, args[0] if args else None, nx=True, **kwargs)
            else:
                result = client.setnx(key, args[0] if args else None)
            return _text(result), self._affected(result)

        if command == "GET":
            return _text(client.get(key)), 0
        if command == "GETDEL":
            return _text(client.getdel(key)), 0
        if command == "GETEX":
            kwargs = {
                name: options[name]
                for name in ("ex", "px", "exat", "pxat", "persist")
                if name in options
            }
            return _text(client.getex(key, **kwargs)), 0
        if command == "GETSET":
            return _text(client.getset(key, args[0])), 1
        if command == "MGET":
            keys = ([key] if key else []) + list(args)
            return _text(client.mget(keys)), 0
        if command == "STRLEN":
            return int(client.strlen(key)), 0
        if command == "APPEND":
            result = int(client.append(key, args[0]))
            return result, 1
        if command == "INCR":
            return int(client.incr(key)), 1
        if command == "DECR":
            return int(client.decr(key)), 1
        if command == "INCRBY":
            return int(client.incrby(key, args[0])), 1
        if command == "DECRBY":
            return int(client.decrby(key, args[0])), 1
        if command == "INCRBYFLOAT":
            return float(client.incrbyfloat(key, args[0])), 1
        if command in {"MSET", "MSETNX"}:
            mapping = self._mapping(args)
            if key and not mapping:
                mapping = {}
            result = client.mset(mapping) if command == "MSET" else client.msetnx(mapping)
            return _text(result), len(mapping) if result else 0

        if command == "HSET":
            mapping = self._mapping(args)
            result = int(client.hset(key, mapping=mapping))
            return result, result
        if command == "HSETNX":
            result = bool(client.hsetnx(key, args[0], args[1]))
            return result, int(result)
        if command == "HGET":
            return _text(client.hget(key, args[0])), 0
        if command == "HMGET":
            return _text(client.hmget(key, args)), 0
        if command == "HGETALL":
            result = client.hgetall(key)
            return _text(result), 0
        if command == "HDEL":
            result = int(client.hdel(key, *args))
            return result, result
        if command == "HEXISTS":
            result = bool(client.hexists(key, args[0]))
            return result, 0
        if command == "HLEN":
            return int(client.hlen(key)), 0
        if command == "HKEYS":
            return _text(client.hkeys(key)), 0
        if command == "HVALS":
            return _text(client.hvals(key)), 0
        if command == "HINCRBY":
            return int(client.hincrby(key, args[0], args[1])), 1
        if command == "HINCRBYFLOAT":
            return float(client.hincrbyfloat(key, args[0], args[1])), 1
        if command == "HRANDFIELD":
            count = args[0] if args else None
            withvalues = bool(self._option(options, "withvalues", "with_values"))
            return _text(client.hrandfield(key, count, withvalues)), 0
        if command == "HSCAN":
            cursor = int(args[0]) if args else 0
            result = client.hscan(
                key,
                cursor=cursor,
                match=self._option(options, "match"),
                count=self._option(options, "count"),
            )
            return {"cursor": result[0], "entries": _text(result[1])}, 0

        if command in {"LPUSH", "RPUSH"}:
            method = client.lpush if command == "LPUSH" else client.rpush
            result = int(method(key, *args))
            return result, len(args)
        if command in {"LPOP", "RPOP"}:
            method = client.lpop if command == "LPOP" else client.rpop
            count = args[0] if args else None
            return _text(method(key, count)), 1 if args or count is None else len(args)
        if command == "LLEN":
            return int(client.llen(key)), 0
        if command == "LRANGE":
            return _text(client.lrange(key, args[0], args[1])), 0
        if command == "LINDEX":
            return _text(client.lindex(key, args[0])), 0
        if command == "LSET":
            result = client.lset(key, args[0], args[1])
            return _text(result), 1
        if command == "LTRIM":
            result = client.ltrim(key, args[0], args[1])
            return _text(result), 1
        if command == "LINSERT":
            result = int(client.linsert(key, args[0], args[1], args[2]))
            return result, 1 if result >= 0 else 0
        if command == "LPOS":
            result = client.lpos(key, args[0], rank=options.get("rank"), count=options.get("count"))
            return _text(result), 0
        if command == "LMOVE":
            result = client.lmove(key, args[0], args[1], args[2])
            return _text(result), 1 if result is not None else 0
        if command == "RPOPLPUSH":
            result = client.rpoplpush(key, args[0])
            return _text(result), 1 if result is not None else 0

        if command == "SADD":
            result = int(client.sadd(key, *args))
            return result, result
        if command == "SREM":
            result = int(client.srem(key, *args))
            return result, result
        if command == "SMEMBERS":
            return _text(client.smembers(key)), 0
        if command == "SISMEMBER":
            result = bool(client.sismember(key, args[0]))
            return result, 0
        if command == "SMISMEMBER":
            return _text(client.smismember(key, args)), 0
        if command == "SCARD":
            return int(client.scard(key)), 0
        if command in {"SINTER", "SUNION", "SDIFF"}:
            method = {
                "SINTER": client.sinter,
                "SUNION": client.sunion,
                "SDIFF": client.sdiff,
            }[command]
            keys = ([key] if key else []) + list(args)
            return _text(method(keys)), 0
        if command == "SMOVE":
            result = bool(client.smove(key, args[0], args[1]))
            return result, int(result)
        if command == "SPOP":
            result = client.spop(key, args[0] if args else None)
            return _text(result), self._affected(result)
        if command == "SRANDMEMBER":
            return _text(client.srandmember(key, args[0] if args else None)), 0
        if command == "SSCAN":
            cursor = int(args[0]) if args else 0
            result = client.sscan(
                key,
                cursor=cursor,
                match=self._option(options, "match"),
                count=self._option(options, "count"),
            )
            return {"cursor": result[0], "members": _text(result[1])}, 0

        if command == "ZADD":
            mapping = self._zadd_mapping(args)
            kwargs = {
                name: options[name]
                for name in ("nx", "xx", "gt", "lt", "ch", "incr")
                if name in options
            }
            result = client.zadd(key, mapping, **kwargs)
            if isinstance(result, float):
                return result, 1
            return _text(result), int(result) if isinstance(result, int) else 1
        if command == "ZREM":
            result = int(client.zrem(key, *args))
            return result, result
        if command == "ZSCORE":
            return _text(client.zscore(key, args[0])), 0
        if command == "ZMSCORE":
            return _text(client.zmscore(key, args)), 0
        if command == "ZCARD":
            return int(client.zcard(key)), 0
        if command == "ZCOUNT":
            return int(client.zcount(key, args[0], args[1])), 0
        if command == "ZLEXCOUNT":
            return int(client.zlexcount(key, args[0], args[1])), 0
        if command in {"ZRANGE", "ZREVRANGE"}:
            start, stop = args[0], args[1]
            withscores = bool(self._option(options, "withscores", "with_scores"))
            if command == "ZRANGE":
                result = client.zrange(
                    key,
                    start,
                    stop,
                    desc=bool(self._option(options, "desc", "rev")),
                    withscores=withscores,
                    byscore=bool(self._option(options, "byscore")),
                    bylex=bool(self._option(options, "bylex")),
                    offset=self._option(options, "offset"),
                    num=self._option(options, "num"),
                )
            else:
                result = client.zrevrange(key, start, stop, withscores=withscores)
            return _text(result), 0
        if command in {"ZRANGEBYSCORE", "ZREVRANGEBYSCORE"}:
            minimum, maximum = args[0], args[1]
            withscores = bool(self._option(options, "withscores", "with_scores"))
            offset = self._option(options, "offset")
            num = self._option(options, "num")
            if command == "ZRANGEBYSCORE":
                result = client.zrangebyscore(
                    key, minimum, maximum, start=offset, num=num, withscores=withscores
                )
            else:
                result = client.zrevrangebyscore(
                    key, maximum, minimum, start=offset, num=num, withscores=withscores
                )
            return _text(result), 0
        if command in {"ZRANGEBYLEX", "ZREVRANGEBYLEX"}:
            if command == "ZRANGEBYLEX":
                result = client.zrangebylex(key, args[0], args[1])
            else:
                result = client.zrevrangebylex(key, args[0], args[1])
            return _text(result), 0
        if command == "ZRANK":
            return _text(client.zrank(key, args[0])), 0
        if command == "ZREVRANK":
            return _text(client.zrevrank(key, args[0])), 0
        if command == "ZINCRBY":
            return float(client.zincrby(key, args[0], args[1])), 1
        if command in {"ZPOPMIN", "ZPOPMAX"}:
            method = client.zpopmin if command == "ZPOPMIN" else client.zpopmax
            result = method(key, args[0] if args else None)
            return _text(result), len(result) if isinstance(result, list) else 1
        if command == "ZSCAN":
            cursor = int(args[0]) if args else 0
            result = client.zscan(
                key,
                cursor=cursor,
                match=self._option(options, "match"),
                count=self._option(options, "count"),
            )
            return {"cursor": result[0], "entries": _text(result[1])}, 0

        if command in {"EXPIRE", "PEXPIRE", "EXPIREAT", "PEXPIREAT"}:
            kwargs = {name: options[name] for name in ("nx", "xx", "gt", "lt") if name in options}
            method = {
                "EXPIRE": client.expire,
                "PEXPIRE": client.pexpire,
                "EXPIREAT": client.expireat,
                "PEXPIREAT": client.pexpireat,
            }[command]
            result = bool(method(key, args[0], **kwargs))
            return result, int(result)
        if command == "TTL":
            return int(client.ttl(key)), 0
        if command == "PTTL":
            return int(client.pttl(key)), 0
        if command == "EXPIRETIME":
            return int(client.expiretime(key)), 0
        if command == "PEXPIRETIME":
            return int(client.pexpiretime(key)), 0
        if command == "PERSIST":
            result = bool(client.persist(key))
            return result, int(result)

        if command == "TYPE":
            return _text(client.type(key)), 0
        if command == "EXISTS":
            keys = ([key] if key else []) + list(args)
            return int(client.exists(*keys)), 0
        if command in {"DEL", "UNLINK"}:
            method = client.delete if command == "DEL" else client.unlink
            keys = ([key] if key else []) + list(args)
            result = int(method(*keys))
            return result, result
        if command == "RENAME":
            result = client.rename(key, args[0])
            return _text(result), 1
        if command == "RENAMENX":
            result = bool(client.renamenx(key, args[0]))
            return result, int(result)
        if command == "TOUCH":
            return int(client.touch(key, *args)), 0
        if command == "DBSIZE":
            return int(client.dbsize()), 0
        if command == "RANDOMKEY":
            return _text(client.randomkey()), 0
        if command == "SCAN":
            cursor = int(args[0]) if args else 0
            result = client.scan(
                cursor=cursor,
                match=self._option(options, "match"),
                count=self._option(options, "count"),
                _type=self._option(options, "type"),
            )
            return {"cursor": result[0], "keys": _text(result[1])}, 0
        if command == "OBJECT":
            subcommand = str(args[0]).lower() if args else "encoding"
            return _text(client.object(subcommand, key)), 0
        if command == "MEMORY_USAGE":
            result = client.memory_usage(key, samples=options.get("samples"))
            return result, 0

        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED,
            f"Redis command {command!r} has no safe executor.",
        )

    def _require_connection(self, connection: Connection) -> RedisConnection:
        if not isinstance(connection, RedisConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Redis adapter received a connection it did not create.",
            )
        if connection.is_closed:
            raise AdapterError(AdapterErrorCode.CONNECTION, "Redis connection is closed.")
        return connection

    # -- transactions & lifecycle --------------------------------------------

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        conn = self._require_connection(connection)
        return RedisTransaction(conn, options)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, RedisTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Redis adapter received a transaction it did not create.",
            )
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if not isinstance(transaction, RedisTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Redis adapter received a transaction it did not create.",
            )
        transaction.rollback()

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require_connection(connection)
        info = conn.client.info("server")
        version = str(info.get("redis_version", "0.0.0"))
        parts = version.split(".")
        major = int(parts[0]) if parts and parts[0].isdigit() else 0
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return EngineVersion(
            product="Redis", version=version, major=major, minor=minor, patch=patch
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, RedisConnection):
            connection.close()
