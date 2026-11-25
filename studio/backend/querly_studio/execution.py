"""Real query execution through the Querly runtime adapter contract.

Execution follows the same path as the CLI runtime:

    compiled plan artifact -> adapter.execute(connection, plan, params) -> ResultSet

Studio adds the safety controls the IDE needs: result limits, a wall-clock
timeout using SQLite's progress handler, read-only enforcement, destructive
statement confirmation, and JSON-safe value conversion.  The service never
reports success unless the adapter actually returned a result.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from oq.ir import Artifact, PlanDocument, Safety
from oq.runtime.contract import AdapterError, ResultSet

from .connections import ConnectionService, LiveConnection
from .metadata import _quote

READ_KINDS = frozenset({"select"})
WRITE_KINDS = frozenset({"insert", "update", "delete"})
DDL_KINDS = frozenset({"create_table", "create_index", "drop_table", "alter_table"})


class ExecutionError(RuntimeError):
    """Raised when execution is refused or the adapter fails."""

    def __init__(self, message: str, *, code: str = "OQ-STUDIO-EXEC") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class StatementExecution:
    id: str
    kind: str
    sql: str
    ok: bool
    duration_ms: float
    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    affected_rows: int | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "sql": self.sql,
            "ok": self.ok,
            "durationMs": round(self.duration_ms, 3),
            "columns": self.columns,
            "rows": self.rows,
            "rowCount": self.row_count,
            "affectedRows": self.affected_rows,
            "truncated": self.truncated,
            "warnings": self.warnings,
            "error": self.error,
            "errorCode": self.error_code,
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return "base64:" + base64.b64encode(payload).decode("ascii")
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _infer_type(values: list[Any]) -> str:
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            seen.add("boolean")
        elif isinstance(value, int):
            seen.add("integer")
        elif isinstance(value, float):
            seen.add("float")
        elif isinstance(value, decimal.Decimal):
            seen.add("decimal")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            seen.add("binary")
        elif isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            seen.add("timestamp")
        else:
            seen.add("text")
        if len(seen) > 1:
            return "mixed"
    return next(iter(seen), "unknown")


def _columns_from_result(result: ResultSet, rows: list[list[Any]]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for index, column in enumerate(result.columns):
        samples = [row[index] for row in rows if index < len(row)]
        inferred = _infer_type(samples)
        declared = getattr(column, "type", None)
        columns.append(
            {
                "name": column.name,
                "type": inferred if not declared else str(declared),
                "inferredType": inferred,
                "nullable": all(value is None for value in samples) or bool(
                    getattr(column, "nullable", True)
                ),
            }
        )
    return columns


def _progress_guard(raw: sqlite3.Connection, deadline: float) -> None:
    def handler() -> int:
        return 1 if time.perf_counter() > deadline else 0

    raw.set_progress_handler(handler, 1000)


def _clear_progress_guard(raw: sqlite3.Connection) -> None:
    raw.set_progress_handler(None, 0)


@dataclass(slots=True)
class RunRequest:
    connection_id: str
    statements: list[Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    max_rows: int = 500
    timeout_ms: int = 15000
    read_only: bool | None = None
    confirm_destructive: bool = False


@dataclass(slots=True)
class RunResult:
    ok: bool
    connection_id: str
    read_only: bool
    statements: list[StatementExecution]
    total_duration_ms: float
    refused: bool = False
    refusal_reason: str | None = None
    needs_confirmation: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "connectionId": self.connection_id,
            "readOnly": self.read_only,
            "statements": [statement.public() for statement in self.statements],
            "totalDurationMs": round(self.total_duration_ms, 3),
            "refused": self.refused,
            "refusalReason": self.refusal_reason,
            "needsConfirmation": self.needs_confirmation,
        }


class ExecutionService:
    def __init__(self, connections: ConnectionService) -> None:
        self.connections = connections

    def run(self, request: RunRequest) -> RunResult:
        started = time.perf_counter()
        live = self.connections.get_live(request.connection_id)
        read_only = live.read_only if request.read_only is None else bool(request.read_only)

        if not request.statements:
            raise ExecutionError("there are no compiled statements to run", code="OQ-STUDIO-EMPTY")

        dangerous = [
            statement
            for statement in request.statements
            if getattr(statement, "kind", "") in DDL_KINDS
            or getattr(getattr(statement, "safety", None), "destructive", False)
            or getattr(getattr(statement, "safety", None), "classification", "") == "ddl"
        ]
        if read_only and any(
            getattr(statement, "kind", "") not in READ_KINDS for statement in request.statements
        ):
            return RunResult(
                ok=False,
                connection_id=request.connection_id,
                read_only=True,
                statements=[],
                total_duration_ms=(time.perf_counter() - started) * 1000,
                refused=True,
                refusal_reason=(
                    "the connection or request is read-only; the program contains a "
                    "write or DDL statement"
                ),
            )

        if dangerous and not request.confirm_destructive:
            return RunResult(
                ok=False,
                connection_id=request.connection_id,
                read_only=read_only,
                statements=[],
                total_duration_ms=(time.perf_counter() - started) * 1000,
                needs_confirmation=True,
                refusal_reason=(
                    "the program contains schema changes or destructive statements; "
                    "confirm the run to continue"
                ),
            )

        raw = getattr(live.connection, "raw_connection", None)
        if isinstance(raw, sqlite3.Connection) and read_only:
            raw.execute("PRAGMA query_only = ON")

        results: list[StatementExecution] = []
        try:
            for statement in request.statements:
                results.append(self._execute_statement(live, statement, request))
        finally:
            if isinstance(raw, sqlite3.Connection):
                try:
                    raw.execute("PRAGMA query_only = OFF")
                except sqlite3.Error:
                    pass

        ok = all(item.ok for item in results)
        return RunResult(
            ok=ok,
            connection_id=request.connection_id,
            read_only=read_only,
            statements=results,
            total_duration_ms=(time.perf_counter() - started) * 1000,
        )

    # ------------------------------------------------------------------

    def _execute_statement(
        self,
        live: LiveConnection,
        statement: Any,
        request: RunRequest,
    ) -> StatementExecution:
        started = time.perf_counter()
        statement_id = getattr(statement, "id", "stmt")
        kind = getattr(statement, "kind", "select")
        sql = getattr(statement, "sql", "")

        safety = getattr(statement, "safety", None) or Safety()
        plan = PlanDocument(
            target_adapter=live.adapter_id,
            operation=kind,
            artifact=Artifact(
                kind="sql",
                text=sql,
                dialect=live.adapter_id,
            ),
            parameters=[
                {"name": item.get("name"), "index": item.get("index")}
                for item in getattr(statement, "parameters", []) or []
            ],
            safety=safety,
            limits={"max_rows": request.max_rows, "timeout_ms": request.timeout_ms},
        )

        bound = self._bind_parameters(statement, request.parameters)

        raw = getattr(live.connection, "raw_connection", None)
        deadline = time.perf_counter() + max(1, request.timeout_ms) / 1000
        if isinstance(raw, sqlite3.Connection):
            _progress_guard(raw, deadline)
        try:
            result = live.adapter.execute(live.connection, plan, bound)
        except AdapterError as exc:
            return StatementExecution(
                id=statement_id,
                kind=kind,
                sql=sql,
                ok=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(exc),
                error_code=getattr(exc.code, "value", str(exc.code)),
            )
        except sqlite3.OperationalError as exc:
            message = str(exc)
            timeout = "interrupted" in message.lower()
            return StatementExecution(
                id=statement_id,
                kind=kind,
                sql=sql,
                ok=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=(
                    f"query exceeded the {request.timeout_ms} ms timeout"
                    if timeout
                    else message
                ),
                error_code="OQ-STUDIO-TIMEOUT" if timeout else "OQ-STUDIO-SQLITE",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return StatementExecution(
                id=statement_id,
                kind=kind,
                sql=sql,
                ok=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(exc),
                error_code="OQ-STUDIO-EXEC",
            )
        finally:
            if isinstance(raw, sqlite3.Connection):
                _clear_progress_guard(raw)

        raw_rows = [list(row.values) for row in result.rows]
        truncated = len(raw_rows) > request.max_rows
        visible = raw_rows[: request.max_rows]
        json_rows = [[_json_safe(value) for value in row] for row in visible]
        columns = _columns_from_result(result, raw_rows)

        warnings = list(result.warnings)
        if truncated:
            warnings.append(
                f"result limited to {request.max_rows} rows; more rows were produced by the engine"
            )

        return StatementExecution(
            id=statement_id,
            kind=kind,
            sql=sql,
            ok=True,
            duration_ms=(time.perf_counter() - started) * 1000,
            columns=columns,
            rows=json_rows,
            row_count=len(visible),
            affected_rows=result.affected_rows,
            truncated=truncated,
            warnings=warnings,
        )

    @staticmethod
    def _bind_parameters(statement: Any, supplied: dict[str, Any]) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for item in getattr(statement, "parameters", []) or []:
            index = item.get("index", 0)
            name = item.get("name")
            if name in supplied:
                bound[index] = supplied[name]
            elif item.get("source") == "literal":
                values = getattr(statement, "parameter_values", []) or []
                bound[index] = values[index] if index < len(values) else None
            else:
                raise ExecutionError(
                    f"parameter {name!r} was not supplied",
                    code="OQ-STUDIO-PARAM",
                )
        # Preserve insertion order for adapters that bind positionally.
        return {index: bound[index] for index in sorted(bound)}


def explain_statement(live: LiveConnection, sql: str) -> dict[str, Any]:
    """Return a real engine plan for a SELECT statement.

    SQLite exposes ``EXPLAIN QUERY PLAN``; other engines are not claimed until
    their execution path is verified.  The returned rows come from the engine.
    """

    if live.adapter_id != "sqlite":
        return {
            "supported": False,
            "reason": f"EXPLAIN is only verified for SQLite in this release, not {live.adapter_id!r}",
            "rows": [],
        }
    raw = getattr(live.connection, "raw_connection", None)
    if not isinstance(raw, sqlite3.Connection):
        return {"supported": False, "reason": "no SQLite driver handle", "rows": []}
    stripped = sql.strip().rstrip(";")
    cursor = raw.cursor()
    try:
        rows = cursor.execute("EXPLAIN QUERY PLAN " + stripped).fetchall()
        plan_rows = [
            {"id": row[0], "parent": row[1], "detail": row[3] if len(row) > 3 else row[-1]}
            for row in rows
        ]
        return {"supported": True, "reason": None, "rows": plan_rows, "engine": "SQLite"}
    except sqlite3.Error as exc:
        return {"supported": False, "reason": str(exc), "rows": []}
    finally:
        cursor.close()


def sample_table(
    live: LiveConnection, table: str, *, limit: int = 100
) -> dict[str, Any]:
    """Read at most ``limit`` rows from a table for the data browser."""

    raw = getattr(live.connection, "raw_connection", None)
    if not isinstance(raw, sqlite3.Connection):
        raise ExecutionError("live table browsing requires a SQLite driver handle")
    limit = max(1, min(int(limit), 500))
    cursor = raw.cursor()
    try:
        cursor.execute(f"SELECT * FROM {_quote(table)} LIMIT {limit}")
        description = cursor.description or []
        rows = cursor.fetchall()
        columns = [
            {
                "name": item[0],
                "type": "unknown",
                "inferredType": _infer_type([row[index] for row in rows]),
                "nullable": True,
            }
            for index, item in enumerate(description)
        ]
        return {
            "table": table,
            "columns": columns,
            "rows": [[_json_safe(value) for value in row] for row in rows],
            "rowCount": len(rows),
            "limit": limit,
        }
    except sqlite3.Error as exc:
        raise ExecutionError(str(exc), code="OQ-STUDIO-SAMPLE") from exc
    finally:
        cursor.close()
