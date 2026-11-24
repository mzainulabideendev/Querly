"""IBM Db2 LUW adapter for Querly.

Uses Db2's SQL dialect and SYSCAT catalog through ``ibm_db``. Db2 differs from
the other relational targets in its identity syntax
(``GENERATED ALWAYS AS IDENTITY``), ``DECIMAL(31)`` limit, ``CLOB`` text,
``SMALLINT`` boolean convention, and ``FETCH FIRST`` pagination. These are
reported rather than hidden behind a generic SQL profile.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Mapping

from oq.ir import Artifact, Attribute, Constraint, Entity, Index, PlanDocument, QueryDocument, SchemaDocument, Type
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
from oq_adapters.kit import SqlDialect, build_report, compile_schema_sql, compile_select_sql, type_kind

try:
    import ibm_db
    import ibm_db_dbi
except ImportError:  # pragma: no cover
    ibm_db = None
    ibm_db_dbi = None

__all__ = ["Db2Adapter", "Db2Connection", "Db2Dialect"]


class Db2Dialect(SqlDialect):
    adapter_id = "db2"
    engine = "IBM Db2"
    quote_open = '"'
    quote_close = '"'
    max_identifier_length = 128
    placeholder_style = "qmark"
    supports_full_join = True
    supports_null_ordering = True
    supports_ilike = False
    supports_returning = False
    supports_auto_increment = True

    def type_sql(self, type_obj: Any) -> str | None:
        kind = type_kind(type_obj)
        if kind in ("integer", "int"):
            return "INTEGER"
        if kind in ("bigint", "reference"):
            return "BIGINT"
        if kind == "float":
            return "REAL"
        if kind == "double":
            return "DOUBLE"
        if kind == "decimal":
            precision = getattr(type_obj, "precision", None)
            scale = getattr(type_obj, "scale", None)
            if precision and scale is not None:
                return f"DECIMAL({min(int(precision), 31)},{min(int(scale), 31)})"
            return "DECIMAL(31,9)"
        if kind == "text":
            length = getattr(type_obj, "max_length", None)
            if length and length <= 32672:
                return f"VARCHAR({length})"
            return "CLOB"
        if kind == "boolean":
            return "SMALLINT"
        if kind == "date":
            return "DATE"
        if kind == "time":
            return "TIME"
        if kind in ("timestamp", "datetime"):
            return "TIMESTAMP(6)"
        if kind == "duration":
            return "BIGINT"
        if kind == "uuid":
            return "CHAR(36)"
        if kind == "email":
            return "VARCHAR(320)"
        if kind == "url":
            return "VARCHAR(2048)"
        if kind in ("json", "map", "list", "set"):
            return "CLOB"
        if kind == "binary":
            return "BLOB"
        if kind == "geo":
            return "ST_GEOMETRY"
        if kind == "vector":
            return None
        if kind == "enum":
            return "VARCHAR(255)"
        return None

    def auto_sql(self, attr: Any, type_sql: str) -> str | None:
        if type_kind(attr.type) not in ("integer", "int", "bigint"):
            return None
        return "GENERATED ALWAYS AS IDENTITY"

    def select_limit(
        self, sql: str, limit: int | None, offset: int | None, has_order_by: bool
    ) -> tuple[str, list]:
        if limit is None and offset is None:
            return sql, []
        if offset is not None:
            sql += f" OFFSET {int(offset)} ROWS"
        if limit is not None:
            sql += f" FETCH NEXT {int(limit)} ROWS ONLY"
        return sql, []


DB2_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "INTEGER", range="-2147483648..2147483647"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "BIGINT"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "REAL"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "DOUBLE"),
    "decimal": TypeSupport(TypeSupportLevel.COMPATIBLE, "DECIMAL(31,s)", max_precision=31),
    "text": TypeSupport(TypeSupportLevel.EXACT, "CLOB"),
    "boolean": TypeSupport(TypeSupportLevel.COMPATIBLE, "SMALLINT", notes="0/1 convention; BOOLEAN exists only on newer releases."),
    "date": TypeSupport(TypeSupportLevel.EXACT, "DATE"),
    "time": TypeSupport(TypeSupportLevel.EXACT, "TIME"),
    "timestamp": TypeSupport(TypeSupportLevel.COMPATIBLE, "TIMESTAMP(6)", notes="No zone stored; UTC by convention."),
    "datetime": TypeSupport(TypeSupportLevel.COMPATIBLE, "TIMESTAMP(6)"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "BIGINT", notes="Microseconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.COMPATIBLE, "CHAR(36)", notes="No native UUID type."),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR(320)", notes="No native validation."),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR(2048)", notes="No native URL type."),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "CLOB", notes="Db2 has binary JSON on some releases; CLOB is portable."),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "BLOB"),
    "geo": TypeSupport(TypeSupportLevel.COMPATIBLE, "ST_GEOMETRY", notes="Spatial extender required."),
    "list": TypeSupport(TypeSupportLevel.APPROXIMATE, "CLOB", notes="JSON array text."),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "CLOB", notes="JSON array text."),
    "map": TypeSupport(TypeSupportLevel.APPROXIMATE, "CLOB", notes="JSON object text."),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native vector column in Db2 LUW."),
    "reference": TypeSupport(TypeSupportLevel.EXACT, "BIGINT"),
}

DB2_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(True),
    CAP_TRANSACTIONAL_DDL: Capability(True, "Db2 LUW DDL generally participates in transactions; verify per statement."),
    CAP_RETURNING: Capability(False, "Db2 uses SELECT FROM FINAL TABLE, not a RETURNING clause."),
    CAP_ORDER_BY_NULLS: Capability(True),
    CAP_PARTIAL_INDEXES: Capability(False, "No filtered indexes."),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(True, "JSON functions available."),
    CAP_ARRAY_TYPE: Capability(False),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(True, "Spatial extender."),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False, "Maximum precision 31."),
    CAP_CHECK_CONSTRAINTS: Capability(True),
    CAP_FOREIGN_KEYS: Capability(True),
    CAP_UNIQUE_CONSTRAINTS: Capability(True),
    CAP_INDEXES: Capability(True),
    CAP_FULL_TEXT_SEARCH: Capability(True, "Db2 Text Search; requires configuration."),
    CAP_MATERIALIZED_VIEWS: Capability(True, "Materialized query tables."),
    CAP_PARTITIONING: Capability(True),
    CAP_REPLICATION: Capability(True),
    CAP_JOINS: Capability(True),
    CAP_GROUP_BY: Capability(True),
    CAP_HAVING: Capability(True),
    CAP_SUBQUERIES: Capability(False, "Outside the Phase 8 IR subset."),
    CAP_UNION: Capability(False, "Outside the Phase 8 IR subset."),
    CAP_WINDOW_FUNCTIONS: Capability(True),
    CAP_UPSERT: Capability(True, "MERGE."),
    CAP_TTL: Capability(False),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "SQL PL."),
    CAP_BATCH_WRITES: Capability(True),
    CAP_STRONG_CONSISTENCY: Capability(True),
    CAP_MULTI_KEY_ATOMICITY: Capability(True),
    CAP_SCHEMA_EVOLUTION: Capability(True),
    CAP_UPSERT_BY_KEY: Capability(True, "MERGE."),
    CAP_ANN_SEARCH: Capability(False),
    CAP_HYBRID_SEARCH: Capability(False),
}


class Db2Connection(Connection):
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
            cursor = self._conn.cursor()
            cursor.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1")
            cursor.close()
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


class Db2Transaction(Transaction):
    def __init__(self, connection: Db2Connection, options: TransactionOptions) -> None:
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


class Db2Adapter(Adapter):
    data_model = "relational"
    ir_subset = ("schema", "select")

    def __init__(self) -> None:
        self.dialect = Db2Dialect()

    @property
    def id(self) -> str:
        return "db2"

    @property
    def display_name(self) -> str:
        return "IBM Db2"

    @property
    def package_version(self) -> str:
        from oq_adapters.db2 import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.db2 import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="IBM Db2",
            tested_versions=[],
            capabilities=DB2_CAPS,
            types=DB2_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "BOOLEAN is represented as SMALLINT for portability.",
                "No native vector column.",
                "DDL transactionality varies by statement; verify before relying on rollback.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "db2":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a db2 config.")
        if ibm_db is None or ibm_db_dbi is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "ibm_db is not installed. Install oq-adapter-db2[driver].",
            )
        database = config.database or config.options.get("database")
        host = config.host or "localhost"
        port = config.port or 50000
        if not database:
            raise AdapterError(AdapterErrorCode.CONNECTION, "Db2 requires a database name.")
        conn_str = (
            f"DATABASE={database};HOSTNAME={host};PORT={port};PROTOCOL=TCPIP;"
            f"UID={config.user or ''};PWD={secrets.get('password') or ''};"
            f"Security={config.options.get('security', 'SSL')};"
        )
        try:
            raw = ibm_db_dbi.connect(conn_str, "", "")
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return Db2Connection(config, raw)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        cursor = conn.execute("SELECT SERVICE_LEVEL FROM TABLE(SYSPROC.ENV_GET_INST_INFO())")
        try:
            row = cursor.fetchone()
            raw = str(row[0]) if row else "Db2"
        finally:
            cursor.close()
        import re

        match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
        return EngineVersion(
            product="IBM Db2",
            version=raw,
            major=int(match.group(1)) if match else 0,
            minor=int(match.group(2)) if match else 0,
            patch=int(match.group(3)) if match else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, Db2Connection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        conn = self._require(connection)
        schema = (conn.config.user or "").upper() or "PUBLIC"
        tables = self._fetch(
            conn,
            "SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE = 'T' ORDER BY TABNAME",
            (schema,),
        )
        columns = self._fetch(
            conn,
            """
            SELECT TABNAME, COLNAME, TYPENAME, LENGTH, SCALE, NULLS, DEFAULT, IDENTITY
            FROM SYSCAT.COLUMNS
            WHERE TABSCHEMA = ? AND TABNAME IN (SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TYPE = 'T')
            ORDER BY TABNAME, COLNO
            """,
            (schema, schema),
        )
        keys = self._fetch(
            conn,
            """
            SELECT t.TABNAME, t.CONSTNAME, t.TYPE, c.COLNAME, c.COLSEQ,
                   r.REFTABNAME, rc.COLNAME
            FROM SYSCAT.TABCONST t
            JOIN SYSCAT.KEYCOLUSE c ON c.TABSCHEMA = t.TABSCHEMA AND c.TABNAME = t.TABNAME AND c.CONSTNAME = t.CONSTNAME
            LEFT JOIN SYSCAT.REFERENCES r ON r.TABSCHEMA = t.TABSCHEMA AND r.CONSTNAME = t.CONSTNAME
            LEFT JOIN SYSCAT.KEYCOLUSE rc ON rc.TABSCHEMA = r.REFTABSCHEMA AND rc.TABNAME = r.REFTABNAME
                                       AND rc.COLSEQ = c.COLSEQ
            WHERE t.TABSCHEMA = ? AND t.TYPE IN ('P','U','F')
            ORDER BY t.TABNAME, t.CONSTNAME, c.COLSEQ
            """,
            (schema,),
        )
        indexes = self._fetch(
            conn,
            """
            SELECT i.TABNAME, i.INDNAME, i.UNIQUERULE, c.COLNAME, c.COLSEQ, i.INDEXTYPE
            FROM SYSCAT.INDEXES i
            JOIN SYSCAT.INDEXCOLUSE c ON c.INDSCHEMA = i.INDSCHEMA AND c.INDNAME = i.INDNAME
            WHERE i.TABSCHEMA = ? AND i.SYSTEM_INDEX = 'N'
            ORDER BY i.TABNAME, i.INDNAME, c.COLSEQ
            """,
            (schema,),
        )
        by_table: dict[str, list[tuple[Any, ...]]] = {}
        for row in columns:
            by_table.setdefault(str(row[0]), []).append(row)
        pk_map: dict[str, list[str]] = {}
        for row in keys:
            if str(row[2]) == "P":
                pk_map.setdefault(str(row[0]), []).append(str(row[3]))
        entities: list[Entity] = []
        for (table,) in tables:
            attributes = []
            for row in by_table.get(str(table), []):
                _, name, native, length, scale, nulls, default, identity = row
                kind, type_obj = _db2_type(str(native), length, scale)
                attributes.append(
                    Attribute(
                        name=str(name),
                        type=type_obj,
                        nullable=str(nulls).upper() == "Y",
                        primary=str(name) in pk_map.get(str(table), []),
                        auto=str(identity).upper() == "Y",
                        default=default,
                    )
                )
            entities.append(
                Entity(
                    name=str(table),
                    attributes=attributes,
                    primary_key=pk_map.get(str(table), []),
                    indexes=_db2_indexes(str(table), indexes),
                    constraints=_db2_constraints(str(table), keys),
                )
            )
        return SchemaDocument(entities=entities, enums=[], extensions=[])

    def compile_schema(self, schema: SchemaDocument, options: CompileOptions | None) -> list[DDLArtifact]:
        result = compile_schema_sql(self.dialect, schema, options or CompileOptions())
        if not result.supported:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Db2 cannot compile part of the schema.",
                diagnostics=[item.model_dump(mode="json") for item in result.diagnostics],
            )
        return result.artifacts

    def compile_query(self, query: QueryDocument, schema: SchemaDocument, options: CompileOptions | None = None) -> PlanDocument:
        result = compile_select_sql(self.dialect, query, schema, options=options)
        parameters = [dict(item) for item in result.parameters]
        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="sql", text=result.sql, dialect="db2", placeholders=parameters),
            parameters=parameters,
            required_capabilities=[CAP_JOINS] if query.joins else [],
            safety=query.safety,
            result_columns=result.result_columns,
            engine_plan={"kind": "sql", "sql": result.sql, "parameter_order": [p["name"] for p in parameters]},
            diagnostics=result.diagnostics,
            supported=result.supported,
        )

    def execute(self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]) -> ResultSet:
        conn = self._require(connection)
        if not plan.supported or not plan.artifact.text:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Plan is unsupported and was not executed.",
                diagnostics=[item.model_dump(mode="json") for item in plan.diagnostics],
            )
        values = tuple(params[str(entry["name"])] for entry in plan.parameters)
        try:
            cursor = conn.execute(plan.artifact.text, values)
        except Exception as exc:
            raise self._error(exc, "execute") from exc
        try:
            columns = [ResultColumn(str(c[0]), None, True) for c in cursor.description] if cursor.description else []
            rows = [ResultRow(tuple(_value(v) for v in row)) for row in cursor.fetchall()]
            affected = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None
        finally:
            cursor.close()
        return ResultSet(columns=columns, rows=rows, affected_rows=affected, warnings=[])

    def begin(self, connection: Connection, options: TransactionOptions | None = None) -> Transaction:
        conn = self._require(connection)
        opts = options or TransactionOptions()
        levels = {
            "read_uncommitted": "UR",
            "read_committed": "CS",
            "repeatable_read": "RS",
            "serializable": "RR",
        }
        conn.execute(f"SET CURRENT ISOLATION {levels[opts.isolation.value]}")
        return Db2Transaction(conn, opts)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, Db2Transaction):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong transaction type.")
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if isinstance(transaction, Db2Transaction):
            try:
                transaction.rollback()
            except Exception:
                pass

    def _require(self, connection: Connection) -> Db2Connection:
        if not isinstance(connection, Db2Connection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Db2.")
        return connection

    @staticmethod
    def _fetch(conn: Db2Connection, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        cursor = conn.execute(sql, params)
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        message = str(exc)
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "-803" in message:
            code = AdapterErrorCode.CONSTRAINT
        elif "-530" in message or "-545" in message:
            code = AdapterErrorCode.CONSTRAINT
        elif "-104" in message or "-007" in message or "-206" in message:
            code = AdapterErrorCode.SYNTAX
        elif "-30082" in message or "-1060" in message:
            code = AdapterErrorCode.AUTH
        elif "-952" in message or "-1224" in message or "-1336" in message:
            retryable = True
        return AdapterError(code, f"Db2 {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _db2_type(native: str, length: Any, scale: Any) -> tuple[str, Type]:
    name = native.upper()
    if name in ("INTEGER", "INT", "SMALLINT"):
        return "integer", Type(kind="integer")
    if name == "BIGINT":
        return "bigint", Type(kind="bigint")
    if name == "REAL":
        return "float", Type(kind="float")
    if name in ("DOUBLE", "DECFLOAT"):
        return "double", Type(kind="double")
    if name in ("DECIMAL", "NUMERIC"):
        return "decimal", Type(kind="decimal", scale=int(scale) if scale is not None else None)
    if name in ("VARCHAR", "CHAR", "CLOB", "LONG VARCHAR", "GRAPHIC", "VARGRAPHIC", "DBCLOB"):
        return "text", Type(kind="text", max_length=int(length) if length and name in ("VARCHAR", "CHAR") else None)
    if name == "DATE":
        return "date", Type(kind="date")
    if name == "TIME":
        return "time", Type(kind="time")
    if name == "TIMESTAMP":
        return "datetime", Type(kind="datetime")
    if name in ("BLOB", "BINARY", "VARBINARY"):
        return "binary", Type(kind="binary")
    if name in ("ST_GEOMETRY", "GEOGRAPHY"):
        return "geo", Type(kind="geo")
    if name == "BOOLEAN":
        return "boolean", Type(kind="boolean")
    return "text", Type(kind="text")


def _db2_indexes(table: str, rows: list[tuple[Any, ...]]) -> list[Index]:
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        if str(row[0]) == table:
            grouped.setdefault(str(row[1]), []).append(row)
    return [
        Index(
            name=name,
            columns=[str(item[3]) for item in sorted(items, key=lambda item: int(item[4] or 0))],
            unique=str(items[0][2]).upper() == "U",
            kind="btree",
            options={"native_type": str(items[0][5])},
        )
        for name, items in grouped.items()
    ]


def _db2_constraints(table: str, rows: list[tuple[Any, ...]]) -> list[Constraint]:
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        if str(row[0]) == table and str(row[2]) == "F":
            grouped.setdefault(str(row[1]), []).append(row)
    return [
        Constraint(
            kind="foreign_key",
            columns=[str(item[3]) for item in items],
            reference=str(items[0][5]),
            referenced_columns=[str(item[6]) for item in items if item[6] is not None],
        )
        for items in grouped.values()
    ]


def _value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (Decimal, date, datetime, time, bytes)):
        return value
    return value
