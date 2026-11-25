"""Querly Python SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oq.core.parser import parse
from oq.core.semantic import analyze
from oq.core.semantic import compile as compile_analyzed
from oq.core.source import SourceFile
from oq.ir import PlanDocument
from oq.runtime.connection import ConnectionManager
from oq.runtime.contract import ConnectionConfig, SecretProvider
from oq.runtime.engine import ExecutionEngine, RuntimeOptions

__all__ = [
    "CheckResult",
    "CompileResult",
    "ExecutionPolicy",
    "Querly",
]


@dataclass(frozen=True, slots=True)
class CheckResult:
    source: SourceFile
    ok: bool
    diagnostics: Any
    ast: Any


@dataclass(frozen=True, slots=True)
class CompileResult:
    ok: bool
    diagnostics: Any
    sql: str


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    read_only: bool = False
    max_rows: int = 10_000
    timeout_ms: int = 30_000
    allowed_roles: tuple[str, ...] = ("admin", "operator")
    current_role: str = "operator"


class Querly:
    """Type-safe embedding API for parsing, semantic validation, and compilation."""

    def __init__(self, connections: ConnectionManager | None = None) -> None:
        self.connections = connections or ConnectionManager()

    @staticmethod
    def from_project(path: str | Path = ".") -> Querly:
        project = Path(path)
        if not project.exists():
            raise FileNotFoundError(project)
        return Querly()

    def parse_source(self, text: str, *, syntax: str = "auto") -> tuple[Any, Any]:
        source = SourceFile.from_text(text)
        return parse(source, syntax=syntax)

    def check(self, source: SourceFile | str | Path, *, syntax: str = "auto") -> CheckResult:
        if isinstance(source, (str, Path)):
            try:
                source_file = SourceFile.from_path(source)
            except (OSError, UnicodeDecodeError):
                raise
        else:
            source_file = source
        ast, diagnostics = parse(source_file, syntax=syntax)
        analyzed = analyze(ast, source_file)
        diagnostics.extend(analyzed.diagnostics)
        return CheckResult(source_file, not diagnostics.has_errors, diagnostics, ast)

    def compile_source(
        self,
        source: SourceFile | str | Path,
        *,
        dialect: str = "sqlite",
        syntax: str = "auto",
    ) -> CompileResult:
        checked = self.check(source, syntax=syntax)
        if not checked.ok:
            return CompileResult(False, checked.diagnostics, "")
        analyzed = analyze(checked.ast, checked.source)
        sql, diagnostics = compile_analyzed(analyzed, dialect)
        diagnostics.extend(checked.diagnostics)
        return CompileResult(not diagnostics.has_errors, diagnostics, sql)

    def connect(
        self,
        name: str,
        config: ConnectionConfig,
        *,
        policy: ExecutionPolicy | None = None,
        secrets: SecretProvider | None = None,
    ) -> ConnectionHandle:
        self.connections.add_profile(name, config)
        return ConnectionHandle(self.connections, name, policy or ExecutionPolicy(), secrets)


class ConnectionHandle:
    def __init__(
        self,
        manager: ConnectionManager,
        profile: str,
        policy: ExecutionPolicy,
        secrets: SecretProvider | None,
    ) -> None:
        self._manager = manager
        self._profile = profile
        self._policy = policy
        self._secrets = secrets
        options = RuntimeOptions(
            max_rows=policy.max_rows,
            timeout_ms=policy.timeout_ms,
            read_only_mode=policy.read_only,
            allowed_roles=list(policy.allowed_roles),
            current_role=policy.current_role,
        )
        self._engine = ExecutionEngine(manager, options=options)

    def execute(
        self,
        plan: PlanDocument,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        if not plan.supported:
            raise ValueError("Refusing to execute an unsupported plan")
        return self._engine.execute(self._profile, plan, params or {})
