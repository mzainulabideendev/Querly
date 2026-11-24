"""Microsoft SQL Server adapter for Querly.

Uses T-SQL and the ODBC driver through ``pyodbc``. The adapter maps SQL Server's
catalog (``sys.tables``, ``sys.columns``, ``sys.indexes``, ``sys.foreign_keys``)
rather than assuming INFORMATION_SCHEMA has every detail. Identity columns,
``UNIQUEIDENTIFIER``, ``DATETIMEOFFSET``, ``VARBINARY(MAX)``, and ``BIT`` all have
native mappings. ``OFFSET/FETCH`` requires an ORDER BY; the compiler inserts a
deterministic constant order when the IR does not provide one and emits a
warning, rather than silently returning an arbitrary page.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Mapping

from oq.ir import (
    Attribute,
    Constraint,
    Entity,
    Index,
    PlanDocument,
    QueryDocument,
    SchemaDocument,
    Type,
)
from oq.runtime.contract import (
    CAP_WINDOW_FUNCTIONS,
    CAP_UNION,
    CAP_SUBQUERIES,
    CAP_ARRAY_TYPE,
    CAP_BATCH_WRITES,
    CAP_CHECK_CONSTRAINTS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_GROUP_BY,
    CAP_HAVING,
    CAP_INDEXES,
    CAP_JOINS,
    CAP_JSON,
    CAP_MATERIALIZED_VIEWS,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_SECONDARY_INDEXES,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_UPSERT,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_TYPE,
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
    diagnostic,
    type_kind,
)

try:
    import pyodbc
except ImportError:  # pragma: no cover
    pyodbc = None

__all__ = ["SQLServerAdapter", "SQLServerConnection", "SQLServerDialect"]


class SQLServerDialect(SqlDialect):
    adapter_id = "sqlserver"
    engine = "Microsoft SQL Server"
    quote_open = "["
    quote_close = "]"
    max_identifier_length = 128
    placeholder_style = "qmark"
    supports_full_join = True
    supports_null_ordering = False
    supports_ilike = False
    supports_returning = False
    supports_auto_increment = True

    def type_sql(self, type_obj: Any) -> str | None:
        kind = type_kind(type_obj)
        if kind in ("integer", "int"):
            return "INT"
        if kind in ("bigint", "reference"):
            return "BIGINT"
        if kind == "float":
            return "REAL"
        if kind == "double":
            return "FLOAT"
        if kind == "decimal":
            precision = getattr(type_obj, "precision", None)
            scale = getattr(type_obj, "scale", None)
            if precision and scale is not None:
                return f"DECIMAL({min(int(precision), 38)},{min(int(scale), 38)})"
            return "DECIMAL(38,9)"
        if kind == "text":
            length = getattr(type_obj, "max_length", None)
            if length and length <= 4000:
                return f"NVARCHAR({length})"
            return "NVARCHAR(MAX)"
        if kind == "boolean":
            return "BIT"
        if kind == "date":
            return "DATE"
        if kind == "time":
            return "TIME(7)"
        if kind in ("timestamp", "datetime"):
            return "DATETIMEOFFSET(7)" if getattr(type_obj, "has_timezone", False) else "DATETIME2(7)"
        if kind == "duration":
            return "BIGINT"
        if kind == "uuid":
            return "UNIQUEIDENTIFIER"
        if kind == "email":
            return "NVARCHAR(320)"
        if kind == "url":
            return "NVARCHAR(2048)"
        if kind in ("json", "map", "list", "set"):
            return "NVARCHAR(MAX)"
        if kind == "binary":
            return "VARBINARY(MAX)"
        if kind == "geo":
            return "GEOGRAPHY"
        if kind == "vector":
            # SQL Server 2025 adds a VECTOR type, but it is not available on
            # every version and requires fixed dimensions.
            dimension = getattr(type_obj, "dimension", None)
            return f"VECTOR({int(dimension)})" if dimension else None
        if kind == "enum":
            return "NVARCHAR(255)"
        return None

    def auto_sql(self, attr: Any, type_sql: str) -> str | None:
        if type_kind(attr.type) not in ("integer", "int", "bigint"):
            return None
        return "IDENTITY(1,1)"

    def select_limit(
        self, sql: str, limit: int | None, offset: int | None, has_order_by: bool
    ) -> tuple[str, list]:
        if limit is None and offset is None:
            return sql, []
        if offset is None and limit is not None:
            return sql.replace("SELECT", f"SELECT TOP ({int(limit)})", 1), []
        diagnostics = []
        if not has_order_by:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.CAPABILITY,
                    "SQL Server OFFSET/FETCH requires ORDER BY; a constant order was added.",
                    feature="pagination",
                    severity="warning",
                )
            )
            sql += " ORDER BY (SELECT NULL)"
        if offset is not None:
            sql += f" OFFSET {int(offset)} ROWS"
        if limit is not None:
            sql += f" FETCH NEXT {int(limit)} ROWS ONLY"
        return sql, diagnostics


SQLSERVER_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "INT", range="-2147483648..2147483647"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "BIGINT", range="-2^63..2^63-1"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "REAL"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "FLOAT"),
    "decimal": TypeSupport(TypeSupportLevel.COMPATIBLE, "DECIMAL(38,s)", max_precision=38),
    "text": TypeSupport(TypeSupportLevel.EXACT, "NVARCHAR(MAX)", max_length=2147483647, notes="Unicode by default."),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "BIT"),
    "date": TypeSupport(TypeSupportLevel.EXACT, "DATE"),
    "time": TypeSupport(TypeSupportLevel.EXACT, "TIME(7)"),
    "timestamp": TypeSupport(TypeSupportLevel.EXACT, "DATETIMEOFFSET(7)", notes="Offset preserved."),
    "datetime": TypeSupport(TypeSupportLevel.COMPATIBLE, "DATETIME2(7)", notes="100 ns precision; no zone."),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "BIGINT", notes="Microseconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.EXACT, "UNIQUEIDENTIFIER"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "NVARCHAR(320)", notes="No native validation."),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "NVARCHAR(2048)", notes="No native URL type."),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "NVARCHAR(MAX)", notes="ISJSON check is not added automatically; use OPENJSON."),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "VARBINARY(MAX)"),
    "geo": TypeSupport(TypeSupportLevel.COMPATIBLE, "GEOGRAPHY", notes="Latitude/longitude order follows SQL Server's geography type."),
    "list": TypeSupport(TypeSupportLevel.APPROXIMATE, "NVARCHAR(MAX)", notes="JSON array text."),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "NVARCHAR(MAX)", notes="JSON array text; no set algebra."),
    "map": TypeSupport(TypeSupportLevel.APPROXIMATE, "NVARCHAR(MAX)", notes="JSON object text."),
    "vector": TypeSupport(TypeSupportLevel.COMPATIBLE, "VECTOR(n)", notes="Only SQL Server 2025+ with fixed dimension."),
    "reference": TypeSupport(TypeSupportLevel.EXACT, "BIGINT"),
}

SQLSERVER_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(True),
    CAP_TRANSACTIONAL_DDL: Capability(True, "Most DDL participates in transactions."),
    CAP_RETURNING: Capability(False, "OUTPUT clause is not the IR RETURNING contract."),
    CAP_ORDER_BY_NULLS: Capability(False, "No NULLS FIRST/LAST; use CASE expressions."),
    CAP_PARTIAL_INDEXES: Capability(True, "Filtered indexes with WHERE."),
    CAP_VECTOR_TYPE: Capability(False, "VECTOR type only on SQL Server 2025; not advertised."),
    CAP_JSON: Capability(True, "JSON stored as NVARCHAR; JSON functions available."),
    CAP_ARRAY_TYPE: Capability(False),
    CAP_UUID_TYPE: Capability(True),
    CAP_GEO_TYPE: Capability(True),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False, "Maximum precision 38."),
    CAP_CHECK_CONSTRAINTS: Capability(True),
    CAP_FOREIGN_KEYS: Capability(True),
    CAP_UNIQUE_CONSTRAINTS: Capability(True),
    CAP_INDEXES: Capability(True),
    CAP_FULL_TEXT_SEARCH: Capability(True, "Full-text catalogs must be installed."),
    CAP_MATERIALIZED_VIEWS: Capability(True, "Indexed views."),
    CAP_PARTITIONING: Capability(True),
    CAP_REPLICATION: Capability(True),
    CAP_JOINS: Capability(True),
    CAP_GROUP_BY: Capability(True),
    CAP_HAVING: Capability(True),
    CAP_SUBQUERIES: Capability(False, "Outside the Phase 8 IR subset."),
    CAP_UNION: Capability(False, "Outside the Phase 8 IR subset."),
    CAP_WINDOW_FUNCTIONS: Capability(True),
    CAP_UPSERT: Capability(True, "MERGE; not expressible in current IR."),
    CAP_TTL: Capability(False),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(True, "SQL Server graph tables use MATCH; not mapped by this adapter."),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "T-SQL procedures."),
    CAP_BATCH_WRITES: Capability(True),
    CAP_STRONG_CONSISTENCY: Capability(True),
    CAP_MULTI_KEY_ATOMICITY: Capability(True),
    CAP_SCHEMA_EVOLUTION: Capability(True),
    CAP_UPSERT_BY_KEY: Capability(True, "MERGE."),
    CAP_ANN_SEARCH: Capability(False),
    CAP_HYBRID_SEARCH: Capability(False),
}


class SQLServerConnection(Connection):
    def __init__(self, config: ConnectionConfig, conn: Any) -> None:
        self._config = config
        self._conn = conn
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    def ping(self) -> bool:
        try:
            self._conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    @property
    def raw_connection(self) -> Any:
        return self._conn


class SQLServerTransaction(Transaction):
    def __init__(self, connection: SQLServerConnection, options: TransactionOptions) -> None:
        self._connection = connection
        self._options = options
        self._active = True

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def is_active(self) -> bool:
        return self._active and not self._connection.is_closed

    def commit(self) -> None:
        if self._active:
            self._connection.raw_connection.commit()
            self._active = False

    def rollback(self) -> None:
        if self._active:
            self._connection.raw_connection.rollback()
            self._active = False


class SQLServerAdapter(Adapter):
    data_model = "relational"
    ir_subset = ("schema", "select")

    def __init__(self) -> None:
        self.dialect = SQLServerDialect()

    @property
    def id(self) -> str:
        return "sqlserver"

    @property
    def display_name(self) -> str:
        return "Microsoft SQL Server"

    @property
    def package_version(self) -> str:
        from oq_adapters.sqlserver import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.sqlserver import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Microsoft SQL Server",
            tested_versions=[],
            capabilities=SQLSERVER_CAPS,
            types=SQLSERVER_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "RETURNING is not mapped to OUTPUT.",
                "JSON is textual unless the caller adds ISJSON constraints.",
                "OFFSET/FETCH requires ORDER BY; the compiler emits a warning and a constant order.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "sqlserver":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a sqlserver config.")
        if pyodbc is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "pyodbc is not installed. Install oq-adapter-sqlserver[driver] and an ODBC driver.",
            )
        driver = config.options.get("driver", "ODBC Driver 18 for SQL Server")
        server = config.host or "localhost"
        if config.port:
            server = f"{server},{config.port}"
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={server}",
            f"DATABASE={config.database or 'master'}",
        ]
        if config.user:
            parts.append(f"UID={config.user}")
            parts.append(f"PWD={secrets.get('password') or ''}")
        else:
            parts.append("Trusted_Connection=yes")
        encrypt = config.options.get("encrypt", "yes")
        parts.append(f"Encrypt={encrypt}")
        parts.append(f"TrustServerCertificate={config.options.get('trust_server_certificate', 'no')}")
        parts.append(f"Connection Timeout={config.options.get('connect_timeout', '10')}")
        try:
            raw = pyodbc.connect(";".join(parts), autocommit=False)
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return SQLServerConnection(config, raw)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        cursor = conn.execute("SELECT @@VERSION")
        try:
            raw = str(cursor.fetchone()[0])
        finally:
            cursor.close()
        import re

        match = re.search(r"Microsoft SQL Server (\d{4})", raw)
        major = int(match.group(1)) if match else 0
        return EngineVersion(product="Microsoft SQL Server", version=raw.splitlines()[0], major=major, minor=0, patch=0)

    def close(self, connection: Connection) -> None:
        if isinstance(connection, SQLServerConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        conn = self._require(connection)
        rows = self._fetch(
            conn,
            """
            SELECT t.name, c.name, ty.name, c.max_length, c.precision, c.scale,
                   c.is_nullable, c.is_identity, c.column_id,
                   OBJECT_DEFINITION(c.default_object_id)
            FROM sys.tables t
            JOIN sys.columns c ON c.object_id = t.object_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE t.is_ms_shipped = 0
            ORDER BY t.name, c.column_id
            """,
        )
        pk_rows = self._fetch(
            conn,
            """
            SELECT t.name, c.name
            FROM sys.indexes i
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.is_primary_key = 1 AND t.is_ms_shipped = 0
            ORDER BY t.name, ic.key_ordinal
            """,
        )
        index_rows = self._fetch(
            conn,
            """
            SELECT t.name, i.name, i.is_unique, i.type_desc, c.name, ic.key_ordinal
            FROM sys.indexes i
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.is_primary_key = 0 AND i.name IS NOT NULL AND t.is_ms_shipped = 0
            ORDER BY t.name, i.name, ic.key_ordinal
            """,
        )
        fk_rows = self._fetch(
            conn,
            """
            SELECT fk.name, tp.name, cp.name, tr.name, cr.name,
                   fk.delete_referential_action_desc, fk.update_referential_action_desc
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id
            JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id
            JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id
            JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id
            ORDER BY fk.name, fkc.constraint_column_id
            """,
        )
        by_table: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            by_table.setdefault(str(row[0]), []).append(row)
        pk_map: dict[str, list[str]] = {}
        for table, column in pk_rows:
            pk_map.setdefault(str(table), []).append(str(column))
        entities: list[Entity] = []
        for table, table_rows in by_table.items():
            attributes = []
            for row in table_rows:
                _, name, native, max_length, precision, scale, nullable, identity, _, default = row
                kind, type_obj = _sqlserver_type(str(native), max_length, precision, scale)
                attributes.append(
                    Attribute(
                        name=str(name),
                        type=type_obj,
                        nullable=bool(nullable),
                        primary=str(name) in pk_map.get(table, []),
                        auto=bool(identity),
                        default=default,
                    )
                )
            entities.append(
                Entity(
                    name=table,
                    attributes=attributes,
                    primary_key=pk_map.get(table, []),
                    indexes=_sqlserver_indexes(table, index_rows),
                    constraints=_sqlserver_fks(table, fk_rows),
                )
            )
        return SchemaDocument(entities=entities, enums=[], extensions=[])

    def compile_schema(
        self, schema: SchemaDocument, options: CompileOptions | None
    ) -> list[DDLArtifact]:
        result = compile_schema_sql(self.dialect, schema, options or CompileOptions())
        if not result.supported:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "SQL Server cannot compile part of the schema.",
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
            artifact=__import__("oq.ir", fromlist=["Artifact"]).Artifact(
                kind="sql",
                text=result.sql,
                dialect="tsql",
                placeholders=parameters,
            ),
            parameters=parameters,
            required_capabilities=[CAP_JOINS] if query.joins else [],
            safety=query.safety,
            result_columns=result.result_columns,
            engine_plan={"kind": "sql", "sql": result.sql, "parameter_order": [p["name"] for p in parameters]},
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
        values = [params[str(entry["name"])] for entry in plan.parameters]
        try:
            cursor = conn.execute(plan.artifact.text, tuple(values))
        except Exception as exc:
            raise self._error(exc, "execute") from exc
        try:
            columns = (
                [ResultColumn(str(c[0]), None, True) for c in cursor.description]
                if cursor.description
                else []
            )
            rows = [ResultRow(tuple(_value(v) for v in row)) for row in cursor.fetchall()]
            affected = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None
        finally:
            cursor.close()
        return ResultSet(columns=columns, rows=rows, affected_rows=affected, warnings=[])

    def begin(
        self, connection: Connection, options: TransactionOptions | None = None
    ) -> Transaction:
        conn = self._require(connection)
        opts = options or TransactionOptions()
        levels = {
            "read_uncommitted": "READ UNCOMMITTED",
            "read_committed": "READ COMMITTED",
            "repeatable_read": "REPEATABLE READ",
            "serializable": "SERIALIZABLE",
        }
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {levels[opts.isolation.value]}")
        return SQLServerTransaction(conn, opts)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, SQLServerTransaction):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong transaction type.")
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if isinstance(transaction, SQLServerTransaction):
            try:
                transaction.rollback()
            except Exception:
                pass

    def _require(self, connection: Connection) -> SQLServerConnection:
        if not isinstance(connection, SQLServerConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for SQL Server.")
        return connection

    @staticmethod
    def _fetch(conn: SQLServerConnection, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        cursor = conn.execute(sql, params)
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        sqlstate = str(getattr(exc, "sqlstate", "") or "")
        number = getattr(exc, "args", [None])[0]
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if sqlstate in ("28000", "42000") and number in (18456, 18452):
            code = AdapterErrorCode.AUTH
        elif sqlstate == "23000" or number in (2627, 2601, 547):
            code = AdapterErrorCode.CONSTRAINT
        elif sqlstate == "HYT00" or number == 1222:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        elif number == 102:
            code = AdapterErrorCode.SYNTAX
        elif sqlstate in ("08S01", "08001") or number in (4060, 10054, 10060):
            retryable = True
        return AdapterError(code, f"SQL Server {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _sqlserver_type(native: str, max_length: Any, precision: Any, scale: Any) -> tuple[str, Type]:
    name = native.lower()
    if name in ("int", "smallint", "tinyint"):
        return "integer", Type(kind="integer")
    if name == "bigint":
        return "bigint", Type(kind="bigint")
    if name == "real":
        return "float", Type(kind="float")
    if name == "float":
        return "double", Type(kind="double")
    if name in ("decimal", "numeric", "money", "smallmoney"):
        return "decimal", Type(
            kind="decimal",
            precision=int(precision) if precision else None,
            scale=int(scale) if scale is not None else None,
        )
    if name in ("char", "varchar", "nchar", "nvarchar", "text", "ntext"):
        length = int(max_length) if max_length and int(max_length) > -1 else None
        return "text", Type(kind="text", max_length=length)
    if name == "bit":
        return "boolean", Type(kind="boolean")
    if name == "date":
        return "date", Type(kind="date")
    if name == "time":
        return "time", Type(kind="time")
    if name == "datetimeoffset":
        return "timestamp", Type(kind="timestamp", has_timezone=True)
    if name in ("datetime", "datetime2", "smalldatetime"):
        return "datetime", Type(kind="datetime")
    if name == "uniqueidentifier":
        return "uuid", Type(kind="uuid")
    if name in ("binary", "varbinary", "image", "timestamp", "rowversion"):
        return "binary", Type(kind="binary")
    if name in ("geography", "geometry"):
        return "geo", Type(kind="geo")
    if name == "xml":
        return "json", Type(kind="json")
    return "text", Type(kind="text")


def _sqlserver_indexes(table: str, rows: list[tuple[Any, ...]]) -> list[Index]:
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        if str(row[0]) == table:
            grouped.setdefault(str(row[1]), []).append(row)
    result = []
    for name, items in grouped.items():
        ordered = sorted(items, key=lambda item: int(item[5] or 0))
        result.append(
            Index(
                name=name,
                columns=[str(item[4]) for item in ordered],
                unique=bool(ordered[0][2]),
                kind="btree",
                options={"native_type": str(ordered[0][3])},
            )
        )
    return result


def _sqlserver_fks(table: str, rows: list[tuple[Any, ...]]) -> list[Constraint]:
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        if str(row[1]) == table:
            grouped.setdefault(str(row[0]), []).append(row)
    result = []
    for name, items in grouped.items():
        result.append(
            Constraint(
                kind="foreign_key",
                columns=[str(item[2]) for item in items],
                reference=str(items[0][3]),
                referenced_columns=[str(item[4]) for item in items],
                on_delete=str(items[0][5]).lower(),
                on_update=str(items[0][6]).lower(),
            )
        )
    return result


def _value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (Decimal, date, datetime, time, bytes)):
        return value
    return value
