"""Shared helpers for Querly adapters. No database driver lives here."""

from __future__ import annotations

from oq_adapters.kit.common import (
    CAPABILITY_KEYS,
    build_report,
    diagnostic,
    format_default,
    parameter_type_hint,
    quote_identifier,
    type_attr,
    type_kind,
    validate_identifier,
)
from oq_adapters.kit.cql import (
    CqlCompileResult,
    CqlSchemaResult,
    compile_cql_schema,
    compile_cql_select,
    cql_type,
    quote_cql,
)
from oq_adapters.kit.document import (
    document_safety,
    json_safe,
    normalize_document,
    unsupported_document_plan,
)
from oq_adapters.kit.sql import (
    SqlCompileResult,
    SqlDialect,
    compile_schema_sql,
    compile_select_sql,
)

__all__ = [
    "CAPABILITY_KEYS",
    "CqlCompileResult",
    "CqlSchemaResult",
    "SqlCompileResult",
    "SqlDialect",
    "build_report",
    "compile_cql_schema",
    "compile_cql_select",
    "compile_schema_sql",
    "compile_select_sql",
    "cql_type",
    "diagnostic",
    "document_safety",
    "format_default",
    "json_safe",
    "normalize_document",
    "parameter_type_hint",
    "quote_cql",
    "quote_identifier",
    "type_attr",
    "type_kind",
    "unsupported_document_plan",
    "validate_identifier",
]
