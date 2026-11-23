"""Querly runtime: connections, safe execution, transactions, and security policy.

This package implements the adapter contract defined in
`docs/adapters/ADAPTER_GUIDE.md` and provides connection management,
parameterized execution, transaction control, timeouts, read-only enforcement,
audit logging, and result/error normalization.
"""

from __future__ import annotations

from oq.runtime.contract import (
    # NoSQL capability keys
    CAP_AGGREGATION_FRAMEWORK,
    CAP_ARRAY_TYPE,
    CAP_CHANGE_STREAMS,
    CAP_CHECK_CONSTRAINTS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DOCUMENT_CRUD,
    CAP_DOCUMENT_SCHEMA_VALIDATION,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_INDEXES,
    CAP_JSON,
    CAP_KEY_EXPIRATION,
    CAP_KEY_INSPECTION,
    CAP_KEY_SPACE_NOTIFICATIONS,
    CAP_KEY_VALUE_HASHES,
    CAP_KEY_VALUE_LISTS,
    CAP_KEY_VALUE_SETS,
    CAP_KEY_VALUE_SORTED_SETS,
    CAP_KEY_VALUE_STRINGS,
    CAP_MATERIALIZED_VIEWS,
    CAP_NESTED_DOCUMENTS,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_SAFE_COMMAND_EXECUTION,
    CAP_TRANSACTIONAL_DDL,
    # Standard capability keys
    CAP_TRANSACTIONS,
    CAP_TRANSACTIONS_MULTI_DOC,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_TYPE,
    # Protocol
    Adapter,
    # Errors
    AdapterError,
    AdapterErrorCode,
    Capability,
    # Capability reporting
    CapabilityReport,
    # Compilation
    CompileOptions,
    # Connection & Transaction
    Connection,
    # Configuration
    ConnectionConfig,
    DDLArtifact,
    DocumentAdapter,
    # NoSQL execution result types
    DocumentResult,
    EngineVersion,
    IsolationLevel,
    KeyValueAdapter,
    KeyValueResult,
    ResultColumn,
    ResultRow,
    # Execution
    ResultSet,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    # Factory
    create_capability_report,
    redact_message,
)

__all__ = [  # noqa: RUF022 - grouped for readability
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
    # Factory
    "create_capability_report",
]
