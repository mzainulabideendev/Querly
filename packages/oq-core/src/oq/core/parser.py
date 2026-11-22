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
from lark.exceptions import CYK

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
    semantic_view,
    to_data,
    to_json,
    walk,
)

from typing import Any

# ---------------------------------------------------------------------------
# Lark grammar strings – compiled once at import time
# ---------------------------------------------------------------------------

_terminals_grammar = Path(__file__).with_name("terminals.lark").read_text()
_terminals_parser = Lark(_terminals_grammar, parser="lalr")

_common_grammar = Path(__file__).with_name("grammar/common.lark").read_text()
_english_grammar = Path(__file__).with_name("grammar/english.lark").read_text()
_structured_grammar = Path(__file__).with_name("grammar/structured.lark").read_text()

# Build Lark parsers that use the terminal names from terminals.lark.
# The grammars reference token kinds (IDENT, QUOTED_IDENT, INTEGER, …,
# KW_*) which match the StrEnum values in TokenKind.
_parser_english = Lark(
    _english_grammar,
    parser="lalr",
    maybe_placeholders=False,
    import_terms_from=_terminals_parser,
)
_parser_structured = Lark(
    _structured_grammar,
    parser="lalr",
    maybe_placeholders=False,
    import_terms_from=_terminals_parser,
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
        start_offset = node.line_start_offset if hasattr(node, "line_start_offset") else 0
        end_offset = node.line_end_offset if hasattr(node, "line_end_offset") else 0
        # If Lark didn't provide explicit offsets, compute from line/column.
        # As a fallback we use the Lark node's start/end positions if any.
        return source.span(max(0, start_offset), max(0, end_offset))

    # --- Declaration‑level nodes -------------------------------------------------

    if type(lark_node).__name__ == "program":
        # Program: declarations + span
        declarations = tuple(
            _node_from_lark(d, source, diagnostics, offset_adjustment)
            for d in lark_node.declarations
        )
        span = source.span(0, source.length)
        return Program(declarations=declarations, span=span)

    # --- Schema ---------------------------------------------------------------
    if type(lark_node).__name__ in ("structured_schema", "english_schema"):
        # Convert to TableDecl
        # The Lark node has ``name`` and ``table_item*`` children.
        name = lark_node.name
        # The grammar gives us children; we need to map them to Attributes/
        # IndexDecl/UniqueConstraint.
        # For now we build a minimal TableDecl and emit a note that full
        # conversion will be handled in later phases.
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
        # Convert to SelectStmt / InsertStmt / UpdateStmt / DeleteStmt
        # The Lark grammar already gives us the structured query shape.
        # We only need to map the top‑level statement.
        stmt = _node_from_lark(lark_node.statement, source, diagnostics)
        # Derive a name from the Lark node if present, else None.
        name = getattr(lark_node, "name", None)
        syntax = type(lark_node).__name__
        # Determine the concrete statement type from the Lark tag.
        stmt_type = type(stmt).__name__
        return QueryDecl(
            name=name,
            parameters=(),
            statement=stmt,
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Transaction -----------------------------------------------------------
    if type(lark_node).__name__ in ("structured_transaction", "english_transaction"):
        stmts = tuple(
            _node_from_lark(s, source, diagnostics)
            for s in lark_node.statements
        )
        name = getattr(lark_node, "name", None)
        syntax = type(lark_node).__name__
        return TransactionDecl(
            name=name,
            statements=stmts,
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Migration --------------------------------------------------------------
    if type(lark_node).__name__ in ("structured_migration", "english_migration"):
        up = tuple(
            _node_from_lark(s, source, diagnostics) for s in lark_node.up_body
        )
        down = tuple(
            _node_from_lark(s, source, diagnostics) for s in lark_node.down_body
        ) if hasattr(lark_node, "down_body") and lark_node.down_body else tuple()
        name = getattr(lark_node, "name", None)
        version = int(getattr(lark_node, "version", "0")) if hasattr(lark_node, "version") else 0
        description = getattr(lark_node, "description", None)
        syntax = type(lark_node).__name__
        return MigrationDecl(
            name=name,
            version=version,
            entity=getattr(lark_node, "entity", None),
            description=description,
            up=up,
            down=down,
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Conversion --------------------------------------------------------------
    if type(lark_node).__name__ == "conversion_decl":
        source_adapter = getattr(lark_node, "source_adapter", None)
        target_adapter = getattr(lark_node, "target_adapter", None)
        profile = getattr(lark_node, "profile", None)
        entities = tuple(getattr(lark_node, "entities", ()))
        syntax = type(lark_node).__name__
        return ConversionDecl(
            source_adapter=source_adapter,
            target_adapter=target_adapter,
            profile=profile,
            entities=entities,
            syntax=syntax,
            span=_span_from_lark(lark_node),
        )

    # --- Attribute / table item ---------------------------------------------------
    if type(lark_node).__name__ == "attribute_decl":
        # name : type_expr ? modifiers…
        name = lark_node.name
        type_expr = _node_from_lark(lark_node.type_expr, source, diagnostics)
        # Question mark → nullable
        nullable = not bool(lark_node.question) if hasattr(lark_node, "question") else False
        # Pull modifiers (_AT KW_PRIMARY, _AT KW_REQUIRED, …)
        primary = False
        required = True  # default after semantic normalization
        unique = False
        auto = False
        indexed = False
        default = None
        check = None
        references = None

        # Walk modifier children
        for child in lark_node.children:
            if hasattr(child, "kind") and child.kind == "primary_modifier":
                primary = True
            elif hasattr(child, "kind") and child.kind == "required_modifier":
                required = True
            elif hasattr(child, "kind") and child.kind == "unique_modifier":
                unique = True
            elif hasattr(child, "kind") and child.kind == "auto_modifier":
                auto = True
            elif hasattr(child, "kind") and child.kind == "index_modifier":
                indexed = True
            elif hasattr(child, "kind") and child.kind.startswith("default_modifier"):
                # child.expression is the default value expression
                default = _node_from_lark(child.expression, source, diagnostics)  # type: ignore
            elif hasattr(child, "kind") and child.kind.startswith("check_modifier"):
                check = _node_from_lark(child.expression, source, diagnostics)  # type: ignore
            elif hasattr(child, "kind") and child.kind.startswith("references_modifier"):
                ref_name = str(child.qualified_name) if hasattr(child, "qualified_name") else str(child)  # type: ignore
                references = ref_name

        span = _span_from_lark(lark_node)
        return Attribute(
            name=name,
            type=type_expr,
            nullable=nullable,
            primary=primary,
            unique=unique,
            auto=auto,
            indexed=indexed,
            default=default,
            check=check,
            references=references,
            span=span,
        )

    # --- IndexDecl --------------------------------------------------------------
    if type(lark_node).__name__ == "index_decl":
        name = lark_node.name
        columns = tuple(lark_node.ident_list) if hasattr(lark_node, "ident_list") else tuple()
        unique = bool(lark_node.unique) if hasattr(lark_node, "unique") else False
        span = _span_from_lark(lark_node)
        return IndexDecl(name=name, columns=columns, unique=unique, span=span)

    # --- UniqueConstraint --------------------------------------------------------
    if type(lark_node).__name__ == "unique_constraint":
        cols = tuple(lark_node.ident_list) if hasattr(lark_node, "ident_list") else tuple()
        span = _span_from_lark(lark_node)
        return UniqueConstraint(columns=cols, span=span)

    # --- SelectStmt --------------------------------------------------------------
    if type(lark_node).__name__ == "select_stmt":
        source = _node_from_lark(lark_node.from_clause, source, diagnostics) if hasattr(lark_node, "from_clause") else TableRef(name="", alias=None, span=_span_from_lark(lark_node))
        joins = tuple(
            _node_from_lark(j, source, diagnostics) for j in (lark_node.join_clause or ())
        )
        projections = tuple(
            _node_from_lark(p, source, diagnostics) for p in (lark_node.projections or ())
        )
        where = _node_from_lark(lark_node.where_clause, source, diagnostics) if hasattr(lark_node, "where_clause") and lark_node.where_clause else None
        group_by = tuple(
            _node_from_lark(g, source, diagnostics) for g in (lark_node.group_clause or ())
        ) if hasattr(lark_node, "group_clause") and lark_node.group_clause else tuple()
        having = _node_from_lark(lark_node.having_clause, source, diagnostics) if hasattr(lark_node, "having_clause") and lark_node.having_clause else None
        order_by = tuple(
            _node_from_lark(o, source, diagnostics) for o in (lark_node.order_clause or ())
        ) if hasattr(lark_node, "order_clause") and lark_node.order_clause else tuple()
        limit = int(lark_node.limit_clause) if hasattr(lark_node, "limit_clause") and lark_node.limit_clause else None
        offset = int(lark_node.offset_clause) if hasattr(lark_node, "offset_clause") and lark_node.offset_clause else None
        span = _span_from_lark(lark_node)
        return SelectStmt(
            source=source,
            joins=joins,
            projections=projections,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            span=span,
        )

    # --- InsertStmt --------------------------------------------------------------
    if type(lark_node).__name__ == "insert_stmt":
        table = lark_node.name
        columns = tuple(lark_node.ident_list) if hasattr(lark_node, "ident_list") else tuple()
        values = tuple(
            _node_from_lark(v, source, diagnostics) for v in (lark_node.tuple_lit or ())
        )
        returning = None
        if hasattr(lark_node, "returning_clause") and lark_node.returning_clause:
            returning = _node_from_lark(lark_node.returning_clause, source, diagnostics)
        span = _span_from_lark(lark_node)
        return InsertStmt(
            table=table,
            columns=columns,
            rows=values,
            returning=returning,
            span=span,
        )

    # --- UpdateStmt --------------------------------------------------------------
    if type(lark_node).__name__ == "update_stmt":
        table = lark_node.name
        assignments = tuple(
            _node_from_lark(a, source, diagnostics) for a in (lark_node.assignment or ())
        )
        where = _node_from_lark(lark_node.where_clause, source, diagnostics) if hasattr(lark_node, "where_clause") and lark_node.where_clause else None
        returning = None
        if hasattr(lark_node, "returning_clause") and lark_node.returning_clause:
            returning = _node_from_lark(lark_node.returning_clause, source, diagnostics)
        span = _span_from_lark(lark_node)
        return UpdateStmt(
            table=table,
            assignments=assignments,
            where=where,
            returning=returning,
            span=span,
        )

    # --- DeleteStmt --------------------------------------------------------------
    if type(lark_node).__name__ == "delete_stmt":
        table = lark_node.name
        where = _node_from_lark(lark_node.where_clause, source, diagnostics) if hasattr(lark_node, "where_clause") and lark_node.where_clause else None
        returning = None
        if hasattr(lark_node, "returning_clause") and lark_node.returning_clause:
            returning = _node_from_lark(lark_node.returning_clause, source, diagnostics)
        span = _span_from_lark(lark_node)
        return DeleteStmt(
            table=table,
            where=where,
            returning=returning,
            span=span,
        )

    # --- assignment (structured) ------------------------------------------------
    if type(lark_node).__name__ == "assignment":
        column = lark_node.name
        value = _node_from_lark(lark_node.expression, source, diagnostics)
        span = _span_from_lark(lark_node)
        return Assignment(column=column, value=value, span=span)

    # --- Projection --------------------------------------------------------------
    if type(lark_node).__name__ == "projection":
        expr = _node_from_lark(lark_node.expression, source, diagnostics) if hasattr(lark_node, "expression") else Star(span=_span_from_lark(lark_node))
        alias = getattr(lark_node, "alias", None)
        span = _span_from_lark(lark_node)
        return Projection(expression=expr, alias=alias, span=span)

    # --- OrderItem ---------------------------------------------------------------
    if type(lark_node).__name__ == "order_item":
        expr = _node_from_lark(lark_node.expression, source, diagnostics)
        direction = getattr(lark_node, "direction", "asc") or "asc"
        nulls = getattr(lark_node, "nulls", None)
        span = _span_from_lark(lark_node)
        return OrderItem(expression=expr, direction=direction, nulls=nulls, span=span)

    # --- FromClause / TableRef ---------------------------------------------------
    if type(lark_node).__name__ in ("from_clause", "from_clause_item"):
        name = lark_node.name
        alias = getattr(lark_node, "alias", None)
        span = _span_from_lark(lark_node)
        return TableRef(name=name, alias=alias, span=span)

    # --- JoinClause --------------------------------------------------------------
    if type(lark_node).__name__ in ("join_clause",):
        kind = getattr(lark_node, "kind", "inner") or "inner"
        table = _node_from_lark(lark_node.table, source, diagnostics)
        condition = _node_from_lark(lark_node.expression, source, diagnostics) if hasattr(lark_node, "expression") else None
        span = _span_from_lark(lark_node)
        return Join(kind=kind, table=table, condition=condition, span=span)

    # --- Literal / Expression children -------------------------------------------
    if type(lark_node).__name__ == "literal":
        kind = lark_node.kind  # type: ignore[attr-dict]
        value = lark_node.value  # type: ignore[attr-dict]
        span = _span_from_lark(lark_node)
        return Literal(kind=kind, value=value, span=span)

    # --- ColumnRef ---------------------------------------------------------------
    if type(lark_node).__name__ == "column_ref":
        parts = tuple(lark_node.parts) if hasattr(lark_node, "parts") else ()
        span = _span_from_lark(lark_node)
        return ColumnRef(parts=parts, span=span)

    # --- Parameter ($name) ------------------------------------------------------
    if type(lark_node).__name__ == "parameter_ref":
        name = lark_node.name
        span = _span_from_lark(lark_node)
        return Parameter(name=name, span=span)

    # --- FunctionCall ------------------------------------------------------------
    if type(lark_node).__name__ == "function_call":
        name = tuple(lark_node.name) if hasattr(lark_node, "name") else ()
        args = tuple(
            _node_from_lark(a, source, diagnostics) for a in (lark_node.arguments or ())
        )
        span = _span_from_lark(lark_node)
        return FunctionCall(name=name, arguments=args, span=span)

    # --- Cast --------------------------------------------------------------
    if type(lark_node).__name__ == "cast_expr":
        expression = _node_from_lark(lark_node.expression, source, diagnostics)
        target = _node_from_lark(lark_node.target, source, diagnostics)
        span = _span_from_lark(lark_node)
        return Cast(expression=expression, target=target, span=span)

    # --- Case/When --------------------------------------------------------------
    if type(lark_node).__name__ == "case_expr":
        whens = tuple(
            _node_from_lark(w, source, diagnostics) for w in (lark_node.when_clause or ())
        )
        otherwise = _node_from_lark(lark_node.else_expression, source, diagnostics) if hasattr(lark_node, "else_expression") and lark_node.else_expression else None
        span = _span_from_lark(lark_node)
        return Case(whens=whens, otherwise=otherwise, span=span)

    # --- When --------------------------------------------------------------
    if type(lark_node).__name__ == "when_clause":
        condition = _node_from_lark(lark_node.expression, source, diagnostics)
        result = _node_from_lark(lark_node.then_expression, source, diagnostics)
        span = _span_from_lark(lark_node)
        return When(condition=condition, result=result, span=span)

    # --- ListLiteral ------------------------------------------------------------
    if type(lark_node).__name__ == "list_lit":
        items = tuple(
            _node_from_lark(i, source, diagnostics) for i in (lark_node.items or ())
        )
        span = _span_from_lark(lark_node)
        return ListLiteral(items=items, span=span)

    # --- BinaryOp --------------------------------------------------------------
    if type(lark_node).__name__ == "binary_op":
        left = _node_from_lark(lark_node.left, source, diagnostics)
        right = _node_from_lark(lark_node.right, source, diagnostics)
        operator = lark_node.operator  # type: ignore[attr-dict]
        span = _span_from_lark(lark_node)
        return BinaryOp(operator=operator, left=left, right=right, span=span)

    # --- UnaryOp --------------------------------------------------------------
    if type(lark_node).__name__ == "unary_op":
        operator = lark_node.operator  # type: ignore[attr-dict]
        operand = _node_from_lark(lark_node.operand, source, diagnostics)
        span = _span_from_lark(lark_node)
        return UnaryOp(operator=operator, operand=operand, span=span)

    # --- InList --------------------------------------------------------------
    if type(lark_node).__name__ == "in_list":
        expression = _node_from_lark(lark_node.expression, source, diagnostics)
        items = tuple(_node_from_lark(i, source, diagnostics) for i in lark_node.items)
        negated = bool(lark_node.negated) if hasattr(lark_node, "negated") else False
        span = _span_from_lark(lark_node)
        return InList(expression=expression, items=items, negated=negated, span=span)

    # --- Between --------------------------------------------------------------
    if type(lark_node).__name__ == "between":
        expression = _node_from_lark(lark_node.expression, source, diagnostics)
        low = _node_from_lark(lark_node.low, source, diagnostics)
        high = _node_from_lark(lark_node.high, source, diagnostics)
        negated = bool(lark_node.negated) if hasattr(lark_node, "negated") else False
        span = _span_from_lark(lark_node)
        return Between(expression=expression, low=low, high=high, negated=negated, span=span)

    # --- IsPredicate -----------------------------------------------------------
    if type(lark_node).__name__ == "is_predicate":
        expression = _node_from_lark(lark_node.expression, source, diagnostics)
        test = lark_node.test  # type: ignore[attr-dict]
        negated = bool(lark_node.negated) if hasattr(lark_node, "negated") else False
        span = _span_from_lark(lark_node)
        return IsPredicate(expression=expression, test=test, negated=negated, span=span)

    # --- DECIMAL type expression ------------------------------------------------
    if type(lark_node).__name__ == "decimal_type":
        # KW_DECIMAL _LPAREN INTEGER _COMMA INTEGER _RPAREN
        precision = int(lark_node.arguments[0])  # type: ignore[attr-dict]
        scale = int(lark_node.arguments[1])  # type: ignore[attr-dict]
        span = _span_from_lark(lark_node)
        return TypeExpr(name="decimal", arguments=(precision, scale), span=span)

    # --- Sized type (text/binary) ----------------------------------------------
    if type(lark_node).__name__ == "sized_type":
        name = lark_node.name  # type: ignore[attr-dict]
        size = int(lark_node.arguments[0])  # type: ignore[attr-dict]
        span = _span_from_lark(lark_node)
        return TypeExpr(name=name, arguments=(size,), span=span)

    # --- Vector type ------------------------------------------------------------
    if type(lark_node).__name__ == "vector_type":
        dimension = int(lark_node.arguments[0])  # type: ignore[attr-dict]
        span = _span_from_lark(lark_node)
        return TypeExpr(name="vector", arguments=(dimension,), span=span)

    # --- Collection type (list/set) ---------------------------------------------
    if type(lark_node).__name__ in ("collection_type",):
        inner = _node_from_lark(lark_node.type_expr, source, diagnostics)
        span = _span_from_lark(lark_node)
        return TypeExpr(name=lark_node.collection_kind, arguments=(inner,), span=span)  # type: ignore[attr-dict]

    # --- Map type --------------------------------------------------------------
    if type(lark_node).__name__ == "map_type":
        key_type = _node_from_lark(lark_node.key_type, source, diagnostics)
        value_type = _node_from_lark(lark_node.value_type, source, diagnostics)
        span = _span_from_lark(lark_node)
        return TypeExpr(name="map", arguments=(key_type, value_type), span=span)

    # --- Reference type --------------------------------------------------------
    if type(lark_node).__name__ == "reference_type":
        qualified = _node_from_lark(lark_node.qualified_name, source, diagnostics)
        span = _span_from_lark(lark_node)
        return TypeExpr(name="reference", arguments=(qualified,), span=span)

    # --- Scalar type ------------------------------------------------------------
    if type(lark_node).__name__ == "scalar_type":
        name = lark_node.name  # type: ignore[attr-dict]
        span = _span_from_lark(lark_node)
        return TypeExpr(name=name, arguments=(), span=span)

    # --- Default: return the node as‑is if it already looks like an AST node -----
    # Many Lark nodes already carry the right attribute names; just map the span.
    span = _span_from_lark(lark_node)
    # Try to find a matching dataclass by looking at attribute names.
    # This fallback is used for nodes we haven't explicitly handled.
    return lark_node  # type: ignore[return-value]


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
    lower = text.lower()

    # Heuristic: if the text contains "create table" / "with:" / "get all"
    # patterns it's English‑like; if it contains "table {" / "query {"
    # patterns it's Structured.
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
        # Characters that Lark cannot map to tokens – report as lexical errors.
        start = exc.column - 1  # Lark is 1‑based
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
        # Report the unexpected token with a positioned diagnostic.
        # Lark gives us the line/column; we convert to our span system.
        col = exc.column  # 1‑based
        line = exc.line  # 1‑based
        # Find the offset from the source text.
        offset = sum(
            len(split_source_lines(text)[i]) + 1  # newline count
            for i in range(line - 1)
        ) + col - 1
        # Approximate end offset.
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
    except CYK as exc:
        # General parse failure – Lark may give a message; we capture it.
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
    # Ensure we always return a Program.
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
    # If any structured marker is present and no English marker, assume structured.
    has_structured = any(m in t for m in structured_markers)
    has_english = any(m in t for m in english_markers)
    if has_structured and not has_english:
        return False
    if has_english and not has_structured:
        return True
    # Mixed or ambiguous: default to structured for safety.
    return False


def check(
    source: SourceFile | str | Path,
    *,
    syntax: str = "auto",
) -> DiagnosticBag:
    """Parse *source* and return a ``DiagnosticBag`` of errors/warnings.

    This is the function behind the ``oq check`` CLI command.  It returns
    only the bag; callers decide what to do with the exit code.
    """
    _, bag = parse(source, syntax=syntax)
    return bag


# ---------------------------------------------------------------------------
# Minimal CLI entry point (Phase 7 will replace this with proper CLI wiring)
# ---------------------------------------------------------------------------


def _cli_main(argv: list[str] | None = None) -> int:
    """Very small CLI: ``oq check`` one or more files, exit 0|1."""
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
            # Render each diagnostic as JSON (simple text for now).
            for d in bag.sorted_diagnostics():
                print(render_diagnostic(d, source_text=None), end="")
            print()
        else:
            # Human‑readable rendering – use the first file's text.
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