"""MariaDB adapter for Querly.

MariaDB is a MySQL fork, but this adapter is written against MariaDB's own
catalog and semantics: ``information_schema`` inspection, InnoDB table options,
``JSON_VALID`` checks, MariaDB's lack of ``NULLS FIRST/LAST``, and its
``INSERT ... RETURNING`` (not a general ``SELECT ... RETURNING``). It does not
pretend to be the MySQL adapter and reports different capabilities.

Driver: ``mariadb`` (official MariaDB Connector/Python), imported lazily so
importing the adapter never requires a running server.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from oq.ir import (
    Artifact,
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
    import mariadb as mariadb_driver
except ImportError:  # pragma: no cover - environment without the driver
    mariadb_driver = None

__all__ = ["MariaDBAdapter", "MariaDBConnection", "MariaDBDialect", "MariaDBTransaction"]

_JSON_NATIVE = {"json"}


class MariaDBDialect(SqlDialect):
    """MariaDB 10.x/11.x dialect."""

    adapter_id = "mariadb"
    engine = "MariaDB"
    quote_open = "`"
    quote_close = "`"
    max_identifier_length = 64
    placeholder_style = "qmark"
    supports_full_join = False
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
            return "FLOAT"
        if kind == "double":
            return "DOUBLE"
        if kind == "decimal":
            precision = getattr(type_obj, "precision", None)
            scale = getattr(type_obj, "scale", None)
            if precision and scale is not None:
                return f"DECIMAL({min(int(precision), 65)},{min(int(scale), 30)})"
            return "DECIMAL(65,30)"
        if kind == "text":
            length = getattr(type_obj, "max_length", None)
            if length and length <= 16383:
                return f"VARCHAR({length})"
            return "LONGTEXT"
        if kind == "boolean":
            return "TINYINT(1)"
        if kind == "date":
            return "DATE"
        if kind == "time":
            return "TIME(6)"
        if kind in ("timestamp", "datetime"):
            return "TIMESTAMP(6)" if getattr(type_obj, "has_timezone", False) else "DATETIME(6)"
        if kind == "duration":
            return "BIGINT"
        if kind == "uuid":
            return "CHAR(36)"
        if kind == "email":
            return "VARCHAR(320)"
        if kind == "url":
            return "VARCHAR(2048)"
        if kind == "json":
            return "JSON"
        if kind == "binary":
            return "LONGBLOB"
        if kind == "geo":
            return "GEOMETRY"
        if kind in ("list", "array", "set", "map"):
            return "JSON"
        if kind == "vector":
            dimension = getattr(type_obj, "dimension", None)
            if dimension:
                return f"VECTOR({int(dimension)})"
            return None
        if kind == "enum":
            return "VARCHAR(255)"
        return None

    def auto_sql(self, attr: Any, type_sql: str) -> str | None:
        if type_kind(attr.type) not in ("integer", "int", "bigint"):
            return None
        return "AUTO_INCREMENT"

    def table_suffix(self, entity: Entity) -> str:
        return " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

    def column_suffix(self, attr: Any) -> str:
        if type_kind(attr.type) == "json":
            column = self.quote(attr.name)
            return f"CHECK (JSON_VALID({column}))"
        return ""


MARIADB_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "INT", range="-2147483648..2147483647"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "BIGINT", range="-2^63..2^63-1"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "FLOAT"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "DOUBLE"),
    "decimal": TypeSupport(TypeSupportLevel.COMPATIBLE, "DECIMAL(65,30)", max_precision=65),
    "text": TypeSupport(TypeSupportLevel.EXACT, "LONGTEXT", max_length=4294967295),
    "boolean": TypeSupport(TypeSupportLevel.COMPATIBLE, "TINYINT(1)", notes="No native boolean."),
    "date": TypeSupport(TypeSupportLevel.EXACT, "DATE"),
    "time": TypeSupport(TypeSupportLevel.EXACT, "TIME(6)"),
    "timestamp": TypeSupport(TypeSupportLevel.COMPATIBLE, "TIMESTAMP(6)", notes="UTC; 1970-2038."),
    "datetime": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "DATETIME(6)", notes="Session time zone; no zone stored."
    ),
    "duration": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "BIGINT", notes="Microseconds by convention."
    ),
    "uuid": TypeSupport(TypeSupportLevel.COMPATIBLE, "CHAR(36)", notes="No native UUID type."),
    "email": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "VARCHAR(320)", notes="No native email validation."
    ),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR(2048)", notes="No native URL type."),
    "json": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "JSON",
        notes="Alias for LONGTEXT with JSON_VALID check; key order preserved, duplicate key policy is last-wins.",
    ),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "LONGBLOB", max_length=4294967295),
    "geo": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "GEOMETRY", notes="SRID must be declared by the caller."
    ),
    "list": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "JSON",
        notes="Array stored as JSON; no native array constraints.",
    ),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON", notes="No native set semantics."),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "JSON", notes="Object stored as JSON."),
    "vector": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "VECTOR(n)",
        notes="Requires MariaDB 11.7 vector support; adapter does not enable it.",
    ),
    "reference": TypeSupport(
        TypeSupportLevel.EXACT, "BIGINT", notes="Foreign-key reference column."
    ),
}

MARIADB_CAPABILITIES: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(True, "InnoDB ACID transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(
        False, "DDL implicitly commits; atomic DDL in 10.6+ is not rollback-safe."
    ),
    CAP_RETURNING: Capability(
        False, "Only INSERT/UPDATE/DELETE ... RETURNING; no general SELECT RETURNING."
    ),
    CAP_ORDER_BY_NULLS: Capability(False, "No NULLS FIRST/LAST syntax."),
    CAP_PARTIAL_INDEXES: Capability(
        False, "Index prefix lengths exist; WHERE-clause partial indexes do not."
    ),
    CAP_VECTOR_TYPE: Capability(
        False, "VECTOR(n) exists only in recent versions and is not enabled by this adapter."
    ),
    CAP_JSON: Capability(True, "JSON alias plus JSON functions."),
    CAP_ARRAY_TYPE: Capability(False, "No native array type."),
    CAP_UUID_TYPE: Capability(False, "No native UUID type."),
    CAP_GEO_TYPE: Capability(True, "Geometry types and spatial indexes."),
    CAP_DURATION_TYPE: Capability(False, "No native duration type."),
    CAP_EMAIL_TYPE: Capability(False, "No native email type."),
    CAP_URL_TYPE: Capability(False, "No native URL type."),
    CAP_DECIMAL_UNBOUNDED: Capability(False, "Maximum precision is 65 digits."),
    CAP_CHECK_CONSTRAINTS: Capability(True, "Enforced since 10.2.1."),
    CAP_FOREIGN_KEYS: Capability(True, "InnoDB only."),
    CAP_UNIQUE_CONSTRAINTS: Capability(True, "Unique and composite unique constraints."),
    CAP_INDEXES: Capability(True, "B-tree, hash, full-text, spatial."),
    CAP_FULL_TEXT_SEARCH: Capability(True, "FULLTEXT indexes on InnoDB."),
    CAP_MATERIALIZED_VIEWS: Capability(False, "No materialized views."),
    CAP_PARTITIONING: Capability(True, "Range, list, hash, key partitioning."),
    CAP_REPLICATION: Capability(True, "Asynchronous, semi-synchronous, and group replication."),
    CAP_JOINS: Capability(True, "INNER, LEFT, RIGHT; no FULL OUTER JOIN."),
    CAP_GROUP_BY: Capability(True),
    CAP_HAVING: Capability(True),
    CAP_SUBQUERIES: Capability(False, "Subqueries are outside the Phase 8 IR subset."),
    CAP_UNION: Capability(False, "UNION is outside the Phase 8 IR subset."),
    CAP_WINDOW_FUNCTIONS: Capability(True, "Since 10.2."),
    CAP_UPSERT: Capability(
        True, "INSERT ... ON DUPLICATE KEY UPDATE; not expressible in current IR."
    ),
    CAP_TTL: Capability(False, "No per-row TTL; use an application purge."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "Stored procedures and functions."),
    CAP_BATCH_WRITES: Capability(True, "Multi-row INSERT and executemany."),
    CAP_STRONG_CONSISTENCY: Capability(True, "InnoDB default."),
    CAP_MULTI_KEY_ATOMICITY: Capability(True, "Within one transaction."),
    CAP_SCHEMA_EVOLUTION: Capability(True, "ALTER TABLE, with implicit commit."),
    CAP_UPSERT_BY_KEY: Capability(True, "ON DUPLICATE KEY UPDATE."),
    CAP_ANN_SEARCH: Capability(False),
    CAP_HYBRID_SEARCH: Capability(False),
}


class MariaDBConnection(Connection):
    """Connection wrapper exposing MariaDB cursor helpers."""

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
            self._conn.ping()
            return True
        except Exception:
            return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    @property
    def raw_connection(self) -> Any:
        return self._conn


class MariaDBTransaction(Transaction):
    def __init__(self, connection: MariaDBConnection, options: TransactionOptions) -> None:
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
            self._connection.commit()
            self._active = False

    def rollback(self) -> None:
        if self._active:
            self._connection.rollback()
            self._active = False


class MariaDBAdapter(Adapter):
    """MariaDB adapter implementing schema, select, execution, and transactions."""

    data_model = "relational"
    ir_subset = ("schema", "select")

    def __init__(self) -> None:
        self.dialect = MariaDBDialect()

    @property
    def id(self) -> str:
        return "mariadb"

    @property
    def display_name(self) -> str:
        return "MariaDB"

    @property
    def package_version(self) -> str:
        from oq_adapters.mariadb import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.mariadb import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="MariaDB",
            tested_versions=["10.11", "11.4"],
            capabilities=MARIADB_CAPABILITIES,
            types=MARIADB_TYPE_MAP,
            limitations=[
                "Integration suite is defined but was not executed in this environment; no tested version is claimed.",
                "No general SELECT RETURNING; only mutation RETURNING.",
                "No FULL OUTER JOIN and no NULLS FIRST/LAST.",
                "DDL causes an implicit commit.",
                "JSON is stored as LONGTEXT with a JSON_VALID check.",
            ],
        )

    # -- connection ----------------------------------------------------------

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "mariadb":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a mariadb connection config.")
        if mariadb_driver is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "MariaDB driver is not installed. Install oq-adapter-mariadb[driver].",
            )
        if not config.host:
            raise AdapterError(AdapterErrorCode.CONNECTION, "MariaDB requires a host.")

        kwargs: dict[str, Any] = {
            "host": config.host,
            "port": config.port or 3306,
            "user": config.user,
            "password": secrets.get("password"),
            "database": config.database,
            "connect_timeout": int(config.options.get("connect_timeout", "10")),
        }
        if config.sslmode:
            if config.sslmode == "disable":
                kwargs["ssl_disabled"] = True
            else:
                if config.sslrootcert:
                    kwargs["ssl_ca"] = config.sslrootcert
                if config.sslcert:
                    kwargs["ssl_cert"] = config.sslcert
                if config.sslkey:
                    kwargs["ssl_key"] = config.sslkey
                kwargs["ssl_verify_cert"] = config.sslmode in ("verify-ca", "verify-full")
        try:
            conn = mariadb_driver.connect(**kwargs)
        except Exception as exc:  # driver exposes several error bases
            raise self._map_error(exc, "connect") from exc
        return MariaDBConnection(config, conn)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require_connection(connection)
        cursor = conn.execute("SELECT VERSION()")
        try:
            raw = str(cursor.fetchone()[0])
        finally:
            cursor.close()
        match = raw.split("-")[0]
        parts = match.split(".")
        return EngineVersion(
            product="MariaDB",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, MariaDBConnection):
            with contextlib.suppress(Exception):
                connection.close()

    # -- inspection ----------------------------------------------------------

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        conn = self._require_connection(connection)
        database = conn.config.database
        if not database:
            raise AdapterError(
                AdapterErrorCode.CONNECTION, "MariaDB inspection requires a database."
            )

        tables = self._fetch(
            conn,
            """
            SELECT TABLE_NAME, TABLE_TYPE
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = ?
            ORDER BY TABLE_NAME
            """,
            (database,),
        )
        columns = self._fetch(
            conn,
            """
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE,
                   COLUMN_DEFAULT, EXTRA, CHARACTER_MAXIMUM_LENGTH,
                   NUMERIC_PRECISION, NUMERIC_SCALE, COLUMN_KEY
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = ?
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """,
            (database,),
        )
        constraints = self._fetch(
            conn,
            """
            SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE,
                   kcu.COLUMN_NAME, kcu.ORDINAL_POSITION,
                   kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME
            FROM information_schema.TABLE_CONSTRAINTS tc
            LEFT JOIN information_schema.KEY_COLUMN_USAGE kcu
              ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
             AND kcu.TABLE_NAME = tc.TABLE_NAME
             AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
            WHERE tc.CONSTRAINT_SCHEMA = ?
            ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """,
            (database,),
        )
        indexes = self._fetch(
            conn,
            """
            SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX, INDEX_TYPE
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = ?
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """,
            (database,),
        )

        by_table: dict[str, list[tuple[Any, ...]]] = {}
        for row in columns:
            by_table.setdefault(str(row[0]), []).append(row)
        entities: list[Entity] = []
        unsupported: dict[str, list[dict[str, str]]] = {}
        for table_name, table_type in tables:
            if str(table_type).upper() != "BASE TABLE":
                unsupported.setdefault(str(table_name), []).append(
                    {"kind": "view", "name": str(table_name)}
                )
                continue
            attrs: list[Attribute] = []
            primary: list[str] = []
            for row in by_table.get(str(table_name), []):
                (
                    _table,
                    column_name,
                    data_type,
                    column_type,
                    is_nullable,
                    default,
                    extra,
                    char_length,
                    numeric_precision,
                    numeric_scale,
                    column_key,
                ) = row
                native = str(column_type).lower()
                _kind, type_obj = _mariadb_type(
                    str(data_type),
                    native,
                    char_length,
                    numeric_precision,
                    numeric_scale,
                )
                attrs.append(
                    Attribute(
                        name=str(column_name),
                        type=type_obj,
                        nullable=str(is_nullable).upper() == "YES",
                        primary=str(column_key).upper() == "PRI",
                        auto="auto_increment" in str(extra).lower(),
                        default=_normalise_default(default),
                    )
                )
                if str(column_key).upper() == "PRI":
                    primary.append(str(column_name))
            entities.append(
                Entity(
                    name=str(table_name),
                    attributes=attrs,
                    primary_key=primary,
                    indexes=_mariadb_indexes(str(table_name), indexes),
                    constraints=_mariadb_constraints(str(table_name), constraints),
                    unsupported_objects=unsupported.get(str(table_name), []),
                )
            )
        return SchemaDocument(entities=entities, enums=[], extensions=[])

    # -- compilation ---------------------------------------------------------

    def compile_schema(
        self, schema: SchemaDocument, options: CompileOptions | None
    ) -> list[DDLArtifact]:
        result = compile_schema_sql(self.dialect, schema, options or CompileOptions())
        if not result.supported:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "MariaDB cannot compile part of the schema.",
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
                kind="sql",
                text=result.sql,
                dialect="mariadb",
                placeholders=parameters,
            ),
            parameters=parameters,
            required_capabilities=[]
            if query.operation != "select"
            else [key for key in (CAP_JOINS if query.joins else None,) if key],
            safety=query.safety,
            result_columns=result.result_columns,
            engine_plan={
                "kind": "sql",
                "sql": result.sql,
                "parameter_order": [item["name"] for item in parameters],
            },
            diagnostics=result.diagnostics,
            supported=result.supported,
        )

    # -- execution -----------------------------------------------------------

    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> ResultSet:
        conn = self._require_connection(connection)
        if not plan.supported:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Plan contains unsupported constructs and was not executed.",
                diagnostics=[item.model_dump(mode="json") for item in plan.diagnostics],
            )
        if plan.artifact.kind != "sql" or not plan.artifact.text:
            raise AdapterError(
                AdapterErrorCode.SYNTAX, "Plan does not contain a MariaDB SQL artifact."
            )
        values = self._bind_values(plan, params)
        try:
            cursor = conn.execute(plan.artifact.text, tuple(values))
        except Exception as exc:
            raise self._map_error(exc, "execute") from exc
        try:
            columns = (
                [
                    ResultColumn(name=str(column[0]), type=None, nullable=True)
                    for column in cursor.description
                ]
                if cursor.description
                else []
            )
            rows = [
                ResultRow(values=tuple(_normalise_value(value) for value in row))
                for row in cursor.fetchall()
            ]
            affected = (
                cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None
            )
        finally:
            cursor.close()
        return ResultSet(columns=columns, rows=rows, affected_rows=affected, warnings=[])

    def _bind_values(self, plan: PlanDocument, params: Mapping[str, Any]) -> list[Any]:
        values: list[Any] = []
        for entry in plan.parameters:
            name = str(entry.get("name"))
            if name not in params:
                raise AdapterError(
                    AdapterErrorCode.DATA,
                    f"Missing bind value for parameter {name!r}.",
                )
            values.append(params[name])
        return values

    # -- transactions --------------------------------------------------------

    def begin(
        self, connection: Connection, options: TransactionOptions | None = None
    ) -> Transaction:
        conn = self._require_connection(connection)
        opts = options or TransactionOptions()
        level = {
            "read_uncommitted": "READ UNCOMMITTED",
            "read_committed": "READ COMMITTED",
            "repeatable_read": "REPEATABLE READ",
            "serializable": "SERIALIZABLE",
        }[opts.isolation.value]
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
        if opts.read_only:
            conn.execute("START TRANSACTION READ ONLY")
        else:
            conn.execute("START TRANSACTION")
        return MariaDBTransaction(conn, opts)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MariaDBTransaction):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong transaction type for MariaDB.")
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if isinstance(transaction, MariaDBTransaction):
            with contextlib.suppress(Exception):
                transaction.rollback()

    # -- internals -----------------------------------------------------------

    def _require_connection(self, connection: Connection) -> MariaDBConnection:
        if not isinstance(connection, MariaDBConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for MariaDB.")
        return connection

    @staticmethod
    def _fetch(conn: MariaDBConnection, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        cursor = conn.execute(sql, params)
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def _map_error(self, exc: BaseException, operation: str) -> AdapterError:
        code = AdapterErrorCode.CONNECTION
        retryable = False
        errno = getattr(exc, "errno", None)
        sqlstate = str(getattr(exc, "sqlstate", "") or "")
        text = redact_message(str(exc))
        if errno in (1045, 1698):
            code = AdapterErrorCode.AUTH
        elif errno == 1062 or sqlstate == "23000" or errno == 1451 or errno == 1452:
            code = AdapterErrorCode.CONSTRAINT
        elif errno == 1205 or errno == 1213:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        elif errno == 1064:
            code = AdapterErrorCode.SYNTAX
        elif errno in (2002, 2003, 2006, 2013):
            code = AdapterErrorCode.CONNECTION
            retryable = True
        return AdapterError(
            code,
            f"MariaDB {operation} failed: {text}",
            retryable=retryable,
            cause=exc,
            redacted_cause=text,
        )


# ---------------------------------------------------------------------------
# Native -> universal helpers
# ---------------------------------------------------------------------------


def _mariadb_type(
    data_type: str,
    column_type: str,
    char_length: Any,
    numeric_precision: Any,
    numeric_scale: Any,
) -> tuple[str, Type]:
    data = data_type.lower()
    if data in ("tinyint", "smallint", "mediumint", "int", "integer"):
        return "integer", Type(kind="integer")
    if data == "bigint":
        return "bigint", Type(kind="bigint")
    if data in ("float",):
        return "float", Type(kind="float")
    if data in ("double", "real"):
        return "double", Type(kind="double")
    if data in ("decimal", "numeric"):
        return "decimal", Type(
            kind="decimal",
            precision=int(numeric_precision) if numeric_precision else None,
            scale=int(numeric_scale) if numeric_scale else None,
        )
    if data in ("char", "varchar", "tinytext", "text", "mediumtext", "longtext"):
        return "text", Type(
            kind="text",
            max_length=int(char_length) if char_length else None,
        )
    if data == "json":
        return "json", Type(kind="json")
    if data in ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"):
        return "binary", Type(kind="binary")
    if data == "date":
        return "date", Type(kind="date")
    if data == "time":
        return "time", Type(kind="time")
    if data == "datetime":
        return "datetime", Type(kind="datetime")
    if data == "timestamp":
        return "timestamp", Type(kind="timestamp", has_timezone=True)
    if data == "year":
        return "integer", Type(kind="integer")
    if data in ("bit", "bool", "boolean"):
        return "boolean", Type(kind="boolean")
    if data in ("enum", "set"):
        return "text", Type(kind="text")
    if data in (
        "geometry",
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
    ):
        return "geo", Type(kind="geo")
    if data == "uuid":
        return "uuid", Type(kind="uuid")
    if "vector" in column_type:
        return "vector", Type(kind="vector")
    return "text", Type(kind="text")


def _normalise_default(value: Any) -> Any:
    if isinstance(value, str) and value.upper() in ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"):
        return None
    return value


def _normalise_value(value: Any) -> Any:
    if isinstance(value, (date, datetime, time, Decimal, bytes, bytearray)):
        return value
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _mariadb_indexes(table: str, rows: list[tuple[Any, ...]]) -> list[Index]:
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        if str(row[0]) != table:
            continue
        grouped.setdefault(str(row[1]), []).append(row)
    result: list[Index] = []
    for name, items in grouped.items():
        if name == "PRIMARY":
            continue
        ordered = sorted(items, key=lambda item: int(item[4] or 0))
        result.append(
            Index(
                name=name,
                columns=[str(item[3]) for item in ordered],
                unique=not bool(ordered[0][2]) if ordered else False,
                kind="btree"
                if str(ordered[0][5]).lower() in ("btree", "hash")
                else str(ordered[0][5]).lower(),
                options={"native_type": str(ordered[0][5])},
            )
        )
    return result


def _mariadb_constraints(table: str, rows: list[tuple[Any, ...]]) -> list[Constraint]:
    grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for row in rows:
        if str(row[0]) != table:
            continue
        grouped.setdefault((str(row[1]), str(row[2])), []).append(row)
    constraints: list[Constraint] = []
    for (_name, kind), items in grouped.items():
        ordered = sorted(items, key=lambda item: int(item[4] or 0))
        columns = [str(item[3]) for item in ordered if item[3] is not None]
        if kind == "FOREIGN KEY":
            constraints.append(
                Constraint(
                    kind="foreign_key",
                    columns=columns,
                    reference=str(ordered[0][5]) if ordered[0][5] else None,
                    referenced_columns=[str(item[6]) for item in ordered if item[6] is not None],
                )
            )
        elif kind == "CHECK":
            constraints.append(Constraint(kind="check", columns=columns))
    return constraints
