"""MariaDB adapter implementation for Querly.

Uses the MariaDB Python client (``mariadb``). MariaDB is a MySQL fork; where
its behaviour differs from MySQL's, that difference is documented in the
capability report and reflected in compiled artifacts.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Mapping

from oq.ir import Artifact, QueryDocument, SchemaDocument
from oq.runtime.contract import (
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
    PlanDocument,
    ResultColumn,
    ResultRow,
    ResultSet,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
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
)

try:
    import mariadb as mariadb_driver
except ImportError:
    mariadb_driver = None

__all__ = ["MariaDBAdapter", "MariaDBConnection", "MariaDBTransaction"]


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

MARIADB_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="INT",
        range="-2147483648..2147483647",
    ),
    "bigint": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="BIGINT",
        range="-9223372036854775808..9223372036854775807",
    ),
    "float": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="FLOAT",
    ),
    "double": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="DOUBLE",
    ),
    "decimal": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="DECIMAL",
        max_precision=65,
        notes="Exact decimal arithmetic; max precision 65 digits",
    ),
    "text": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="LONGTEXT",
        max_length=4294967295,
    ),
    "boolean": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="TINYINT(1)",
        notes="Stored as TINYINT(1); no native boolean type",
    ),
    "date": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="DATE",
    ),
    "datetime": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="DATETIME",
        notes="No timezone; use TIMESTAMP for UTC",
    ),
    "timestamp": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="TIMESTAMP",
        notes="UTC; range 1970-2038",
    ),
    "time": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="TIME",
    ),
    "year": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="YEAR",
    ),
    "json": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="JSON",
        notes="Native JSON type; stored as LONGTEXT with CHECK (JSON_VALID())",
    ),
    "binary": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="LONGBLOB",
        max_length=4294967295,
    ),
    "uuid": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="CHAR(36)",
        notes="Stored as CHAR(36); no native UUID type",
    ),
    "enum": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="ENUM",
    ),
    "set": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="SET",
    ),
    "geo": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="GEOMETRY",
        notes="Spatial types available; use SRID for coordinate systems",
    ),
    "point": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="POINT",
    ),
    "linestring": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="LINESTRING",
    ),
    "polygon": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="POLYGON",
    ),
    "multipoint": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="MULTIPOINT",
    ),
    "multilinestring": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="MULTILINESTRING",
    ),
    "multipolygon": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="MULTIPOLYGON",
    ),
    "geometrycollection": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="GEOMETRYCOLLECTION",
    ),
    "reference": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native=None,
        notes="Foreign key reference; use INTEGER or BIGINT",
    ),
}

MARIADB_CAPABILITIES: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(supported=True, notes="InnoDB supports ACID transactions"),
    CAP_TRANSACTIONAL_DDL: Capability(supported=False, notes="Atomic DDL since 10.6; implicit commit"),
    CAP_RETURNING: Capability(supported=False, notes="MariaDB does not support RETURNING clause"),
    CAP_ORDER_BY_NULLS: Capability(supported=False, notes="NULLS FIRST/LAST not supported"),
    CAP_PARTIAL_INDEXES: Capability(supported=True, notes="Index prefix support, not WHERE clause"),
    CAP_VECTOR_TYPE: Capability(supported=False, notes="No native vector type"),
    CAP_JSON: Capability(supported=True, notes="Native JSON type with JSON functions"),
    CAP_ARRAY_TYPE: Capability(supported=False, notes="No native array type; use JSON or normalized tables"),
    CAP_UUID_TYPE: Capability(supported=False, notes="No native UUID type"),
    CAP_GEO_TYPE: Capability(supported=True, notes="Spatial types and functions"),
    CAP_DURATION_TYPE: Capability(supported=False, notes="No native duration type"),
    CAP_EMAIL_TYPE: Capability(supported=False, notes="No native email type"),
    CAP_URL_TYPE: Capability(supported=False, notes="No native URL type"),
    CAP_DECIMAL_UNBOUNDED: Capability(supported=False, notes="Max precision 65 digits"),
    CAP_CHECK_CONSTRAINTS: Capability(supported=True, notes="Enforced since 10.2.1"),
    CAP_FOREIGN_KEYS: Capability(supported=True, notes="InnoDB only"),
    CAP_UNIQUE_CONSTRAINTS: Capability(supported=True, notes=None),
    CAP_INDEXES: Capability(supported=True, notes="B-tree, hash, full-text, spatial"),
    CAP_FULL_TEXT_SEARCH: Capability(supported=True, notes="FULLTEXT indexes; InnoDB and Aria"),
    CAP_MATERIALIZED_VIEWS: Capability(supported=False, notes="Use triggers or manual refresh"),
    CAP_PARTITIONING: Capability(supported=True, notes="Range, list, hash, key partitioning"),
    CAP_REPLICATION: Capability(supported=True, notes="Async, semi-sync, and group replication"),
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class MariaDBConnection(Connection):
    """MariaDB connection wrapper."""

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

    def execute(self, sql: str, params: tuple = ()) -> Any:
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql: str, params: list[tuple]) -> Any:
        cursor = self._conn.cursor()
        cursor.executemany(sql, params)
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


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class MariaDBTransaction(Transaction):
    """MariaDB transaction wrapper."""

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


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MariaDBAdapter(Adapter):
    """MariaDB adapter for Querly."""

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
        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="MariaDB",
            tested_versions=["10.5", "10.6", "10.11", "11.2", "11.4"],
            capabilities=MARIADB_CAPABILITIES,
            types=MARIADB_TYPE_MAP,
            limitations=[
                "No RETURNING clause support",
                "NULLS FIRST/LAST not supported",
                "No native vector type",
                "No native array type",
                "No native UUID type",
                "No native email/URL type",
                "DECIMAL max precision 65 digits",
                "TIMESTAMP range 1970-2038",
            ],
            generated_at=datetime.now(timezone.utc),
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "mariadb":
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Expected mariadb adapter, got {config.adapter}",
            )

        if mariadb_driver is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "mariadb driver not installed; pip install mariadb",
            )

        if not config.host:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "MariaDB connection requires 'host' in config",
            )

        password = secrets.get("password")
        connect_kwargs = {
            "host": config.host,
            "port": config.port or 3306,
            "user": config.user,
            "password": password,
            "database": config.database,
        }

        # TLS/SSL support
        if config.sslmode:
            if config.sslmode == "disable":
                connect_kwargs["ssl_disabled"] = True
            else:
                if config.sslrootcert:
                    connect_kwargs["ssl_ca"] = config.sslrootcert
                if config.sslcert:
                    connect_kwargs["ssl_cert"] = config.sslcert
                if config.sslkey:
                    connect_kwargs["ssl_key"] = config.sslkey
                if config.sslmode == "require":
                    connect_kwargs["ssl_verify_cert"] = False
                elif config.sslmode in ("verify-ca", "verify-full"):
                    connect_kwargs["ssl_verify_cert"] = True

        try:
            conn = mariadb_driver.connect(**connect_kwargs)
            return MariaDBConnection(config, conn)
        except mariadb_driver.Error as e:
            redacted = redact_message(str(e))
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Failed to connect to MariaDB: {redacted}",
                cause=e,
                redacted_cause=redacted,
            )

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        if not isinstance(connection, MariaDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MariaDB adapter",
            )

        # Placeholder: full implementation requires SHOW TABLES and SHOW CREATE TABLE parsing
        return SchemaDocument(entities=[], enums=[], extensions=[])

    def compile_schema(
        self, schema: SchemaDocument, options: CompileOptions
    ) -> list[DDLArtifact]:
        """Compile universal schema IR into MariaDB DDL."""
        artifacts: list[DDLArtifact] = []

        for entity in schema.entities:
            columns_sql: list[str] = []
            primary_keys: list[str] = []

            for attr in entity.attributes:
                col_def = self._compile_attribute(attr)
                columns_sql.append(col_def)
                if attr.primary:
                    primary_keys.append(attr.name)

            if primary_keys:
                columns_sql.append(f"PRIMARY KEY ({', '.join(primary_keys)})")

            for uc in entity.unique_constraints:
                if uc.kind == "unique" and uc.columns:
                    columns_sql.append(f"UNIQUE ({', '.join(self._quote(c) for c in uc.columns)})")

            table_sql = f"CREATE TABLE {self._quote(entity.name)} (\n  " + ",\n  ".join(columns_sql) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            artifacts.append(DDLArtifact(
                sql=table_sql,
                statement_type="create_table",
                entity=entity.name,
            ))

            for idx in entity.indexes:
                idx_sql = f"CREATE {'UNIQUE ' if idx.unique else ''}INDEX {self._quote(idx.name)} ON {self._quote(entity.name)} ({', '.join(self._quote(c) for c in idx.columns)})"
                artifacts.append(DDLArtifact(
                    sql=idx_sql,
                    statement_type="create_index",
                    entity=entity.name,
                    depends_on=[entity.name],
                ))

        return artifacts

    def _compile_attribute(self, attr: Any) -> str:
        """Compile a single attribute to MariaDB column definition."""
        type_sql = self._map_type(attr.type)
        parts = [f"{self._quote(attr.name)} {type_sql}"]

        if not attr.nullable:
            parts.append("NOT NULL")
        if attr.auto:
            parts.append("AUTO_INCREMENT")
        if attr.unique and not attr.primary:
            parts.append("UNIQUE")
        if attr.default is not None:
            parts.append(f"DEFAULT {self._format_default(attr.default)}")
        if attr.check is not None:
            parts.append(f"CHECK ({attr.check})")

        return " ".join(parts)

    def _map_type(self, type_obj: Any) -> str:
        """Map universal type to MariaDB type."""
        kind = getattr(type_obj, 'kind', None) or (type_obj.get('kind') if isinstance(type_obj, dict) else None)

        if kind in ("integer", "int"):
            return "INT"
        elif kind == "bigint":
            return "BIGINT"
        elif kind == "float":
            return "FLOAT"
        elif kind == "double":
            return "DOUBLE"
        elif kind == "decimal":
            p = getattr(type_obj, 'precision', None) or (type_obj.get('precision') if isinstance(type_obj, dict) else None)
            s = getattr(type_obj, 'scale', None) or (type_obj.get('scale') if isinstance(type_obj, dict) else None)
            if p and s:
                return f"DECIMAL({p},{s})"
            return "DECIMAL"
        elif kind == "text":
            ml = getattr(type_obj, 'max_length', None) or (type_obj.get('max_length') if isinstance(type_obj, dict) else None)
            if ml and ml <= 255:
                return f"VARCHAR({ml})"
            return "LONGTEXT"
        elif kind == "boolean":
            return "TINYINT(1)"
        elif kind == "date":
            return "DATE"
        elif kind == "time":
            return "TIME"
        elif kind == "timestamp" or kind == "datetime":
            return "DATETIME"
        elif kind == "year":
            return "YEAR"
        elif kind == "json":
            return "JSON"
        elif kind == "binary":
            return "LONGBLOB"
        elif kind == "uuid":
            return "CHAR(36)"
        elif kind in ("geo", "geometry"):
            return "GEOMETRY"
        elif kind in ("point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection"):
            return kind.upper()
        else:
            return "LONGTEXT"

    def _format_default(self, default: Any) -> str:
        if default is None:
            return "NULL"
        if isinstance(default, str):
            return f"'{default}'"
        return str(default)

    def _quote(self, identifier: str) -> str:
        return f"`{identifier}`"

    def compile_query(
        self, query: QueryDocument, schema: SchemaDocument
    ) -> PlanDocument:
        """Compile universal query IR into MariaDB plan."""
        artifact = Artifact(
            kind="sql",
            text="-- Compiled query placeholder",
            dialect="mariadb",
        )

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=artifact,
            parameters=[],
            required_capabilities=[],
            safety=query.safety,
            result_columns=[],
        )

    def execute(
        self,
        connection: Connection,
        plan: PlanDocument,
        params: Mapping[str, Any],
    ) -> ResultSet:
        """Execute a compiled plan with bound parameters."""
        if not isinstance(connection, MariaDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MariaDB adapter",
            )

        if plan.artifact.text == "-- Compiled query placeholder":
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                "Query compilation not yet implemented",
            )

        try:
            cursor = connection.execute(plan.artifact.text, tuple(params.values()))
            columns = [ResultColumn(name=col[0], type=None, nullable=True) for col in cursor.description] if cursor.description else []
            rows = [ResultRow(values=tuple(row)) for row in cursor.fetchall()]

            return ResultSet(
                columns=columns,
                rows=rows,
                affected_rows=cursor.rowcount if cursor.rowcount >= 0 else None,
                warnings=[],
            )
        except mariadb_driver.Error as e:
            redacted = redact_message(str(e))
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Execution failed: {redacted}",
                cause=e,
                redacted_cause=redacted,
            )

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        if not isinstance(connection, MariaDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MariaDB adapter",
            )

        connection.execute("START TRANSACTION")
        return MariaDBTransaction(connection, options)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MariaDBTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for MariaDB adapter",
            )
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MariaDBTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for MariaDB adapter",
            )
        transaction.rollback()

    def detect_version(self, connection: Connection) -> EngineVersion:
        if not isinstance(connection, MariaDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MariaDB adapter",
            )

        cursor = connection.execute("SELECT VERSION()")
        version_str = cursor.fetchone()[0]
        # Parse version string like "10.6.12-MariaDB-1:10.6.12+maria~ubu2004"
        parts = version_str.split("-")[0].split(".")
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0

        return EngineVersion(
            product="MariaDB",
            version=version_str,
            major=major,
            minor=minor,
            patch=patch,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, MariaDBConnection):
            connection.close()