"""ClickHouse adapter for Querly.

ClickHouse is an analytical, append-oriented database. It is not a row-store
OLTP engine: there are no enforced primary/foreign keys, no general
transactions, and no unique constraints. Tables are declared with a storage
engine and a sorting key. This adapter makes those differences explicit:
``CREATE TABLE ... ENGINE = MergeTree ORDER BY (...)``, storage-engine-aware
inspection, and ``{p:Type}`` server-side bind parameters through
``clickhouse-connect``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from oq.ir import (
    Artifact,
    Attribute,
    Entity,
    PlanDocument,
    QueryDocument,
    SchemaDocument,
    Type,
)
from oq.runtime.contract import (
    CAP_ANN_SEARCH,
    CAP_ARRAY_TYPE,
    CAP_BATCH_WRITES,
    CAP_CHECK_CONSTRAINTS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_GRAPH_TRAVERSAL,
    CAP_GROUP_BY,
    CAP_HAVING,
    CAP_HYBRID_SEARCH,
    CAP_INDEXES,
    CAP_JOINS,
    CAP_JSON,
    CAP_MATERIALIZED_VIEWS,
    CAP_MULTI_KEY_ATOMICITY,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_SCHEMA_EVOLUTION,
    CAP_SEARCH_QUERY_DSL,
    CAP_SECONDARY_INDEXES,
    CAP_SERVER_SIDE_SCRIPTING,
    CAP_STRONG_CONSISTENCY,
    CAP_SUBQUERIES,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_TTL,
    CAP_UNION,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_UPSERT,
    CAP_UPSERT_BY_KEY,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_SEARCH,
    CAP_VECTOR_TYPE,
    CAP_WINDOW_FUNCTIONS,
    Adapter,
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    EngineVersion,
    ResultColumn,
    ResultRow,
    ResultSet,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)
from oq_adapters.kit import (
    SqlDialect,
    build_report,
    compile_schema_sql,
    compile_select_sql,
    type_kind,
)

try:
    import clickhouse_connect
except ImportError:  # pragma: no cover
    clickhouse_connect = None

__all__ = ["ClickHouseAdapter", "ClickHouseConnection", "ClickHouseDialect"]


class ClickHouseDialect(SqlDialect):
    adapter_id = "clickhouse"
    engine = "ClickHouse"
    quote_open = "`"
    quote_close = "`"
    max_identifier_length = 255
    placeholder_style = "brace"
    supports_full_join = True
    supports_null_ordering = True
    supports_ilike = True  # case-insensitive LIKE via ilike function; not advertised as operator
    supports_returning = False
    supports_auto_increment = False
    supports_transactions = False

    def type_sql(self, type_obj: Any) -> str | None:
        kind = type_kind(type_obj)
        if kind in ("integer", "int"):
            return "Int32"
        if kind in ("bigint", "reference"):
            return "Int64"
        if kind == "float":
            return "Float32"
        if kind == "double":
            return "Float64"
        if kind == "decimal":
            precision = getattr(type_obj, "precision", None)
            scale = getattr(type_obj, "scale", None)
            if precision and scale is not None:
                return f"Decimal({min(int(precision), 76)},{int(scale)})"
            return "Decimal(38, 9)"
        if kind == "text":
            return "String"
        if kind == "boolean":
            return "Bool"
        if kind == "date":
            return "Date"
        if kind == "time":
            return "String"
        if kind in ("timestamp", "datetime"):
            return (
                "DateTime64(6, 'UTC')"
                if getattr(type_obj, "has_timezone", False)
                else "DateTime64(6)"
            )
        if kind == "duration":
            return "Int64"
        if kind == "uuid":
            return "UUID"
        if kind == "email":
            return "String"
        if kind == "url":
            return "String"
        if kind == "json":
            return "String"
        if kind == "binary":
            return "String"
        if kind == "geo":
            return "Point"
        if kind in ("list", "array"):
            return "Array(String)"
        if kind == "set":
            return "Array(String)"
        if kind == "map":
            return "Map(String, String)"
        if kind == "vector":
            dimension = getattr(type_obj, "dimension", None)
            return "Array(Float32)" if dimension else None
        if kind == "enum":
            return "String"
        return None

    def create_table_sql(
        self, entity: Entity, columns: list[str], primary_columns: list[str]
    ) -> str:
        order_by = (
            "(" + ", ".join(self.quote(column) for column in primary_columns) + ")"
            if primary_columns
            else "tuple()"
        )
        engine = entity.model_extra.get("engine") if getattr(entity, "model_extra", None) else None
        engine_sql = str(engine or "MergeTree")
        return (
            f"CREATE TABLE {self.quote(entity.name)} (\n  "
            + ",\n  ".join(columns)
            + f"\n) ENGINE = {engine_sql} ORDER BY {order_by}"
        )

    def bind_placeholder(self, index: int, hint: str, label: str) -> str:
        return f"{{{label}:{self.brace_type(hint)}}}"


CLICKHOUSE_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "Int32"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "Int64"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "Float32"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "Float64"),
    "decimal": TypeSupport(TypeSupportLevel.COMPATIBLE, "Decimal(P,S)", max_precision=76),
    "text": TypeSupport(
        TypeSupportLevel.EXACT, "String", notes="UTF-8 bytes; no length enforcement."
    ),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "Bool"),
    "date": TypeSupport(TypeSupportLevel.EXACT, "Date"),
    "time": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "String", notes="No time-of-day type; string by convention."
    ),
    "timestamp": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "DateTime64(6)",
        notes="Microsecond precision; optional time zone in the column type.",
    ),
    "datetime": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "DateTime64(6)", notes="No automatic zone conversion."
    ),
    "duration": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "Int64", notes="Microseconds by convention."
    ),
    "uuid": TypeSupport(TypeSupportLevel.EXACT, "UUID"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "String", notes="No native validation."),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "String", notes="No native URL type."),
    "json": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "String", notes="JSON is text unless the JSON type is enabled."
    ),
    "binary": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "String", notes="Arbitrary bytes; no separate BLOB type."
    ),
    "geo": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "Point", notes="Geo types exist but no general GEOGRAPHY."
    ),
    "list": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "Array(String)", notes="Element type narrowed by this adapter."
    ),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "Array(String)", notes="No set uniqueness."),
    "map": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "Map(String, String)",
        notes="Keys and values are strings in this mapping.",
    ),
    "vector": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "Array(Float32)", notes="No ANN index in this adapter."
    ),
    "reference": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "Int64", notes="No foreign-key enforcement."
    ),
}

CLICKHOUSE_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No general multi-statement transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(True),
    CAP_PARTIAL_INDEXES: Capability(
        False, "Data-skipping indexes exist but are not partial indexes."
    ),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(True, "JSON functions; JSON type is version-dependent."),
    CAP_ARRAY_TYPE: Capability(True),
    CAP_UUID_TYPE: Capability(True),
    CAP_GEO_TYPE: Capability(True, "Point/ring/polygon types."),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False, "Decimal precision up to 76."),
    CAP_CHECK_CONSTRAINTS: Capability(False, "Constraints are not enforced."),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(False),
    CAP_INDEXES: Capability(
        True, "Primary key sorting and skipping indexes, not B-tree uniqueness."
    ),
    CAP_FULL_TEXT_SEARCH: Capability(True, "Token/ngram indexes vary by version."),
    CAP_MATERIALIZED_VIEWS: Capability(True),
    CAP_PARTITIONING: Capability(True),
    CAP_REPLICATION: Capability(True, "ReplicatedMergeTree."),
    CAP_JOINS: Capability(True),
    CAP_GROUP_BY: Capability(True),
    CAP_HAVING: Capability(True),
    CAP_SUBQUERIES: Capability(False, "Outside the Phase 8 IR subset."),
    CAP_UNION: Capability(False, "Outside the Phase 8 IR subset."),
    CAP_WINDOW_FUNCTIONS: Capability(True),
    CAP_UPSERT: Capability(False, "Use ReplacingMergeTree or explicit deduplication."),
    CAP_TTL: Capability(True, "Table and column TTL."),
    CAP_SECONDARY_INDEXES: Capability(True, "Data-skipping indexes."),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "UDFs, dictionaries."),
    CAP_BATCH_WRITES: Capability(True),
    CAP_STRONG_CONSISTENCY: Capability(False, "Eventual visibility across replicas."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "ALTER TABLE."),
    CAP_UPSERT_BY_KEY: Capability(False),
    CAP_ANN_SEARCH: Capability(False),
    CAP_HYBRID_SEARCH: Capability(False),
}


class ClickHouseConnection(Connection):
    def __init__(self, config: ConnectionConfig, client: Any) -> None:
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
            self._client.command("SELECT 1")
            return True
        except Exception:
            return False

    @property
    def client(self) -> Any:
        return self._client

    def close(self) -> None:
        if not self._closed:
            close = getattr(self._client, "close", None)
            if close:
                close()
            self._closed = True


class ClickHouseAdapter(Adapter):
    data_model = "analytical"
    ir_subset = ("schema", "select")

    def __init__(self) -> None:
        self.dialect = ClickHouseDialect()

    @property
    def id(self) -> str:
        return "clickhouse"

    @property
    def display_name(self) -> str:
        return "ClickHouse"

    @property
    def package_version(self) -> str:
        from oq_adapters.clickhouse import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.clickhouse import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="ClickHouse",
            tested_versions=[],
            capabilities=CLICKHOUSE_CAPS,
            types=CLICKHOUSE_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "No transactions and no enforced primary/foreign/unique keys.",
                "CREATE TABLE requires an engine and ORDER BY; MergeTree is the default.",
                "Mutations (UPDATE/DELETE) are asynchronous and are not part of this adapter's IR subset.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "clickhouse":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a clickhouse config.")
        if clickhouse_connect is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "clickhouse-connect is not installed. Install oq-adapter-clickhouse[driver].",
            )
        if not config.host:
            raise AdapterError(AdapterErrorCode.CONNECTION, "ClickHouse requires a host.")
        try:
            client = clickhouse_connect.get_client(
                host=config.host,
                port=config.port or 8123,
                username=config.user or "default",
                password=secrets.get("password") or "",
                database=config.database or "default",
                secure=config.sslmode not in (None, "disable"),
                verify=config.sslmode in ("verify-ca", "verify-full"),
                ca_cert=config.sslrootcert,
                connect_timeout=int(config.options.get("connect_timeout", "10")),
            )
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return ClickHouseConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            raw = str(conn.client.command("SELECT version()"))
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="ClickHouse",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, ClickHouseConnection):
            with contextlib.suppress(Exception):
                connection.close()

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        conn = self._require(connection)
        database = conn.config.database or "default"
        try:
            rows = conn.client.query(
                """
                SELECT table, name, type, default_kind, default_expression, is_in_primary_key
                FROM system.columns
                WHERE database = {database:String}
                ORDER BY table, position
                """,
                parameters={"database": database},
            ).result_rows
            engines = conn.client.query(
                "SELECT name, engine FROM system.tables WHERE database = {database:String}",
                parameters={"database": database},
            ).result_rows
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        engine_map = {str(name): str(engine) for name, engine in engines}
        by_table: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            by_table.setdefault(str(row[0]), []).append(row)
        entities = []
        for table, table_rows in by_table.items():
            attributes = []
            primary = []
            for row in table_rows:
                _, name, native, default_kind, default_expression, in_pk = row
                _kind, type_obj = _clickhouse_type(str(native))
                attributes.append(
                    Attribute(
                        name=str(name),
                        type=type_obj,
                        nullable=str(native).startswith("Nullable("),
                        primary=bool(in_pk),
                        default=default_expression if default_kind else None,
                    )
                )
                if in_pk:
                    primary.append(str(name))
            entity = Entity(
                name=table,
                attributes=attributes,
                primary_key=primary,
                indexes=[],
            )
            engine = engine_map.get(table)
            if engine:
                entity = Entity(
                    name=table,
                    attributes=attributes,
                    primary_key=primary,
                    indexes=[],
                    engine=engine,
                )
            entities.append(entity)
        return SchemaDocument(entities=entities, enums=[], extensions=[])

    def compile_schema(
        self, schema: SchemaDocument, options: CompileOptions | None
    ) -> list[DDLArtifact]:
        result = compile_schema_sql(self.dialect, schema, options or CompileOptions())
        if not result.supported:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "ClickHouse cannot compile part of the schema.",
                diagnostics=[item.model_dump(mode="json") for item in result.diagnostics],
            )
        return result.artifacts

    def compile_query(
        self, query: QueryDocument, schema: SchemaDocument, options: CompileOptions | None = None
    ) -> PlanDocument:
        result = compile_select_sql(self.dialect, query, schema, options=options)
        parameters = [dict(item) for item in result.parameters]
        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="sql", text=result.sql, dialect="clickhouse", placeholders=parameters
            ),
            parameters=parameters,
            required_capabilities=[CAP_JOINS] if query.joins else [],
            safety=query.safety,
            result_columns=result.result_columns,
            engine_plan={
                "kind": "sql",
                "sql": result.sql,
                "parameter_order": [p["name"] for p in parameters],
            },
            diagnostics=result.diagnostics,
            supported=result.supported,
        )

    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> ResultSet:
        conn = self._require(connection)
        if not plan.supported or not plan.artifact.text:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Plan is unsupported and was not executed.",
                diagnostics=[item.model_dump(mode="json") for item in plan.diagnostics],
            )
        binds = {str(entry["name"]): params[str(entry["name"])] for entry in plan.parameters}
        try:
            result = conn.client.query(plan.artifact.text, parameters=binds)
        except Exception as exc:
            raise self._error(exc, "execute") from exc
        columns = [
            ResultColumn(str(name), None, True) for name in getattr(result, "column_names", [])
        ]
        rows = [
            ResultRow(tuple(_value(v) for v in row)) for row in getattr(result, "result_rows", [])
        ]
        return ResultSet(columns=columns, rows=rows, affected_rows=None, warnings=[])

    def begin(
        self, connection: Connection, options: TransactionOptions | None = None
    ) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "ClickHouse has no general multi-statement transactions.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "ClickHouse has no transactions.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> ClickHouseConnection:
        if not isinstance(connection, ClickHouseConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for ClickHouse.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "Authentication failed" in text or "Code: 516" in text:
            code = AdapterErrorCode.AUTH
        elif "Code: 62" in text or "Code: 60" in text or "Code: 47" in text:
            code = AdapterErrorCode.SYNTAX
        elif "Code: 241" in text or "Code: 159" in text or "Timeout" in text:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        elif "Code: 209" in text:
            code = AdapterErrorCode.CONSTRAINT
        return AdapterError(
            code,
            f"ClickHouse {operation} failed: {text}",
            retryable=retryable,
            cause=exc,
            redacted_cause=text,
        )


def _clickhouse_type(native: str) -> tuple[str, Type]:
    name = native.removeprefix("Nullable(").rstrip(")")
    if name in ("Int8", "Int16", "Int32", "UInt8", "UInt16", "UInt32"):
        return "integer", Type(kind="integer")
    if name in ("Int64", "UInt64", "Int128", "UInt128", "Int256", "UInt256"):
        return "bigint", Type(kind="bigint")
    if name in ("Float32",):
        return "float", Type(kind="float")
    if name in ("Float64",):
        return "double", Type(kind="double")
    if name.startswith("Decimal"):
        return "decimal", Type(kind="decimal")
    if name in ("String", "FixedString"):
        return "text", Type(kind="text")
    if name == "Bool":
        return "boolean", Type(kind="boolean")
    if name == "Date" or name.startswith("Date32"):
        return "date", Type(kind="date")
    if name.startswith("DateTime"):
        return "timestamp", Type(
            kind="timestamp", has_timezone="'UTC'" in name or "WITH TIME ZONE" in name
        )
    if name == "UUID":
        return "uuid", Type(kind="uuid")
    if name.startswith("Array"):
        return "list", Type(kind="list")
    if name.startswith("Map"):
        return "map", Type(kind="map")
    if name in ("Point", "Ring", "Polygon", "MultiPolygon"):
        return "geo", Type(kind="geo")
    return "text", Type(kind="text")


def _value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (Decimal, date, datetime, bytes)):
        return value
    return value
