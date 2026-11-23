"""Comprehensive unit and security tests for Phase 7 execution runtime and CLI."""

from __future__ import annotations

import pytest
from oq.runtime.contract import ConnectionConfig, SecretProvider, AdapterError, AdapterErrorCode
from oq.runtime.connection import ParameterBinder, ConnectionManager, TransactionManager
from oq.runtime.engine import ExecutionEngine, RuntimeOptions, AuditLogger
from oq.ir import QueryDocument, PlanDocument, Artifact, Safety, TableRef


def test_parameter_binder_security() -> None:
    """Test protection against SQL injection and unsafe identifiers."""
    # Valid identifier
    assert ParameterBinder.validate_identifier("users") == "users"

    # Unsafe identifiers with SQL injection vectors
    with pytest.raises(ValueError):
        ParameterBinder.validate_identifier("users; DROP TABLE users;")

    with pytest.raises(ValueError):
        ParameterBinder.validate_identifier("users -- comment")

    with pytest.raises(ValueError):
        ParameterBinder.validate_identifier("users' OR '1'='1")


def test_audit_logger_redaction() -> None:
    """Test audit logging and credential redaction."""
    logger = AuditLogger()
    logger.log(
        role="operator",
        operation="select",
        query_text="SELECT * FROM users WHERE password = 'super_secret_password'",
        status="success",
        duration_ms=12.5,
    )

    record = logger.records[0]
    assert "super_secret_password" not in record.query_text
    assert "***" in record.query_text


def test_runtime_read_only_policy() -> None:
    """Test that write/destructive operations are blocked in read-only mode."""
    conn_mgr = ConnectionManager()
    options = RuntimeOptions(read_only_mode=True, current_role="operator", allowed_roles=["operator"])
    engine = ExecutionEngine(conn_mgr, options=options)

    plan = PlanDocument(
        target_adapter="sqlite",
        operation="update",
        artifact=Artifact(text="UPDATE users SET status = 'active'"),
        safety=Safety(classification="write"),
    )

    with pytest.raises(AdapterError) as exc_info:
        engine.execute("default", plan, {})
    assert exc_info.value.code == AdapterErrorCode.AUTH
    assert "read-only mode" in str(exc_info.value)


def test_runtime_role_authorization() -> None:
    """Test unauthorized role execution block."""
    conn_mgr = ConnectionManager()
    options = RuntimeOptions(current_role="guest", allowed_roles=["admin", "operator"])
    engine = ExecutionEngine(conn_mgr, options=options)

    plan = PlanDocument(
        target_adapter="sqlite",
        operation="select",
        artifact=Artifact(text="SELECT * FROM users"),
    )

    with pytest.raises(AdapterError) as exc_info:
        engine.execute("default", plan, {})
    assert exc_info.value.code == AdapterErrorCode.AUTH
    assert "not authorized" in str(exc_info.value)
