"""Adapter contract: protocol definitions and shared types for database adapters.

This module defines the interface that all Querly adapters must implement.
Adapters are discovered via the `oq.adapters` entry point group.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from oq.ir import (
    DocumentQuery,
    DocumentStoreSchema,
    KeySpaceSchema,
    KeyValueQuery,
    PlanDocument,
    QueryDocument,
    SchemaDocument,
)

__all__ = [  # noqa: RUF022 - grouped for readability
    # Configuration
    "ConnectionConfig",
    "SecretProvider",
    "SecretProviderProtocol",
    # Connection & Transaction
    "Connection",
    "Transaction",
    "TransactionOptions",
    "IsolationLevel",
    # Capability reporting
    "CapabilityReport",
    "TypeSupport",
    "TypeSupportLevel",
    "Capability",
    "EngineVersion",
    # Compilation
    "CompileOptions",
    "DDLArtifact",
    "PlanDocument",
    # Execution
    "ResultSet",
    "ResultColumn",
    "ResultRow",
    "AdapterError",
    "AdapterErrorCode",
    # Protocol
    "Adapter",
    "DocumentAdapter",
    "KeyValueAdapter",
    # NoSQL execution result types
    "DocumentResult",
    "KeyValueResult",
    # NoSQL capability keys
    "CAP_AGGREGATION_FRAMEWORK",
    "CAP_DOCUMENT_SCHEMA_VALIDATION",
    "CAP_KEY_EXPIRATION",
    "CAP_KEY_SPACE_NOTIFICATIONS",
    "CAP_TRANSACTIONS_MULTI_DOC",
    "CAP_CHANGE_STREAMS",
    "CAP_NESTED_DOCUMENTS",
    "CAP_DOCUMENT_CRUD",
    "CAP_KEY_VALUE_STRINGS",
    "CAP_KEY_VALUE_HASHES",
    "CAP_KEY_VALUE_LISTS",
    "CAP_KEY_VALUE_SETS",
    "CAP_KEY_VALUE_SORTED_SETS",
    "CAP_KEY_INSPECTION",
    "CAP_SAFE_COMMAND_EXECUTION",
    # Standard capability keys
    "CAP_TRANSACTIONS",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Database connection configuration (non-secret fields only).

    Secrets (passwords, certificates, tokens) are provided via a SecretProvider
    at connection time, never embedded in this object.
    """

    adapter: str
    """Adapter id (e.g., 'postgres', 'sqlite', 'mysql')."""

    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    """Database username (not a secret)."""

    # SQLite-specific
    path: str | None = None
    """Path to SQLite database file."""

    # TLS/SSL
    sslmode: str | None = None
    """SSL mode: 'disable', 'require', 'verify-ca', 'verify-full', etc."""

    sslcert: str | None = None
    sslkey: str | None = None
    sslrootcert: str | None = None

    # Engine-specific extra options
    options: dict[str, str] = field(default_factory=dict)
    """Additional driver-specific options (non-secret)."""

    def __post_init__(self) -> None:
        if not self.adapter:
            raise ValueError("adapter is required")
        if self.adapter == "sqlite" and not self.path:
            raise ValueError("SQLite connection requires 'path'")
        if self.adapter in ("postgres", "mysql") and not self.host:
            raise ValueError(f"{self.adapter} connection requires 'host'")


class SecretProviderProtocol(Protocol):
    """Protocol for secret providers."""

    def get(self, key: str) -> str | None: ...


class SecretProvider:
    """Provides secrets for connection configuration.

    Secrets are never logged, printed, or included in error messages.
    Implementations may read from environment variables, secret managers,
    keyring, or prompt interactively.
    """

    def __init__(self, env_prefix: str = "OQ_") -> None:
        self.env_prefix = env_prefix

    def get(self, key: str) -> str | None:
        """Get a secret by key.

        Keys are adapter-specific but conventionally:
        - 'password' or '<connection_name>_password' for database passwords
        - 'sslcert', 'sslkey', 'sslrootcert' for certificate paths
        """
        import os

        return os.environ.get(f"{self.env_prefix}{key.upper()}")


# ---------------------------------------------------------------------------
# Connection & Transaction
# ---------------------------------------------------------------------------


class IsolationLevel(Enum):
    """SQL standard isolation levels."""

    READ_UNCOMMITTED = "read_uncommitted"
    READ_COMMITTED = "read_committed"
    REPEATABLE_READ = "repeatable_read"
    SERIALIZABLE = "serializable"


@dataclass(frozen=True, slots=True)
class TransactionOptions:
    """Options for beginning a transaction."""

    isolation: IsolationLevel = IsolationLevel.READ_COMMITTED
    read_only: bool = False
    deferrable: bool = False


class Connection(ABC):
    """An open database connection.

    Connections are obtained via `Adapter.connect()` and must be closed
    with `Adapter.close()`.
    """

    @property
    @abstractmethod
    def config(self) -> ConnectionConfig:
        """The connection configuration (without secrets)."""
        ...

    @property
    @abstractmethod
    def is_closed(self) -> bool:
        """Whether the connection has been closed."""
        ...

    @abstractmethod
    def ping(self) -> bool:
        """Check if the connection is alive."""
        ...


class Transaction(ABC):
    """A database transaction.

    Obtained via `Adapter.begin()`. Must be committed or rolled back.
    """

    @property
    @abstractmethod
    def connection(self) -> Connection:
        """The parent connection."""
        ...

    @property
    @abstractmethod
    def is_active(self) -> bool:
        """Whether the transaction is still open."""
        ...


# ---------------------------------------------------------------------------
# Capability Reporting
# ---------------------------------------------------------------------------


class TypeSupportLevel(Enum):
    """Level of type support in an adapter."""

    EXACT = "exact"
    """Values and constraints survive round-trip without loss."""

    COMPATIBLE = "compatible"
    """Values survive, constraints may differ but don't change results."""

    APPROXIMATE = "approximate"
    """Some values or guarantees are lost. Participates in conversion reporting."""

    UNSUPPORTED = "unsupported"
    """Type cannot be represented."""


@dataclass(frozen=True, slots=True)
class TypeSupport:
    """Type support information for a universal type."""

    support: TypeSupportLevel
    native: str | None
    """Native type name (e.g., 'bigint', 'jsonb')."""

    max_precision: int | None = None
    """For decimal: maximum precision supported."""

    max_length: int | None = None
    """For text/binary: maximum length supported."""

    dimension: int | None = None
    """For vector: fixed dimension."""

    range: str | None = None
    """For integer: min..max as string."""

    notes: str | None = None
    """Human-readable notes on differences or limitations."""


@dataclass(frozen=True, slots=True)
class Capability:
    """A single capability flag."""

    supported: bool
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class EngineVersion:
    """Detected engine version information."""

    product: str
    """Product name (e.g., 'PostgreSQL', 'SQLite', 'MySQL')."""

    version: str
    """Full version string (e.g., '16.2', '3.45.1', '8.4.0')."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.product} {self.version}"


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """Complete capability report for an adapter + engine combination.

    This is the machine-readable source of truth for what an adapter
    actually supports on a specific engine version. Generated from the
    same probes used by integration tests so it cannot drift.
    """

    adapter: str
    """Adapter id (e.g., 'postgres')."""

    adapter_version: str
    """Package version of the adapter."""

    engine: str
    """Engine product name (e.g., 'PostgreSQL')."""

    tested_versions: list[str]
    """Engine versions this report was validated against."""

    capabilities: dict[str, Capability]
    """Engine capability flags."""

    types: dict[str, TypeSupport]
    """Per-type support details."""

    limitations: list[str]
    """Known limitations not covered by capability flags."""

    generated_at: datetime
    """When this report was generated (UTC)."""


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompileOptions:
    """Options controlling schema/query compilation."""

    pretty: bool = False
    """Format output for readability."""

    quote_identifiers: bool = True
    """Quote all identifiers according to engine rules."""

    target_version: str | None = None
    """Target engine version for version-specific features."""


@dataclass(frozen=True, slots=True)
class DDLArtifact:
    """A single DDL statement artifact."""

    sql: str
    """The DDL statement."""

    statement_type: str
    """Type: 'create_table', 'create_index', 'alter_table', etc."""

    entity: str | None = None
    """Associated entity/table name if applicable."""

    depends_on: list[str] = field(default_factory=list)
    """Other artifact ids this depends on (for ordering)."""


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResultColumn:
    """Metadata for a result column."""

    name: str
    type: Any  # Will be oq.types.TypeExpr once available
    nullable: bool = True
    """Whether the column can contain null."""


@dataclass(frozen=True, slots=True)
class ResultRow:
    """A single result row as a tuple of values.

    Values are normalized to universal types:
    - decimals as strings or decimal.Decimal
    - timestamps as timezone-aware datetimes (or naive if source has no zone)
    - binary as bytes
    - JSON as parsed Python values
    """

    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ResultSet:
    """Normalized query result set."""

    columns: list[ResultColumn]
    rows: list[ResultRow]
    affected_rows: int | None = None
    """For mutations: number of rows affected. None for reads."""

    warnings: list[str] = field(default_factory=list)
    """Engine-specific warnings, normalized to strings."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdapterErrorCode(Enum):
    """Stable adapter error codes."""

    CONNECTION = "OQ-ADAPTER-CONNECTION"
    """Connection failed or lost."""

    AUTH = "OQ-ADAPTER-AUTH"
    """Authentication/authorization rejected."""

    CAPABILITY = "OQ-ADAPTER-CAPABILITY"
    """Plan requires an unsupported capability."""

    CONSTRAINT = "OQ-ADAPTER-CONSTRAINT"
    """Unique/foreign-key/check violation."""

    TIMEOUT = "OQ-ADAPTER-TIMEOUT"
    """Statement timeout."""

    CANCELLED = "OQ-ADAPTER-CANCELLED"
    """Cancelled by user or runtime."""

    SYNTAX = "OQ-ADAPTER-SYNTAX"
    """Generated artifact rejected by engine (adapter bug)."""

    DATA = "OQ-ADAPTER-DATA"
    """Value conversion failure."""

    UNSUPPORTED = "OQ-ADAPTER-UNSUPPORTED"
    """Engine/version does not support the operation."""


class AdapterError(Exception):
    """Base exception for adapter errors."""

    def __init__(
        self,
        code: AdapterErrorCode,
        message: str,
        *,
        retryable: bool = False,
        cause: BaseException | None = None,
        redacted_cause: str | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.cause = cause
        self.redacted_cause = redacted_cause
        """Redacted version of the driver exception for logging."""
        self.diagnostics = diagnostics or []
        """Structured findings attached to the error, when available."""


def redact_message(message: str) -> str:
    """Redact sensitive information from error messages.

    This is a baseline redaction; adapters should override with
    engine-specific patterns.
    """
    import re

    # Redact potential connection strings, passwords, tokens
    message = re.sub(r"password\s*=\s*\S+", "password=***", message, flags=re.IGNORECASE)
    message = re.sub(r"pwd\s*=\s*\S+", "pwd=***", message, flags=re.IGNORECASE)
    message = re.sub(r"token\s*=\s*\S+", "token=***", message, flags=re.IGNORECASE)
    message = re.sub(r"postgresql://[^:]+:[^@]+@", "postgresql://***:***@", message)
    message = re.sub(r"mysql://[^:]+:[^@]+@", "mysql://***:***@", message)
    return message


# ---------------------------------------------------------------------------
# Adapter Protocol
# ---------------------------------------------------------------------------


class Adapter(ABC):
    """Base protocol for all Querly database adapters.

    Adapters are discovered via the `oq.adapters` entry point group.
    The adapter id is the entry point name (e.g., 'postgres', 'sqlite', 'mysql').
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Adapter identifier (lower-case ASCII, stable)."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g., 'PostgreSQL')."""
        ...

    @property
    @abstractmethod
    def package_version(self) -> str:
        """Package version of this adapter distribution."""
        ...

    @abstractmethod
    def capabilities(self) -> CapabilityReport:
        """Return the capability report for this adapter.

        This is a static report based on the adapter's implementation,
        not a live probe. Live probing happens in `detect_version()`.
        """
        ...

    @abstractmethod
    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        """Open a new connection to the database.

        Args:
            config: Connection configuration (non-secret fields).
            secrets: Provider for secrets (passwords, certificates).

        Returns:
            A new Connection object.

        Raises:
            AdapterError: On connection failure (OQ-ADAPTER-CONNECTION),
                          auth failure (OQ-ADAPTER-AUTH).
        """
        ...

    @abstractmethod
    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        """Inspect an existing database schema into universal schema IR.

        Must be read-only. Must not acquire write locks.
        On engines where catalog reads can block, use read-only transactions.

        Returns:
            SchemaDocument with all inspectable objects.

        Raises:
            AdapterError: On inspection failure.
        """
        ...

    @abstractmethod
    def compile_schema(self, schema: SchemaDocument, options: CompileOptions) -> list[DDLArtifact]:
        """Compile universal schema IR into engine DDL artifacts.

        Must be pure with respect to the database: no DDL execution.
        May use a live connection for catalog introspection only when
        explicitly passed an inspection handle (documented per-adapter).

        Returns:
            List of DDLArtifact in dependency order.

        Raises:
            AdapterError: On compilation failure (OQ-ADAPTER-SYNTAX if the
                          engine would reject it, OQ-ADAPTER-UNSUPPORTED if
                          a construct cannot be represented).
        """
        ...

    @abstractmethod
    def compile_query(self, query: QueryDocument, schema: SchemaDocument) -> PlanDocument:
        """Compile universal query IR into an execution plan.

        Must validate that every required capability is reported as supported.
        If not, return a plan with capability finding (do not approximate).

        Returns:
            PlanDocument with artifact, parameter binding plan, and metadata.

        Raises:
            AdapterError: On compilation failure (OQ-ADAPTER-CAPABILITY if
                          unsupported, OQ-ADAPTER-SYNTAX for adapter bugs).
        """
        ...

    @abstractmethod
    def execute(
        self,
        connection: Connection,
        plan: PlanDocument,
        params: Mapping[str, Any],
    ) -> ResultSet:
        """Execute a compiled plan with bound parameters.

        Must bind values, never interpolate them.
        Must return normalized ResultSet.
        Must not retry writes implicitly.

        Returns:
            ResultSet with columns, rows, affected_rows, warnings.

        Raises:
            AdapterError: On execution failure with appropriate code.
        """
        ...

    @abstractmethod
    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        """Begin a new transaction.

        Returns:
            Transaction object.

        Raises:
            AdapterError: If transactions not supported (OQ-ADAPTER-CAPABILITY).
        """
        ...

    @abstractmethod
    def commit(self, transaction: Transaction) -> None:
        """Commit a transaction.

        Raises:
            AdapterError: On commit failure.
        """
        ...

    @abstractmethod
    def rollback(self, transaction: Transaction) -> None:
        """Roll back a transaction.

        Must not raise on rollback failure; log and suppress.
        """
        ...

    @abstractmethod
    def detect_version(self, connection: Connection) -> EngineVersion:
        """Detect the engine version from a live connection.

        Used to generate accurate capability reports and for version-
        specific compilation.

        Returns:
            EngineVersion with parsed version components.
        """
        ...

    @abstractmethod
    def close(self, connection: Connection) -> None:
        """Close a connection and release resources.

        Must be idempotent and must not raise.
        """
        ...


# ---------------------------------------------------------------------------
# Document-store adapter protocol (Phase 5: NoSQL)
# ---------------------------------------------------------------------------


class DocumentAdapter(ABC):
    """Protocol for document-store adapters (e.g., MongoDB).

    Document stores adapt the universal IR to a document data model:
    collections instead of tables, documents instead of rows, nested
    objects instead of normalized relations, BSON types instead of SQL
    types. Redis keys are not document stores.
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Adapter identifier (lower-case ASCII, stable)."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name."""
        ...

    @property
    @abstractmethod
    def package_version(self) -> str:
        """Package version of this adapter distribution."""
        ...

    @abstractmethod
    def capabilities(self) -> CapabilityReport:
        """Return the capability report for this document adapter."""
        ...

    @abstractmethod
    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        """Open a new connection to the document store."""
        ...

    @abstractmethod
    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        """Inspect an existing database into universal document-store schema IR.

        Returns:
            DocumentStoreSchema with collections, fields, and indexes.

        Raises:
            AdapterError: On inspection failure.
        """
        ...

    @abstractmethod
    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        """Compile universal document-store schema IR into engine artifacts.

        Returns:
            List of artifacts (create_collection, create_index, etc.).

        Raises:
            AdapterError: On compilation failure.
        """
        ...

    @abstractmethod
    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        """Compile universal document query into an execution plan.

        Must validate that every required capability is reported as supported.
        If not, return a plan with capability finding (do not approximate).
        """
        ...

    @abstractmethod
    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> DocumentResult:
        """Execute a compiled document query with bound parameters.

        Returns:
            DocumentResult with documents, affected count, and warnings.
        """
        ...

    @abstractmethod
    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        """Begin a new transaction/multi-document session."""
        ...

    @abstractmethod
    def commit(self, transaction: Transaction) -> None:
        """Commit a transaction/multi-document session."""
        ...

    @abstractmethod
    def rollback(self, transaction: Transaction) -> None:
        """Roll back a transaction/multi-document session."""
        ...

    @abstractmethod
    def detect_version(self, connection: Connection) -> EngineVersion:
        """Detect the engine version from a live connection."""
        ...

    @abstractmethod
    def close(self, connection: Connection) -> None:
        """Close a connection and release resources.

        Must be idempotent and must not raise.
        """
        ...


# ---------------------------------------------------------------------------
# Key-value adapter protocol (Phase 5: NoSQL)
# ---------------------------------------------------------------------------


class KeyValueAdapter(ABC):
    """Protocol for key-value adapters (e.g., Redis).

    Key-value stores adapt the universal IR to a key-space data model:
    keys with data types instead of tables, commands instead of queries,
    TTLs instead of timestamps. Document stores are not key-value stores.
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Adapter identifier (lower-case ASCII, stable)."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name."""
        ...

    @property
    @abstractmethod
    def package_version(self) -> str:
        """Package version of this adapter distribution."""
        ...

    @abstractmethod
    def capabilities(self) -> CapabilityReport:
        """Return the capability report for this key-value adapter."""
        ...

    @abstractmethod
    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        """Open a new connection to the key-value store."""
        ...

    @abstractmethod
    def inspect_schema(self, connection: Connection) -> KeySpaceSchema:
        """Inspect an existing key space into universal key-value schema IR.

        Returns:
            KeySpaceSchema with key specs, data types, and TTLs.

        Raises:
            AdapterError: On inspection failure.
        """
        ...

    @abstractmethod
    def compile_schema(self, schema: KeySpaceSchema, options: CompileOptions) -> list[DDLArtifact]:
        """Compile universal key-value schema IR into engine artifacts.

        Returns:
            List of artifacts.

        Raises:
            AdapterError: On compilation failure.
        """
        ...

    @abstractmethod
    def compile_query(self, query: KeyValueQuery, schema: KeySpaceSchema) -> PlanDocument:
        """Compile universal key-value query into an execution plan.

        Must validate that every required capability is reported as supported.
        If not, return a plan with capability finding (do not approximate).
        """
        ...

    @abstractmethod
    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> KeyValueResult:
        """Execute a compiled key-value query with bound parameters.

        Returns:
            KeyValueResult with key results, affected count, and warnings.
        """
        ...

    @abstractmethod
    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        """Begin a new transaction/MULTI block."""
        ...

    @abstractmethod
    def commit(self, transaction: Transaction) -> None:
        """Commit a transaction/MULTI block."""
        ...

    @abstractmethod
    def rollback(self, transaction: Transaction) -> None:
        """Roll back a transaction/MULTI block (DISCARD)."""
        ...

    @abstractmethod
    def detect_version(self, connection: Connection) -> EngineVersion:
        """Detect the engine version from a live connection."""
        ...

    @abstractmethod
    def close(self, connection: Connection) -> None:
        """Close a connection and release resources.

        Must be idempotent and must not raise.
        """
        ...


# ---------------------------------------------------------------------------
# NoSQL execution result types
# ---------------------------------------------------------------------------


class DocumentResult:
    """Normalized result of a document-store query execution.

    Documents are returned as ordered maps with metadata about which
    native shape produced them.
    """

    def __init__(
        self,
        documents: list[dict[str, Any]],
        affected_count: int | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.documents = documents
        self.affected_count = affected_count
        self.warnings = warnings or []


class KeyValueResult:
    """Normalized result of a key-value query execution.

    Key results are returned as ordered maps or tuples with metadata
    about which native shape produced them.
    """

    def __init__(
        self,
        results: list[dict[str, Any]],
        affected_count: int | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.results = results
        self.affected_count = affected_count
        self.warnings = warnings or []


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def create_capability_report(
    adapter_id: str,
    adapter_version: str,
    engine: str,
    tested_versions: list[str],
    capabilities: dict[str, Capability],
    types: dict[str, TypeSupport],
    limitations: list[str] | None = None,
) -> CapabilityReport:
    """Factory for creating capability reports."""
    from datetime import datetime

    return CapabilityReport(
        adapter=adapter_id,
        adapter_version=adapter_version,
        engine=engine,
        tested_versions=tested_versions,
        capabilities=capabilities,
        types=types,
        limitations=limitations or [],
        generated_at=datetime.now(UTC),
    )


# Standard capability keys (for consistency across adapters)
CAP_TRANSACTIONS = "transactions"
CAP_TRANSACTIONAL_DDL = "transactional_ddl"
CAP_RETURNING = "returning"
CAP_ORDER_BY_NULLS = "order_by_nulls"
CAP_PARTIAL_INDEXES = "partial_indexes"
CAP_VECTOR_TYPE = "vector_type"
CAP_JSON = "json"
CAP_ARRAY_TYPE = "array_type"
CAP_UUID_TYPE = "uuid_type"
CAP_GEO_TYPE = "geo_type"
CAP_DURATION_TYPE = "duration_type"
CAP_EMAIL_TYPE = "email_type"
CAP_URL_TYPE = "url_type"
CAP_DECIMAL_UNBOUNDED = "decimal_unbounded"
CAP_CHECK_CONSTRAINTS = "check_constraints"
CAP_FOREIGN_KEYS = "foreign_keys"
CAP_UNIQUE_CONSTRAINTS = "unique_constraints"
CAP_INDEXES = "indexes"
CAP_FULL_TEXT_SEARCH = "full_text_search"
CAP_MATERIALIZED_VIEWS = "materialized_views"
CAP_PARTITIONING = "partitioning"
CAP_REPLICATION = "replication"
CAP_AGGREGATION_FRAMEWORK = "aggregation_framework"
CAP_DOCUMENT_SCHEMA_VALIDATION = "document_schema_validation"
CAP_KEY_EXPIRATION = "key_expiration"
CAP_KEY_SPACE_NOTIFICATIONS = "key_space_notifications"
CAP_TRANSACTIONS_MULTI_DOC = "transactions_multi_doc"
CAP_CHANGE_STREAMS = "change_streams"

# Phase 5 NoSQL capability keys. These describe *native* data models; they are
# not aliases for relational capabilities.
CAP_NESTED_DOCUMENTS = "nested_documents"
CAP_DOCUMENT_CRUD = "document_crud"
CAP_KEY_VALUE_STRINGS = "key_value_strings"
CAP_KEY_VALUE_HASHES = "key_value_hashes"
CAP_KEY_VALUE_LISTS = "key_value_lists"
CAP_KEY_VALUE_SETS = "key_value_sets"
CAP_KEY_VALUE_SORTED_SETS = "key_value_sorted_sets"
CAP_KEY_INSPECTION = "key_inspection"
CAP_SAFE_COMMAND_EXECUTION = "safe_command_execution"

# Phase 8 capability keys. These are shared by SQL, wide-column, graph, search,
# and vector adapters where a common name is meaningful. An adapter reports every
# key (supported or not) so a missing feature is never mistaken for support.
CAP_JOINS = "joins"
CAP_GROUP_BY = "group_by"
CAP_HAVING = "having"
CAP_SUBQUERIES = "subqueries"
CAP_UNION = "union"
CAP_WINDOW_FUNCTIONS = "window_functions"
CAP_UPSERT = "upsert"
CAP_TTL = "ttl"
CAP_SECONDARY_INDEXES = "secondary_indexes"
CAP_GRAPH_TRAVERSAL = "graph_traversal"
CAP_VECTOR_SEARCH = "vector_search"
CAP_SEARCH_QUERY_DSL = "search_query_dsl"
CAP_SERVER_SIDE_SCRIPTING = "server_side_scripting"
CAP_BATCH_WRITES = "batch_writes"
CAP_STRONG_CONSISTENCY = "strong_consistency"
CAP_MULTI_KEY_ATOMICITY = "multi_key_atomicity"
CAP_SCHEMA_EVOLUTION = "schema_evolution"
CAP_UPSERT_BY_KEY = "upsert_by_key"
CAP_ANN_SEARCH = "ann_search"
CAP_HYBRID_SEARCH = "hybrid_search"
