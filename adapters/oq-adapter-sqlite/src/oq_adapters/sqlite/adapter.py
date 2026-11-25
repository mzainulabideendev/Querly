"""SQLite adapter implementation for Querly.

This adapter uses Python's built-in sqlite3 module. SQLite is the local
baseline engine with no external infrastructure requirements.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from oq.ir import QueryDocument, SchemaDocument
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
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
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
)

__all__ = ["SQLiteAdapter", "SqliteConnection", "SqliteTransaction"]


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

SQLITE_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="INTEGER",
        range="-9223372036854775808..9223372036854775807",
    ),
    "float": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="REAL",
    ),
    "decimal": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="NUMERIC",
        max_precision=1000,
        notes="SQLite uses NUMERIC affinity; precision/scale not enforced",
    ),
    "text": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="TEXT",
        max_length=1000000000,
    ),
    "boolean": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="INTEGER",
        notes="Stored as 0/1; no native boolean type",
    ),
    "date": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="TEXT",
        notes="Stored as ISO 8601 TEXT; no native date type",
    ),
    "time": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="TEXT",
        notes="Stored as ISO 8601 TEXT; no native time type",
    ),
    "timestamp": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="TEXT",
        notes="Stored as ISO 8601 TEXT with timezone; no native timestamp type",
    ),
    "duration": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native duration type; store as integer microseconds",
    ),
    "uuid": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="TEXT",
        notes="Stored as TEXT; no native UUID type or generation",
    ),
    "email": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native email type; store as TEXT with CHECK constraint",
    ),
    "url": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native URL type; store as TEXT with CHECK constraint",
    ),
    "json": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="TEXT",
        notes="SQLite JSON1 extension provides json() functions; stored as TEXT",
    ),
    "binary": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="BLOB",
    ),
    "geo": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native geography type; requires SpatiaLite extension",
    ),
    "list": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native array type; use JSON or normalized tables",
    ),
    "set": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native set type; use JSON or normalized tables",
    ),
    "map": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native map type; use JSON or normalized tables",
    ),
    "vector": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native vector type; requires sqlite-vss or similar extension",
    ),
    "reference": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="INTEGER",
        notes="Foreign key reference; no native reference type",
    ),
}


SQLITE_CAPABILITIES: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(supported=True, notes=None),
    CAP_TRANSACTIONAL_DDL: Capability(supported=True, notes="SQLite supports transactional DDL"),
    CAP_RETURNING: Capability(supported=True, notes="RETURNING clause supported since SQLite 3.35"),
    CAP_ORDER_BY_NULLS: Capability(
        supported=False, notes="No NULLS FIRST/LAST; NULLs always sort first"
    ),
    CAP_PARTIAL_INDEXES: Capability(supported=True, notes="WHERE clause on CREATE INDEX"),
    CAP_VECTOR_TYPE: Capability(supported=False, notes="Requires sqlite-vss extension"),
    CAP_JSON: Capability(supported=True, notes="JSON1 extension built into modern SQLite"),
    CAP_ARRAY_TYPE: Capability(supported=False, notes="No native array type"),
    CAP_UUID_TYPE: Capability(supported=False, notes="No native UUID type"),
    CAP_GEO_TYPE: Capability(supported=False, notes="Requires SpatiaLite extension"),
    CAP_DURATION_TYPE: Capability(supported=False, notes="No native duration type"),
    CAP_EMAIL_TYPE: Capability(supported=False, notes="No native email type"),
    CAP_URL_TYPE: Capability(supported=False, notes="No native URL type"),
    CAP_DECIMAL_UNBOUNDED: Capability(supported=False, notes="No precision/scale enforcement"),
    CAP_CHECK_CONSTRAINTS: Capability(supported=True, notes=None),
    CAP_FOREIGN_KEYS: Capability(supported=True, notes="Must enable with PRAGMA foreign_keys=ON"),
    CAP_UNIQUE_CONSTRAINTS: Capability(supported=True, notes=None),
    CAP_INDEXES: Capability(supported=True, notes=None),
    CAP_FULL_TEXT_SEARCH: Capability(supported=True, notes="FTS5 extension"),
    CAP_MATERIALIZED_VIEWS: Capability(supported=False, notes="Use triggers or manual refresh"),
    CAP_PARTITIONING: Capability(supported=False, notes="No native partitioning"),
    CAP_REPLICATION: Capability(supported=False, notes="No built-in replication"),
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class SqliteConnection(Connection):
    """SQLite connection wrapper."""

    def __init__(self, config: ConnectionConfig, conn: sqlite3.Connection) -> None:
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
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple]) -> sqlite3.Cursor:
        return self._conn.executemany(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    @property
    def raw_connection(self) -> sqlite3.Connection:
        return self._conn


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class SqliteTransaction(Transaction):
    """SQLite transaction wrapper."""

    def __init__(self, connection: SqliteConnection, options: TransactionOptions) -> None:
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


class SQLiteAdapter(Adapter):
    """SQLite adapter for Querly."""

    @property
    def id(self) -> str:
        return "sqlite"

    @property
    def display_name(self) -> str:
        return "SQLite"

    @property
    def package_version(self) -> str:
        from oq_adapters.sqlite import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.sqlite import __version__

        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="SQLite",
            tested_versions=["3.45", "3.46", "3.47"],
            capabilities=SQLITE_CAPABILITIES,
            types=SQLITE_TYPE_MAP,
            limitations=[
                "No concurrent writes (single writer)",
                "No row-level locking",
                "Type affinity not enforcement",
                "No native enum type",
                "ALTER TABLE limited (no column drop until 3.35, no rename constraint)",
            ],
            generated_at=datetime.now(UTC),
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "sqlite":
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Expected sqlite adapter, got {config.adapter}",
            )

        if not config.path:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "SQLite connection requires 'path' in config",
            )

        try:
            conn = sqlite3.connect(
                config.path,
                isolation_level=None,  # autocommit mode; we control transactions explicitly
                check_same_thread=False,
            )
            # Enable foreign keys
            conn.execute("PRAGMA foreign_keys = ON")
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode = WAL")
            return SqliteConnection(config, conn)
        except sqlite3.Error as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Failed to connect to SQLite: {redact_message(str(e))}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            ) from e

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        if not isinstance(connection, SqliteConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for SQLite adapter",
            )

        # This is a placeholder - full implementation requires catalog queries
        # Return empty schema for now
        return SchemaDocument(entities=[], enums=[], extensions=[])

    def compile_schema(self, schema: SchemaDocument, options: CompileOptions) -> list[DDLArtifact]:
        """Compile universal schema IR into SQLite DDL."""
        artifacts: list[DDLArtifact] = []

        for entity in schema.entities:
            # Build CREATE TABLE
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
                    columns_sql.append(f"UNIQUE ({', '.join(uc.columns)})")

            table_sql = (
                f"CREATE TABLE {self._quote(entity.name)} (\n  " + ",\n  ".join(columns_sql) + "\n)"
            )
            artifacts.append(
                DDLArtifact(
                    sql=table_sql,
                    statement_type="create_table",
                    entity=entity.name,
                )
            )

            for idx in entity.indexes:
                idx_sql = f"CREATE {'UNIQUE ' if idx.unique else ''}INDEX {self._quote(idx.name)} ON {self._quote(entity.name)} ({', '.join(self._quote(c) for c in idx.columns)})"
                artifacts.append(
                    DDLArtifact(
                        sql=idx_sql,
                        statement_type="create_index",
                        entity=entity.name,
                        depends_on=[entity.name],
                    )
                )

        return artifacts

    def _compile_attribute(self, attr) -> str:
        """Compile a single attribute to SQLite column definition."""
        type_sql = self._map_type(attr.type)
        parts = [f"{self._quote(attr.name)} {type_sql}"]

        if not attr.nullable:
            parts.append("NOT NULL")
        if attr.auto:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        if attr.unique and not attr.primary:
            parts.append("UNIQUE")
        if attr.default is not None:
            parts.append(f"DEFAULT {self._format_default(attr.default)}")
        if attr.check is not None:
            parts.append(f"CHECK ({attr.check})")

        return " ".join(parts)

    def _map_type(self, type_obj: Any) -> str:
        """Map universal type to SQLite type."""
        kind = getattr(type_obj, "kind", None) or (
            type_obj.get("kind") if isinstance(type_obj, dict) else None
        )

        if kind == "integer":
            return "INTEGER"
        elif kind == "float":
            return "REAL"
        elif kind == "decimal":
            p = getattr(type_obj, "precision", None) or (
                type_obj.get("precision") if isinstance(type_obj, dict) else None
            )
            s = getattr(type_obj, "scale", None) or (
                type_obj.get("scale") if isinstance(type_obj, dict) else None
            )
            if p and s:
                return f"NUMERIC({p},{s})"
            return "NUMERIC"
        elif kind == "text":
            return "TEXT"  # SQLite doesn't enforce length
        elif kind == "boolean":
            return "INTEGER"
        elif kind in ("date", "time", "timestamp"):
            return "TEXT"
        elif kind == "binary":
            return "BLOB"
        elif kind == "json" or kind == "uuid":
            return "TEXT"
        elif kind == "reference":
            return "INTEGER"
        return "TEXT"

    def _format_default(self, default: Any) -> str:
        if default is None:
            return "NULL"
        # Simplified - would need proper expression handling
        return str(default)

    def _quote(self, identifier: str) -> str:
        return f'"{identifier}"'

    def compile_query(self, query: QueryDocument, schema: SchemaDocument) -> PlanDocument:
        """Compile universal query IR into SQLite plan."""
        # This is a placeholder - full implementation required
        from oq.ir import Artifact

        artifact = Artifact(
            kind="sql",
            text="-- Compiled query placeholder",
            dialect="sqlite",
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
        """Execute a compiled plan."""
        if not isinstance(connection, SqliteConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for SQLite adapter",
            )

        cursor = connection.execute(plan.artifact.text, tuple(params.values()))
        columns = (
            [ResultColumn(name=col[0], type=None, nullable=True) for col in cursor.description]
            if cursor.description
            else []
        )
        rows = [ResultRow(values=tuple(row)) for row in cursor.fetchall()]

        return ResultSet(
            columns=columns,
            rows=rows,
            affected_rows=cursor.rowcount if cursor.rowcount >= 0 else None,
            warnings=[],
        )

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        if not isinstance(connection, SqliteConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for SQLite adapter",
            )

        # SQLite uses BEGIN IMMEDIATE for write transactions, BEGIN DEFERRED for read-only
        if options.read_only:
            connection.execute("BEGIN DEFERRED")
        else:
            connection.execute("BEGIN IMMEDIATE")

        return SqliteTransaction(connection, options)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, SqliteTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for SQLite adapter",
            )
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if not isinstance(transaction, SqliteTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for SQLite adapter",
            )
        transaction.rollback()

    def detect_version(self, connection: Connection) -> EngineVersion:
        if not isinstance(connection, SqliteConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for SQLite adapter",
            )

        cursor = connection.execute("SELECT sqlite_version()")
        version_str = cursor.fetchone()[0]
        parts = version_str.split(".")
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0

        return EngineVersion(
            product="SQLite",
            version=version_str,
            major=major,
            minor=minor,
            patch=patch,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, SqliteConnection):
            connection.close()


# Re-export for the package
from oq.runtime.contract import CompileOptions  # noqa: E402
