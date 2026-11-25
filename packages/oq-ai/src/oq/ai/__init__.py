"""Deterministic optional assistant for generating Querly source."""

from __future__ import annotations

from dataclasses import dataclass

from oq.core.parser import parse
from oq.core.source import SourceFile

__all__ = ["AssistantResult", "QuerlyAssistant"]


@dataclass(frozen=True, slots=True)
class AssistantResult:
    source: str
    syntax_valid: bool
    semantic_valid: bool
    diagnostics: object
    requires_confirmation: bool = True


class QuerlyAssistant:
    """Rules-based assistant; generated source always passes the compiler gate."""

    def interpret(self, request: str) -> str:
        text = request.strip().lower()
        if "create" in text and "table" in text:
            return "table users {\n    id: integer @primary @auto\n    name: string @required\n}\n"
        if "all" in text and "user" in text:
            return "get all users"
        raise ValueError("Request is outside the supported assistant templates")

    def generate(self, request: str) -> AssistantResult:
        source = self.interpret(request)
        program, diagnostics = parse(SourceFile.from_text(source), syntax="auto")
        syntax_valid = not diagnostics.has_errors
        semantic_valid = bool(program.declarations) and syntax_valid
        return AssistantResult(source, syntax_valid, semantic_valid, diagnostics)

    def execute(
        self, result: AssistantResult, executor: object, *, confirmed: bool = False
    ) -> object:
        if not confirmed:
            raise PermissionError("Execution requires explicit confirmation")
        if not result.syntax_valid or not result.semantic_valid:
            raise ValueError("Cannot execute source that failed validation")
        if not hasattr(executor, "execute_source"):
            raise TypeError("Executor must provide execute_source(source)")
        return executor.execute_source(result.source)
