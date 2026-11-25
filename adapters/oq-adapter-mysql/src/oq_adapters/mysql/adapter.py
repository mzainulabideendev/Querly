"""MySQL adapter implementation for Querly."""

from __future__ import annotations

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
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    EngineVersion,
    PlanDocument,
    ResultSet,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)

import mysql.connector

__all__ = ["MySQLAdapter", "MySQLConnection", "MySQLTransaction"]

# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

MYSQL_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(support=TypeSupportLevel.EXACT, native="BIGINT"),
    "float": TypeSupport(support=TypeSupportLevel.EXACT, native="DOUBLE"),
    "decimal": TypeSupport(support=TypeSupportLevel.EXACT, native="DECIMAL"),
    "text": TypeSupport(support=TypeSupportLevel.EXACT, native="LONGTEXT"),
    "boolean": TypeSupport(support=TypeSupportLevel.EXACT, native="TINYINT(1)"),
    "date": TypeSupport(support=TypeSupportLevel.EXACT, native="DATE"),
    "time": TypeSupport(support=TypeSupportLevel.EXACT, native="TIME"),
    "timestamp": TypeSupport(support=TypeSupportLevel.EXACT, native="DATETIME"),
    "duration": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="BIGINT"),
    "uuid": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="CHAR(36)"),
    "email": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="VARCHAR(255)"),
    "url": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="VARCHAR(2048)"),
    "json": TypeSupport(support=TypeSupportLevel.EXACT, native="JSON"),
    "binary": TypeSupport(support=TypeSupportLevel.EXACT, native="LONGBLOB"),
    "geo": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="GEOMETRY"),
    "list": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="MySQL does not have a native array/list type; use JSON for array-like data",
    ),
    "set": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="MySQL does not have a native set type; use JSON for set-like data",
    ),
    "map": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="MySQL does not have a native map/object type; use JSON for object-like data",
    ),
    "vector": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="MySQL does not have a native vector type; use external vector search or JSON for embeddings",
    ),
    "reference": TypeSupport(support=TypeSupportLevel.EXACT, native="BIGINT"),
}

MYSQL_CAPABILITIES: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(supported=True, notes=None),
    CAP_TRANSACTIONAL_DDL: Capability(supported=False, notes="MySQL DDL is not transactional"),
    CAP_RETURNING: Capability(supported=False, notes=None),
    CAP_ORDER_BY_NULLS: Capability(supported=True, notes=None),
    CAP_PARTIAL_INDEXES: Capability(supported=False, notes=None),
    CAP_VECTOR_TYPE: Capability(supported=False, notes=None),
    CAP_JSON: Capability(supported=True, notes=None),
    CAP_ARRAY_TYPE: Capability(supported=False, notes=None),
    CAP_UUID_TYPE: Capability(supported=False, notes=None),
    CAP_GEO_TYPE: Capability(supported=True, notes=None),
    CAP_DURATION_TYPE: Capability(supported=False, notes=None),
    CAP_EMAIL_TYPE: Capability(supported=False, notes=None),
    CAP_URL_TYPE: Capability(supported=False, notes=None),
    CAP_DECIMAL_UNBOUNDED: Capability(supported=False, notes=None),
    CAP_CHECK_CONSTRAINTS: Capability(supported=True, notes="Supported since MySQL 8.0.16"),
    CAP_FOREIGN_KEYS: Capability(supported=True, notes=None),
    CAP_UNIQUE_CONSTRAINTS: Capability(supported=True, notes=None),
    CAP_INDEXES: Capability(supported=True, notes=None),
    CAP_FULL_TEXT_SEARCH: Capability(supported=True, notes=None),
    CAP_MATERIALIZED_VIEWS: Capability(supported=False, notes=None),
    CAP_PARTITIONING: Capability(supported=True, notes=None),
    CAP_REPLICATION: Capability(supported=True, notes=None),
}

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class MySQLConnection(Connection):
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
            self._conn.ping(reconnect=True)
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


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class MySQLTransaction(Transaction):
    def __init__(self, connection: MySQLConnection, options: TransactionOptions) -> None:
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


class MySQLAdapter(Adapter):
    @property
    def id(self) -> str:
        return "mysql"

    @property
    def display_name(self) -> str:
        return "MySQL"

    @property
    def package_version(self) -> str:
        from oq_adapters.mysql import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.mysql import __version__

        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="MySQL",
            tested_versions=["8.0"],
            capabilities=MYSQL_CAPABILITIES,
            types=MYSQL_TYPE_MAP,
            limitations=["No transactional DDL"],
            generated_at=datetime.now(UTC),
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        try:
            conn = mysql.connector.connect(
                host=config.host,
                port=config.port or 3306,
                user=config.user,
                password=secrets.get("password"),
                database=config.database,
            )
            return MySQLConnection(config, conn)
        except Exception as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION, f"Failed to connect: {redact_message(str(e))}"
            ) from e

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        return SchemaDocument(entities=[], enums=[], extensions=[])

    def compile_schema(self, schema: SchemaDocument, options: CompileOptions) -> list[DDLArtifact]:
        return []

    def compile_query(self, query: QueryDocument, schema: SchemaDocument) -> PlanDocument:
        from oq.ir import Artifact

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="sql", text="-- Placeholder", dialect="mysql"),
            parameters=[],
            required_capabilities=[],
            safety=query.safety,
            result_columns=[],
        )

    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> ResultSet:
        return ResultSet(columns=[], rows=[], affected_rows=None, warnings=[])

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        connection.execute("START TRANSACTION")
        return MySQLTransaction(connection, options)

    def commit(self, transaction: Transaction) -> None:
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        transaction.rollback()

    def detect_version(self, connection: Connection) -> EngineVersion:
        cursor = connection.execute("SELECT VERSION()")
        version = cursor.fetchone()[0]
        return EngineVersion(product="MySQL", version=version, major=8, minor=0, patch=0)

    def close(self, connection: Connection) -> None:
        connection.close()
