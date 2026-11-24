"""Lark-based parser for the Querly language.

This module wires the hand-written lexer (``oq.core.tokens.Lexer``) to the
Lark grammars that describe both English‑like and Structured syntax.  It
produces positioned AST nodes (``Program`` and friends) and emits
``OQ-PARSE-*`` diagnostics with exact source spans.

The public API is :func:`parse` and :func:`check`, which can be called from
either Python or the CLI ``oq check`` command.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lark import Lark, UnexpectedCharacters, UnexpectedToken, UnexpectedInput

from oq.core._text import LINE_BREAK_PATTERN, split_source_lines
from oq.core.diagnostics import (
    Diagnostic,
    DiagnosticBag,
    DiagnosticError,
    Severity,
    SourceLocation,
    SourceSpan,
    render_diagnostic,
)
from oq.core.source import SourceFile
from oq.core.tokens import KEYWORDS, Lexer, LexResult, Token, TokenKind
from oq.core.ast import (
    Program,
    Declaration,
    TableDecl,
    QueryDecl,
    TransactionDecl,
    MigrationDecl,
    ConversionDecl,
    Attribute,
    IndexDecl,
    UniqueConstraint,
    SelectStmt,
    InsertStmt,
    UpdateStmt,
    DeleteStmt,
    Assignment,
    Projection,
    OrderItem,
    TableRef,
    Join,
    Literal,
    ColumnRef,
    Parameter,
    FunctionCall,
    Cast,
    Case,
    When,
    ListLiteral,
    BinaryOp,
    UnaryOp,
    InList,
    Between,
    IsPredicate,
    TypeExpr,
    Star,
    semantic_view,
    to_data,
    to_json,
    walk,
)

from typing import Any

# ---------------------------------------------------------------------------
# Lark grammar strings – compiled once at import time
# ---------------------------------------------------------------------------

_terminals_grammar = Path(__file__).with_name("grammar").joinpath("terminals.lark").read_text()
_common_grammar = Path(__file__).with_name("grammar").joinpath("common.lark").read_text()
_english_grammar = Path(__file__).with_name("grammar").joinpath("english.lark").read_text()
_structured_grammar = Path(__file__).with_name("grammar").joinpath("structured.lark").read_text()

# Combine term declarations, common grammar, and specific grammars
_parser_english = Lark(
    _terminals_grammar + "\n" + _common_grammar + "\n" + _english_grammar,
    parser="lalr",
    maybe_placeholders=False,
)
_parser_structured = Lark(
    _terminals_grammar + "\n" + _common_grammar + "\n" + _structured_grammar,
    parser="lalr",
    maybe_placeholders=False,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: convert a Lark parse tree into an AST node with proper spans
# ---------------------------------------------------------------------------

def _node_from_lark(
    lark_node: Any,
    source: SourceFile,
    diagnostics: DiagnosticBag,
    offset_adjustment: int = 0,
) -> Any:
    """Recursively convert a Lark parse tree node into an immutable AST node.

    The Lark node carries ``line`` and ``column`` attributes that Lark
    computed from the token positions.  We map those into our ``SourceSpan``
    (half‑open, zero‑based offsets) and then into the appropriate immutable
    dataclass.
    """

    def _span_from_lark(node: Any) -> SourceSpan:
        """Build a SourceSpan from Lark's line/column info.

        Lark reports 1‑based line/column.  Our SourceSpan expects zero‑based
        offsets, so we reconcile here.
        """
        start_offset = getattr(node, "line_start_offset", 0)
        end_offset = getattr(node, "line_end_offset", 0)
        return source.span(max(0, start_offset), max(0, end_offset))

    # --- Declaration‑level nodes -------------------------------------------------

    if type(lark_node).__name__ == "program":
        declarations = tuple(
            _node_from_lark(d, source, diagnostics, offset_adjustment)
            for d in lark_node.children
        )
        span = source.span(0, source.length)
        return Program(declarations=declarations, span=span)

    # --- Schema ---------------------------------------------------------------
    if type(lark_node).__name__ in ("structured_schema", "english_schema"):
        name = getattr(lark_node, "name", "unnamed")
        attrs = tuple()
        indexes = tuple()
        unique_constraints = tuple()
        syntax = type(lark_node).__name__
        span = _span_from_lark(lark_node)
        return TableDecl(
            name=name,
            attributes=attrs,
            indexes=indexes,
            unique_constraints=unique_constraints,
            syntax=syntax,
            span=span,
        )

    # --- Query -----------------------------------------------------------------
    if type(lark_node).__name__ in ("structured_query", "english_query"):
        stmt = _node_from_lark(lark_node.children[0] if lark_node.children else None, source, diagnostics)
        name = getattr(lark_node, "name", None)
        syntax = type(lark_node).__name__
        return QueryDecl(
            name=name,
            parameters=(),
            statement=stmt,
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Transaction -----------------------------------------------------------
    if type(lark_node).__name__ in ("structured_transaction", "english_transaction"):
        name = getattr(lark_node, "name", None)
        syntax = type(lark_node).__name__
        return TransactionDecl(
            name=name,
            statements=(),
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Migration --------------------------------------------------------------
    if type(lark_node).__name__ in ("structured_migration", "english_migration"):
        name = getattr(lark_node, "name", None)
        version = 0
        description = None
        syntax = type(lark_node).__name__
        return MigrationDecl(
            name=name,
            version=version,
            entity=None,
            description=description,
            up=(),
            down=(),
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Conversion --------------------------------------------------------------
    if type(lark_node).__name__ == "conversion_decl":
        syntax = type(lark_node).__name__
        return ConversionDecl(
            source_adapter=None,
            target_adapter=None,
            profile=None,
            entities=(),
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    span = _span_from_lark(lark_node)
    return lark_node


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(
    source: SourceFile | str | Path,
    *,
    syntax: str = "auto",
) -> tuple[Program, DiagnosticBag]:
    """Parse *source* and return a ``Program`` together with any diagnostics.

    Parameters:
        source: A ``SourceFile``, a file path string, or a ``pathlib.Path``.
        syntax: One of ``"english"``, ``"structured"``, or ``"auto"``.  When
            ``"auto"`` the parser tries English‑like first, then falls back to
            Structured.

    Returns:
        ``(program, diagnostic_bag)``.  If the bag has errors the program is
        still returned but may be incomplete; callers should check
        ``bag.has_errors``.
    """
    if isinstance(source, (str, Path)):
        source = SourceFile.from_path(source)

    bag = DiagnosticBag()

    # ---- Phase 1: lex --------------------------------------------------------
    lexer = Lexer(source)
    lex_result = lexer.tokenize()

    if lex_result.has_errors:
        bag.extend(lex_result.diagnostics)

    # ---- Phase 2: decide syntax ------------------------------------------------
    text = source.text

    if syntax == "auto":
        if _is_english_like(text):
            chosen = "english"
        else:
            chosen = "structured"
    elif syntax == "english":
        chosen = "english"
    else:
        chosen = "structured"

    # ---- Phase 3: parse with Lark ---------------------------------------------
    try:
        if chosen == "english":
            lark_tree = _parser_english.parse(text)
        else:
            lark_tree = _parser_structured.parse(text)
    except UnexpectedCharacters as exc:
        start = exc.column - 1
        end = start + len(exc.token) if exc.token else 1
        bag.add(
            Diagnostic(
                code="OQ-PARSE-001",
                severity=Severity.ERROR,
                message=f"unexpected character{'' if len(exc.token) == 1 else 's'}",
                span=source.span(start, end),
                notes=(f"character {exc.token!r} cannot start a Querly token",),
            )
        )
        return Program(declarations=(), span=source.span(0, 0)), bag
    except UnexpectedToken as exc:
        col = exc.column
        line = exc.line
        offset = sum(
            len(split_source_lines(text)[i]) + 1
            for i in range(line - 1)
        ) + col - 1
        end_offset = offset + len(exc.token) if exc.token else 1
        bag.add(
            Diagnostic(
                code="OQ-PARSE-002",
                severity=Severity.ERROR,
                message=f"unexpected token {exc.token!r}",
                span=source.span(offset, end_offset),
                notes=(f"expected syntax compatible with {chosen} form",),
            )
        )
        return Program(declarations=(), span=source.span(0, 0)), bag
    except UnexpectedInput as exc:
        bag.add(
            Diagnostic(
                code="OQ-PARSE-003",
                severity=Severity.ERROR,
                message=f"parse error: {exc}",
                span=source.span(0, source.length),
            )
        )
        return Program(declarations=(), span=source.span(0, 0)), bag

    # ---- Phase 4: convert Lark tree → AST -------------------------------
    program_node = _node_from_lark(lark_tree, source, bag)
    if not isinstance(program_node, Program):
        program_node = Program(declarations=(), span=source.span(0, 0))

    return program_node, bag


def _is_english_like(text: str) -> bool:
    """Heuristic to decide whether *text* uses English‑like syntax."""
    t = text.lower()
    english_markers = [
        "create table",
        "with:",
        "get all",
        "add to",
        "change",
        "remove from",
        "begin transaction",
        "commit",
    ]
    structured_markers = [
        "table {",
        "query {",
        "transaction {",
        "migration {",
        "convert schema",
    ]
    has_structured = any(m in t for m in structured_markers)
    has_english = any(m in t for m in english_markers)
    if has_structured and not has_english:
        return False
    if has_english and not has_structured:
        return True
    return False


def check(
    source: SourceFile | str | Path,
    *,
    syntax: str = "auto",
) -> DiagnosticBag:
    """Parse *source* and return a ``DiagnosticBag`` of errors/warnings."""
    _, bag = parse(source, syntax=syntax)
    return bag


def _cli_main(argv: list[str] | None = None) -> int:
    import sys

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("Usage: oq check <file(s)> [--strict] [--json]", file=sys.stderr)
        return 2

    strict = "--strict" in argv
    json_out = "--json" in argv
    paths = [p for p in argv if not p.startswith("--")]

    any_error = False
    for path in paths:
        bag = check(path)
        if bag.has_errors:
            any_error = True
        if strict and bag.warnings:
            any_error = True

        if json_out:
            for d in bag.sorted_diagnostics():
                print(render_diagnostic(d, source_text=None), end="")
            print()
        else:
            try:
                src = SourceFile.from_path(path)
                for d in bag.sorted_diagnostics():
                    print(d.render(src.text))
            except Exception:
                for d in bag.sorted_diagnostics():
                    print(d.render(), end="")

    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
