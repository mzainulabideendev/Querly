"""Querly runtime: connections, safe execution, transactions, and security policy.

This package implements the adapter contract defined in
`docs/adapters/ADAPTER_GUIDE.md` and provides connection management,
parameterized execution, transaction control, timeouts, read-only enforcement,
audit logging, and result/error normalization.
"""

from __future__ import annotations

from oq.runtime.contract import (
    # Configuration
    ConnectionConfig,
    SecretProvider,
    # Connection & Transaction
    Connection,
    Transaction,
    TransactionOptions,
    IsolationLevel,
    # Capability reporting
    CapabilityReport,
    TypeSupport,
    TypeSupportLevel,
    Capability,
    EngineVersion,
    # Compilation
    CompileOptions,
    DDLArtifact,
    # Execution
    ResultSet,
    ResultColumn,
    ResultRow,
    # Errors
    AdapterError,
    AdapterErrorCode,
    redact_message,
    # Protocol
    Adapter,
    DocumentAdapter,
    KeyValueAdapter,
    # NoSQL execution result types
    DocumentResult,
    KeyValueResult,
    # Standard capability keys
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
    # NoSQL capability keys
    CAP_AGGREGATION_FRAMEWORK,
    CAP_DOCUMENT_SCHEMA_VALIDATION,
    CAP_KEY_EXPIRATION,
    CAP_KEY_SPACE_NOTIFICATIONS,
    CAP_TRANSACTIONS_MULTI_DOC,
    CAP_CHANGE_STREAMS,
    # Factory
    create_capability_report,
)

__all__ = [
    # Configuration
    "ConnectionConfig",
    "SecretProvider",
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
    # Execution
    "ResultSet",
    "ResultColumn",
    "ResultRow",
    # Errors
    "AdapterError",
    "AdapterErrorCode",
    "redact_message",
    # Protocol
    "Adapter",
    "DocumentAdapter",
    "KeyValueAdapter",
    # NoSQL execution result types
    "DocumentResult",
    "KeyValueResult",
    # Standard capability keys
    "CAP_TRANSACTIONS",
    "CAP_TRANSACTIONAL_DDL",
    "CAP_RETURNING",
    "CAP_ORDER_BY_NULLS",
    "CAP_PARTIAL_INDEXES",
    "CAP_VECTOR_TYPE",
    "CAP_JSON",
    "CAP_ARRAY_TYPE",
    "CAP_UUID_TYPE",
    "CAP_GEO_TYPE",
    "CAP_DURATION_TYPE",
    "CAP_EMAIL_TYPE",
    "CAP_URL_TYPE",
    "CAP_DECIMAL_UNBOUNDED",
    "CAP_CHECK_CONSTRAINTS",
    "CAP_FOREIGN_KEYS",
    "CAP_UNIQUE_CONSTRAINTS",
    "CAP_INDEXES",
    "CAP_FULL_TEXT_SEARCH",
    "CAP_MATERIALIZED_VIEWS",
    "CAP_PARTITIONING",
    "CAP_REPLICATION",
    # Factory
    "create_capability_report",
]