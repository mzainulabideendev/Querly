"""The Querly Studio compiler pipeline.

Stages are reported exactly as they happen:

    source -> core lexer -> parser -> semantic checks -> universal IR
           -> capability analysis -> target SQL emission

The capability stage is marked *not run* when no adapter capability report is
available.  A compile is successful only when every required stage succeeded and
the emitter produced executable units.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from oq.core.ast import (
    Between,
    BinaryOp,
    Case,
    Cast,
    ColumnRef,
    DeleteStmt,
    Expression,
    FunctionCall,
    InList,
    InsertStmt,
    IsPredicate,
    ListLiteral,
    Literal,
    Parameter,
    Program,
    Projection,
    QueryDecl,
    SelectStmt,
    Star,
    TableDecl,
    UnaryOp,
    UpdateStmt,
)
from oq.core.diagnostics import Diagnostic, DiagnosticBag, Severity
from oq.core.source import SourceError, SourceFile
from oq.core.version import version_string

from .emitter import EmittedProgram, emit_program
from .formatter import conservative_format, format_program
from .ir_builder import query_document, schema_document
from .parser import Parser

CHECK_ONLY = "check"
COMPILE = "compile"


@dataclass(slots=True)
class PipelineOptions:
    dialect: str = "sqlite"
    max_diagnostics: int = 200
    include_ir: bool = True
    capabilities: dict[str, Any] | None = None
    adapter_id: str | None = None


@dataclass(slots=True)
class Stage:
    name: str
    status: str  # success, failed, skipped, not_run
    detail: str
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "durationMs": round(self.duration_ms, 3),
        }


@dataclass(slots=True)
class CompilationResult:
    ok: bool
    syntax: str
    dialect: str
    diagnostics: list[dict[str, Any]]
    stages: list[Stage]
    statements: list[dict[str, Any]] = field(default_factory=list)
    schema_ir: dict[str, Any] | None = None
    query_ir: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    compiled: list[Any] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "syntax": self.syntax,
            "dialect": self.dialect,
            "diagnostics": self.diagnostics,
            "stages": [stage.to_dict() for stage in self.stages],
            "statements": self.statements,
            "schemaIr": self.schema_ir,
            "queryIr": self.query_ir,
            "warnings": self.warnings,
            "stats": self.stats,
            "compilerVersion": version_string(),
        }


def diagnostic_to_dict(diagnostic: Diagnostic) -> dict[str, Any]:
    span = diagnostic.span
    return {
        "code": diagnostic.code,
        "severity": diagnostic.severity.value,
        "message": diagnostic.message,
        "notes": list(diagnostic.notes),
        "line": span.start.line if span else 1,
        "column": span.start.column if span else 1,
        "endLine": span.end.line if span else 1,
        "endColumn": span.end.column if span else 1,
        "offset": span.start.offset if span else 0,
        "length": span.length if span else 0,
    }


class CompilerPipeline:
    """Pure compiler service. No database connections, no GUI state."""

    def check(self, text: str, *, file_name: str = "<memory>") -> CompilationResult:
        return self._run(text, file_name=file_name, mode=CHECK_ONLY, options=PipelineOptions())

    def compile(
        self,
        text: str,
        *,
        file_name: str = "<memory>",
        options: PipelineOptions | None = None,
    ) -> CompilationResult:
        return self._run(text, file_name=file_name, mode=COMPILE, options=options or PipelineOptions())

    # ------------------------------------------------------------------

    def _run(
        self,
        text: str,
        *,
        file_name: str,
        mode: str,
        options: PipelineOptions,
    ) -> CompilationResult:
        started = time.perf_counter()
        stages: list[Stage] = []
        diagnostics = DiagnosticBag()
        warnings: list[str] = []

        source = SourceFile.from_text(text, name=file_name)

        # Stage 1: lexing is performed by the upstream hand-written lexer.
        lex_started = time.perf_counter()
        parser = Parser(source)
        lex_duration = (time.perf_counter() - lex_started) * 1000
        lex_items = tuple(parser.diagnostics.diagnostics)
        lex_errors = [item for item in lex_items if item.is_error]
        diagnostics.extend(lex_items)
        stages.append(
            Stage(
                "Lexing",
                "failed" if lex_errors else "success",
                f"{len(parser.tokens)} tokens"
                + (f"; {len(lex_errors)} lexical error(s)" if lex_errors else ""),
                lex_duration,
            )
        )

        # Stage 2: parsing.
        parse_started = time.perf_counter()
        parse_result = parser.parse()
        parse_duration = (time.perf_counter() - parse_started) * 1000
        parse_items = tuple(
            item for item in parse_result.diagnostics.diagnostics if item not in lex_items
        )
        diagnostics.extend(parse_items)
        parse_errors = [item for item in parse_result.diagnostics if item.is_error]
        stages.append(
            Stage(
                "Parsing",
                "failed" if parse_errors else "success",
                f"{len(parse_result.program.declarations)} declaration(s); syntax: {parse_result.syntax}",
                parse_duration,
            )
        )
        program = parse_result.program

        # Stage 3: semantic validation.
        semantic_started = time.perf_counter()
        semantic_bag = self._semantic(program)
        diagnostics.extend(semantic_bag.diagnostics)
        semantic_duration = (time.perf_counter() - semantic_started) * 1000
        semantic_errors = [item for item in semantic_bag if item.is_error]
        stages.append(
            Stage(
                "Semantic analysis",
                "failed" if semantic_errors else "success",
                f"{len(semantic_bag)} finding(s)",
                semantic_duration,
            )
        )

        schema_ir = None
        query_irs: list[dict[str, Any]] = []
        if not (parse_errors or semantic_errors) or program.declarations:
            ir_started = time.perf_counter()
            try:
                schema = schema_document(program)
                if schema is not None:
                    schema_ir = schema.model_dump(mode="json")
                for declaration in program.declarations:
                    if isinstance(declaration, QueryDecl):
                        document = query_document(declaration)
                        if document is not None:
                            query_irs.append(document.model_dump(mode="json"))
                ir_detail = (
                    f"{len(schema.entities) if schema is not None else 0} schema entity/entities; "
                    f"{len(query_irs)} query document(s)"
                )
                stages.append(Stage("IR generation", "success", ir_detail, (time.perf_counter() - ir_started) * 1000))
            except Exception as exc:  # pragma: no cover - defensive
                diagnostics.add(
                    Diagnostic(
                        code="OQ-IR-001",
                        severity=Severity.ERROR,
                        message=f"IR generation failed: {exc}",
                    )
                )
                stages.append(
                    Stage("IR generation", "failed", str(exc), (time.perf_counter() - ir_started) * 1000)
                )
        else:
            stages.append(Stage("IR generation", "skipped", "no valid declarations to lower", 0.0))

        if mode == CHECK_ONLY:
            duration = (time.perf_counter() - started) * 1000
            return self._result(
                source=text,
                program=program,
                diagnostics=diagnostics,
                stages=stages,
                statements=[],
                schema_ir=schema_ir if options.include_ir else None,
                query_irs=query_irs if options.include_ir else [],
                warnings=warnings,
                dialect=options.dialect,
                syntax=parse_result.syntax,
                duration=duration,
            )

        # Stage 4: capability analysis (real adapter report only).
        capability_started = time.perf_counter()
        capability_findings = self._capability_analysis(program, options)
        diagnostics.extend(capability_findings)
        capability_errors = [item for item in capability_findings if item.is_error]
        if options.capabilities is None:
            capability_status = "not_run"
            capability_detail = "no adapter capability report supplied"
        else:
            capability_status = "failed" if capability_errors else "success"
            capability_detail = (
                f"{len(capability_findings)} finding(s) against "
                f"{options.adapter_id or 'the selected adapter'}"
            )
        stages.append(
            Stage(
                "Capability analysis",
                capability_status,
                capability_detail,
                (time.perf_counter() - capability_started) * 1000,
            )
        )

        # Stage 5: target emission.
        emit_started = time.perf_counter()
        emitted: EmittedProgram | None = None
        if diagnostics.has_errors:
            stages.append(Stage("Target compilation", "skipped", "blocked by errors", 0.0))
        else:
            try:
                emitted = emit_program(program, options.dialect)
                if not emitted.statements:
                    diagnostics.add(
                        Diagnostic(
                            code="OQ-CC-002",
                            severity=Severity.ERROR,
                            message="no executable statements were produced",
                            notes=("the program may only contain declarations Studio cannot lower yet",),
                        )
                    )
                    stages.append(
                        Stage(
                            "Target compilation",
                            "failed",
                            "no executable statements",
                            (time.perf_counter() - emit_started) * 1000,
                        )
                    )
                else:
                    stages.append(
                        Stage(
                            "Target compilation",
                            "success",
                            f"{len(emitted.statements)} statement(s) for {options.dialect}",
                            (time.perf_counter() - emit_started) * 1000,
                        )
                    )
            except Exception as exc:
                diagnostics.add(
                    Diagnostic(
                        code="OQ-CC-003",
                        severity=Severity.ERROR,
                        message=f"target compilation failed: {exc}",
                    )
                )
                stages.append(
                    Stage("Target compilation", "failed", str(exc), (time.perf_counter() - emit_started) * 1000)
                )
        warnings.extend(emitted.warnings if emitted is not None else [])

        statements = []
        if emitted is not None:
            for statement in emitted.statements:
                statements.append(
                    {
                        "id": statement.id,
                        "kind": statement.kind,
                        "sql": statement.sql,
                        "safety": statement.safety.model_dump(mode="json"),
                        "parameters": statement.parameters,
                        "warnings": statement.warnings,
                        "sourceName": statement.source_name,
                        "span": {
                            "start": statement.span.start.offset,
                            "end": statement.span.end.offset,
                            "line": statement.span.start.line,
                            "column": statement.span.start.column,
                        },
                    }
                )

        duration = (time.perf_counter() - started) * 1000
        result = self._result(
            source=text,
            program=program,
            diagnostics=diagnostics,
            stages=stages,
            statements=statements,
            schema_ir=schema_ir if options.include_ir else None,
            query_irs=query_irs if options.include_ir else [],
            warnings=warnings,
            dialect=options.dialect,
            syntax=parse_result.syntax,
            duration=duration,
        )
        result.compiled = list(emitted.statements) if emitted is not None else []
        return result

    def _result(
        self,
        *,
        source: str,
        program: Program,
        diagnostics: DiagnosticBag,
        stages: list[Stage],
        statements: list[dict[str, Any]],
        schema_ir: dict[str, Any] | None,
        query_irs: list[dict[str, Any]],
        warnings: list[str],
        dialect: str,
        syntax: str,
        duration: float,
    ) -> CompilationResult:
        ordered = diagnostics.sorted_diagnostics()
        return CompilationResult(
            ok=not diagnostics.has_errors,
            syntax=syntax,
            dialect=dialect,
            diagnostics=[diagnostic_to_dict(item) for item in ordered],
            stages=stages,
            statements=statements,
            schema_ir=schema_ir,
            query_ir=query_irs,
            warnings=warnings,
            stats={
                "declarations": len(program.declarations),
                "tables": sum(isinstance(item, TableDecl) for item in program.declarations),
                "queries": sum(isinstance(item, QueryDecl) for item in program.declarations),
                "statements": len(statements),
                "lines": source.count("\n") + 1 if source else 0,
                "durationMs": round(duration, 3),
            },
        )

    # ------------------------------------------------------------------
    # Semantic checks
    # ------------------------------------------------------------------

    def _semantic(self, program: Program) -> DiagnosticBag:
        bag = DiagnosticBag()
        tables: dict[str, TableDecl] = {}
        queries: dict[str, QueryDecl] = {}

        if not program.declarations:
            bag.add(
                Diagnostic(
                    code="OQ-SEM-000",
                    severity=Severity.WARNING,
                    message="the program contains no declarations",
                )
            )

        for declaration in program.declarations:
            if isinstance(declaration, TableDecl):
                if declaration.name in tables:
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-001",
                            severity=Severity.ERROR,
                            message=f"duplicate table {declaration.name!r}",
                            span=declaration.span,
                        )
                    )
                tables[declaration.name] = declaration
                seen: set[str] = set()
                primary_count = 0
                for attribute in declaration.attributes:
                    if attribute.name in seen:
                        bag.add(
                            Diagnostic(
                                code="OQ-SEM-002",
                                severity=Severity.ERROR,
                                message=f"duplicate column {attribute.name!r} in table {declaration.name!r}",
                                span=attribute.span,
                            )
                        )
                    seen.add(attribute.name)
                    if attribute.primary:
                        primary_count += 1
                    if attribute.primary and attribute.nullable:
                        bag.add(
                            Diagnostic(
                                code="OQ-SEM-003",
                                severity=Severity.ERROR,
                                message=f"primary key column {attribute.name!r} cannot be optional",
                                span=attribute.span,
                            )
                        )
                    if attribute.auto and not attribute.primary:
                        bag.add(
                            Diagnostic(
                                code="OQ-SEM-004",
                                severity=Severity.WARNING,
                                message=f"column {attribute.name!r} is auto but not primary",
                                span=attribute.span,
                            )
                        )
                if primary_count == 0 and declaration.attributes:
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-005",
                            severity=Severity.WARNING,
                            message=f"table {declaration.name!r} has no primary key",
                            span=declaration.span,
                        )
                    )
                for index in declaration.indexes:
                    for column in index.columns:
                        if column not in seen:
                            bag.add(
                                Diagnostic(
                                    code="OQ-SEM-006",
                                    severity=Severity.ERROR,
                                    message=f"index {index.name!r} references unknown column {column!r}",
                                    span=index.span,
                                )
                            )
                for unique in declaration.unique_constraints:
                    for column in unique.columns:
                        if column not in seen:
                            bag.add(
                                Diagnostic(
                                    code="OQ-SEM-007",
                                    severity=Severity.ERROR,
                                    message=f"unique constraint references unknown column {column!r}",
                                    span=unique.span,
                                )
                            )
            elif isinstance(declaration, QueryDecl):
                name = declaration.name
                if name:
                    if name in queries:
                        bag.add(
                            Diagnostic(
                                code="OQ-SEM-008",
                                severity=Severity.ERROR,
                                message=f"duplicate query {name!r}",
                                span=declaration.span,
                            )
                        )
                    queries[name] = declaration
                statement = declaration.statement
                table_name = self._statement_table(statement)
                if table_name:
                    table = tables.get(table_name)
                    if table is None:
                        bag.add(
                            Diagnostic(
                                code="OQ-SEM-009",
                                severity=Severity.ERROR,
                                message=f"query references unknown table {table_name!r}",
                                notes=("declare the table in the same program or connect to a database that contains it",),
                                span=statement.span,
                            )
                        )
                    else:
                        columns = {attribute.name for attribute in table.attributes}
                        self._validate_statement_columns(statement, columns, bag)
                if isinstance(statement, (UpdateStmt, DeleteStmt)) and statement.where is None:
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-010",
                            severity=Severity.WARNING,
                            message=f"{type(statement).__name__.replace('Stmt', '').lower()} without a where clause affects every row",
                            span=statement.span,
                        )
                    )
                parameters = {name for name, _ in declaration.parameters}
                used = self._collect_parameters(statement)
                for parameter in sorted(used - parameters):
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-011",
                            severity=Severity.ERROR,
                            message=f"parameter ${parameter} is not declared by the query",
                            span=declaration.span,
                        )
                    )
        return bag

    @staticmethod
    def _statement_table(statement: object) -> str | None:
        if isinstance(statement, SelectStmt):
            return statement.source.name
        if isinstance(statement, (InsertStmt, UpdateStmt, DeleteStmt)):
            return statement.table
        return None

    def _validate_statement_columns(
        self, statement: object, columns: set[str], bag: DiagnosticBag
    ) -> None:
        if isinstance(statement, SelectStmt):
            for projection in statement.projections:
                self._check_expression_columns(projection.expression, columns, bag)
            if statement.where is not None:
                self._check_expression_columns(statement.where, columns, bag)
            for item in statement.order_by:
                self._check_expression_columns(item.expression, columns, bag)
        elif isinstance(statement, UpdateStmt):
            for assignment in statement.assignments:
                if assignment.column not in columns:
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-012",
                            severity=Severity.ERROR,
                            message=f"update references unknown column {assignment.column!r}",
                            span=assignment.span,
                        )
                    )
                self._check_expression_columns(assignment.value, columns, bag)
            if statement.where is not None:
                self._check_expression_columns(statement.where, columns, bag)
        elif isinstance(statement, DeleteStmt):
            if statement.where is not None:
                self._check_expression_columns(statement.where, columns, bag)
        elif isinstance(statement, InsertStmt):
            for column in statement.columns:
                if column not in columns:
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-013",
                            severity=Severity.ERROR,
                            message=f"insert references unknown column {column!r}",
                            span=statement.span,
                        )
                    )
            for row in statement.rows:
                if len(row) != len(statement.columns):
                    bag.add(
                        Diagnostic(
                            code="OQ-SEM-014",
                            severity=Severity.ERROR,
                            message=f"insert has {len(statement.columns)} column(s) but a row has {len(row)} value(s)",
                            span=statement.span,
                        )
                    )

    def _check_expression_columns(
        self, expression: Expression, columns: set[str], bag: DiagnosticBag
    ) -> None:
        if isinstance(expression, ColumnRef):
            name = expression.parts[0] if len(expression.parts) > 1 else expression.parts[-1]
            if name not in columns and len(expression.parts) == 1:
                bag.add(
                    Diagnostic(
                        code="OQ-SEM-015",
                        severity=Severity.ERROR,
                        message=f"unknown column {name!r}",
                        span=expression.span,
                    )
                )
        elif isinstance(expression, BinaryOp):
            self._check_expression_columns(expression.left, columns, bag)
            self._check_expression_columns(expression.right, columns, bag)
        elif isinstance(expression, UnaryOp):
            self._check_expression_columns(expression.operand, columns, bag)
        elif isinstance(expression, InList):
            self._check_expression_columns(expression.expression, columns, bag)
            for item in expression.items:
                self._check_expression_columns(item, columns, bag)
        elif isinstance(expression, Between):
            self._check_expression_columns(expression.expression, columns, bag)
            self._check_expression_columns(expression.low, columns, bag)
            self._check_expression_columns(expression.high, columns, bag)
        elif isinstance(expression, IsPredicate):
            self._check_expression_columns(expression.expression, columns, bag)
        elif isinstance(expression, FunctionCall):
            for argument in expression.arguments:
                self._check_expression_columns(argument, columns, bag)
        elif isinstance(expression, Cast):
            self._check_expression_columns(expression.expression, columns, bag)
        elif isinstance(expression, Case):
            for when in expression.whens:
                self._check_expression_columns(when.condition, columns, bag)
                self._check_expression_columns(when.result, columns, bag)
            if expression.otherwise is not None:
                self._check_expression_columns(expression.otherwise, columns, bag)
        elif isinstance(expression, ListLiteral):
            for item in expression.items:
                self._check_expression_columns(item, columns, bag)

    def _collect_parameters(self, statement: object) -> set[str]:
        found: set[str] = set()

        def visit(expression: Expression | None) -> None:
            if expression is None:
                return
            if isinstance(expression, Parameter):
                found.add(expression.name)
            elif isinstance(expression, BinaryOp):
                visit(expression.left)
                visit(expression.right)
            elif isinstance(expression, UnaryOp):
                visit(expression.operand)
            elif isinstance(expression, InList):
                visit(expression.expression)
                for item in expression.items:
                    visit(item)
            elif isinstance(expression, Between):
                visit(expression.expression)
                visit(expression.low)
                visit(expression.high)
            elif isinstance(expression, IsPredicate):
                visit(expression.expression)
            elif isinstance(expression, FunctionCall):
                for argument in expression.arguments:
                    visit(argument)
            elif isinstance(expression, Cast):
                visit(expression.expression)
            elif isinstance(expression, Case):
                for when in expression.whens:
                    visit(when.condition)
                    visit(when.result)
                visit(expression.otherwise)
            elif isinstance(expression, ListLiteral):
                for item in expression.items:
                    visit(item)

        if isinstance(statement, SelectStmt):
            for projection in statement.projections:
                visit(projection.expression)
            visit(statement.where)
            for item in statement.order_by:
                visit(item.expression)
        elif isinstance(statement, InsertStmt):
            for row in statement.rows:
                for value in row:
                    visit(value)
        elif isinstance(statement, UpdateStmt):
            for assignment in statement.assignments:
                visit(assignment.value)
            visit(statement.where)
        elif isinstance(statement, DeleteStmt):
            visit(statement.where)
        return found

    # ------------------------------------------------------------------
    # Capability analysis
    # ------------------------------------------------------------------

    def _capability_analysis(self, program: Program, options: PipelineOptions) -> DiagnosticBag:
        bag = DiagnosticBag()
        if options.capabilities is None:
            return bag

        types = options.capabilities.get("types", {})
        for declaration in program.declarations:
            if not isinstance(declaration, TableDecl):
                continue
            for attribute in declaration.attributes:
                kind = attribute.type.canonical_name
                support = types.get(kind)
                if support is None:
                    bag.add(
                        Diagnostic(
                            code="OQ-CAP-001",
                            severity=Severity.WARNING,
                            message=(
                                f"adapter {options.adapter_id or 'selected'} does not publish type support "
                                f"for {kind!r}"
                            ),
                            span=attribute.span,
                        )
                    )
                    continue
                level = support.get("support") if isinstance(support, dict) else getattr(support, "support", None)
                notes = support.get("notes") if isinstance(support, dict) else getattr(support, "notes", None)
                if level == "unsupported":
                    bag.add(
                        Diagnostic(
                            code="OQ-CAP-002",
                            severity=Severity.ERROR,
                            message=(
                                f"type {kind!r} is not supported by adapter "
                                f"{options.adapter_id or 'selected'}"
                            ),
                            notes=(notes,) if notes else (),
                            span=attribute.span,
                        )
                    )
                elif level == "approximate":
                    bag.add(
                        Diagnostic(
                            code="OQ-CAP-003",
                            severity=Severity.WARNING,
                            message=f"type {kind!r} is approximate on adapter {options.adapter_id or 'selected'}",
                            notes=(notes,) if notes else (),
                            span=attribute.span,
                        )
                    )
        return bag


def explain_sql(sql: str, dialect: str) -> str:
    stripped = sql.strip().rstrip(";")
    if dialect == "sqlite":
        return f"EXPLAIN QUERY PLAN {stripped};"
    if dialect in ("postgres", "cockroachdb", "mysql", "mariadb"):
        return f"EXPLAIN {stripped};"
    return f"EXPLAIN {stripped};"
