"""Driver-free helpers shared by Querly adapters."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from oq.ir import AdapterDiagnostic
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
    AdapterErrorCode,
    CapabilityReport,
    TypeSupport,
)

CAPABILITY_KEYS: tuple[str, ...] = (
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
"""Capability keys every adapter must answer, supported or not."""


def type_kind(type_obj: Any) -> str:
    """Return the canonical kind of a Type object or dict."""

    if type_obj is None:
        return "null"
    if isinstance(type_obj, dict):
        return str(type_obj.get("kind", "null"))
    return str(getattr(type_obj, "kind", "null"))


def type_attr(type_obj: Any, name: str, default: Any = None) -> Any:
    """Read an optional Type attribute from a model or dict."""

    if type_obj is None:
        return default
    if isinstance(type_obj, dict):
        return type_obj.get(name, default)
    return getattr(type_obj, name, default)


def validate_identifier(name: str, *, max_length: int | None = None) -> None:
    """Reject identifiers that cannot be safely quoted.

    Quoting is not a licence to accept control characters or NUL bytes.
    """

    if not isinstance(name, str) or not name:
        raise ValueError("identifier must be a non-empty string")
    if "\x00" in name:
        raise ValueError("identifier contains a NUL byte")
    if any(ord(char) < 0x20 for char in name):
        raise ValueError("identifier contains a control character")
    if max_length is not None and len(name) > max_length:
        raise ValueError(f"identifier is longer than {max_length} characters")


def quote_identifier(
    name: str,
    *,
    open_quote: str = '"',
    close_quote: str | None = None,
    max_length: int | None = None,
) -> str:
    """Quote an identifier, doubling the closing quote.

    ``open_quote``/``close_quote`` cover double quotes, backticks, and SQL
    Server brackets. A bracketed identifier escapes ``]`` as ``]]``.
    """

    close = close_quote if close_quote is not None else open_quote
    validate_identifier(name, max_length=max_length)
    escaped = name.replace(close, close + close)
    return f"{open_quote}{escaped}{close}"


def format_default(value: Any, *, kind: str | None = None) -> str:
    """Render a schema default as a SQL literal.

    This is used only for DDL defaults, never for query values. Strings escape
    single quotes; containers and arbitrary objects are rejected rather than
    stringified.
    """

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat(sep=' ')}'"
    if isinstance(value, (date, time)):
        return f"'{value.isoformat()}'"
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("default value contains a NUL byte")
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"cannot render a default of type {type(value).__name__}")


def parameter_type_hint(type_obj: Any) -> str:
    """Return a portable type hint for a bound parameter."""

    kind = type_kind(type_obj)
    return kind


def diagnostic(
    code: AdapterErrorCode | str,
    message: str,
    *,
    operation: str | None = None,
    feature: str | None = None,
    path: str | None = None,
    hint: str | None = None,
    severity: str = "error",
    details: dict[str, Any] | None = None,
) -> AdapterDiagnostic:
    """Build a structured adapter diagnostic with stable fields."""

    return AdapterDiagnostic(
        code=code.value if isinstance(code, AdapterErrorCode) else code,
        severity=severity,
        message=message,
        operation=operation,
        feature=feature,
        path=path,
        hint=hint,
        details=details or {},
    )


def build_report(
    *,
    adapter: str,
    adapter_version: str,
    engine: str,
    tested_versions: list[str],
    capabilities: dict[str, Any],
    types: dict[str, TypeSupport],
    limitations: list[str],
    generated_at: datetime | None = None,
) -> CapabilityReport:
    """Build a capability report and enforce completeness.

    Every standard capability key must be present. A missing key is a defect:
    callers must be able to distinguish "no" from "not answered".
    """

    missing = [key for key in CAPABILITY_KEYS if key not in capabilities]
    if missing:
        raise ValueError(f"capability report for {adapter!r} omits: {', '.join(missing)}")
    from datetime import UTC

    return CapabilityReport(
        adapter=adapter,
        adapter_version=adapter_version,
        engine=engine,
        tested_versions=list(tested_versions),
        capabilities=capabilities,
        types=types,
        limitations=list(limitations),
        generated_at=generated_at or datetime.now(UTC),
    )
