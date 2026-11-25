"""Abstract syntax tree for the Querly language.

The AST is the parser's output and the semantic analyzer's input. It is close
to source: it keeps both surface syntaxes, unresolved names, and exact source
spans, and it may be incomplete after a parse error.

Rules encoded here:

* Every node is an immutable dataclass with ``slots`` and a ``span``.
* ``to_data``/``to_json`` produce deterministic output: field order is the
  declaration order, so equivalent programs serialize identically.
* Presentation-only information (``syntax``, ``span``) is excluded by
  :func:`semantic_view`, which is what equivalence tests compare.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, fields, is_dataclass
from typing import Final

from oq.core.diagnostics import SourceSpan

__all__ = [
    "TYPE_ALIASES",
    "AlterAction",
    "AlterTable",
    "Assignment",
    "Attribute",
    "Between",
    "BinaryOp",
    "Case",
    "Cast",
    "ColumnRef",
    "ConversionDecl",
    "CreateIndexStatement",
    "Declaration",
    "DeleteStmt",
    "DropIndexStatement",
    "DropTableStatement",
    "Expression",
    "FunctionCall",
    "ImportDecl",
    "InList",
    "IndexDecl",
    "InsertStmt",
    "IsPredicate",
    "Join",
    "ListLiteral",
    "Literal",
    "MigrationDecl",
    "OrderItem",
    "Parameter",
    "Program",
    "Projection",
    "QueryDecl",
    "Returning",
    "SchemaItem",
    "SelectStmt",
    "Star",
    "Statement",
    "TableDecl",
    "TableRef",
    "TransactionDecl",
    "TypeExpr",
    "UnaryOp",
    "UniqueConstraint",
    "UpdateStmt",
    "When",
    "semantic_view",
    "to_data",
    "to_json",
    "walk",
]

TYPE_ALIASES: Final[dict[str, str]] = {
    "int": "integer",
    "string": "text",
    "bool": "boolean",
    "number": "integer",
}
"""Surface aliases normalized away before IR generation."""


@dataclass(frozen=True, slots=True)
class TypeExpr:
    """A universal type expression, possibly parameterized."""

    name: str
    arguments: tuple[TypeExpr | int | str, ...]
    span: SourceSpan

    @property
    def canonical_name(self) -> str:
        """The canonical universal type name, with aliases normalized."""
        return TYPE_ALIASES.get(self.name, self.name)

    @property
    def nullable(self) -> bool:
        """Always false: nullability belongs to the attribute, not the type."""
        return False


@dataclass(frozen=True, slots=True)
class Literal:
    """A literal value.

    ``kind`` is one of ``integer``, ``decimal``, ``float``, ``string``,
    ``boolean``, ``null``, ``date``, ``time``, ``timestamp``, ``duration``.
    ``value`` is ``int`` for integers, ``str`` for everything else, ``bool`` for
    booleans, and ``None`` for null, so serialization stays JSON-safe.
    """

    kind: str
    value: int | float | str | bool | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A (possibly qualified) column reference."""

    parts: tuple[str, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Parameter:
    """A ``$name`` query parameter reference."""

    name: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Star:
    """``*`` in a projection list or as a function argument."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UnaryOp:
    """A prefix operator: ``not``, ``-``, or ``+``."""

    operator: str
    operand: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BinaryOp:
    """A binary operator with a canonical spelling.

    Word comparisons normalize to their symbol: ``is greater than`` becomes
    ``>``, ``is at most`` becomes ``<=``. That is what makes the two surface
    syntaxes comparable.
    """

    operator: str
    left: Expression
    right: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class InList:
    """``x in (...)`` and its negated form."""

    expression: Expression
    items: tuple[Expression, ...]
    negated: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Between:
    """``x between a and b`` and its negated form."""

    expression: Expression
    low: Expression
    high: Expression
    negated: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class IsPredicate:
    """``x is [not] null`` and ``x is [not] empty``."""

    expression: Expression
    test: str
    negated: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """A function call, including aggregate calls such as ``count(*)``."""

    name: tuple[str, ...]
    arguments: tuple[Expression, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Cast:
    """``cast(expression as type)``."""

    expression: Expression
    target: TypeExpr
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class When:
    """One ``when ... then ...`` arm."""

    condition: Expression
    result: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Case:
    """``case when ... then ... else ... end``."""

    whens: tuple[When, ...]
    otherwise: Expression | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ListLiteral:
    """``[a, b, c]``."""

    items: tuple[Expression, ...]
    span: SourceSpan


type Expression = (
    Literal
    | ColumnRef
    | Parameter
    | Star
    | UnaryOp
    | BinaryOp
    | InList
    | Between
    | IsPredicate
    | FunctionCall
    | Cast
    | Case
    | ListLiteral
)


@dataclass(frozen=True, slots=True)
class Projection:
    """One projected value, with an optional alias."""

    expression: Expression
    alias: str | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class TableRef:
    """A table reference with an optional alias."""

    name: str
    alias: str | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Join:
    """A join clause; ``kind`` is ``inner``, ``left``, ``right``, ``full``."""

    kind: str
    table: TableRef
    condition: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class OrderItem:
    """One ordering term."""

    expression: Expression
    direction: str
    nulls: str | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Assignment:
    """One ``column = value`` (structured) or ``column to value`` (English)."""

    column: str
    value: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Returning:
    """A ``returning`` clause."""

    projections: tuple[Projection, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SelectStmt:
    """A select statement; clause order follows the language specification."""

    source: TableRef
    joins: tuple[Join, ...]
    projections: tuple[Projection, ...]
    where: Expression | None
    group_by: tuple[Expression, ...]
    having: Expression | None
    order_by: tuple[OrderItem, ...]
    limit: int | None
    offset: int | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class InsertStmt:
    """An insert statement."""

    table: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Expression, ...], ...]
    returning: Returning | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UpdateStmt:
    """An update statement."""

    table: str
    assignments: tuple[Assignment, ...]
    where: Expression | None
    returning: Returning | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DeleteStmt:
    """A delete statement."""

    table: str
    where: Expression | None
    returning: Returning | None
    span: SourceSpan


type Statement = SelectStmt | InsertStmt | UpdateStmt | DeleteStmt


@dataclass(frozen=True, slots=True)
class Attribute:
    """One table column.

    ``nullable`` is always explicit after parsing; the default depends on the
    syntax form only insofar as both forms default to *required*.
    """

    name: str
    type: TypeExpr
    nullable: bool
    primary: bool
    unique: bool
    auto: bool
    indexed: bool
    default: Expression | None
    check: Expression | None
    references: str | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class IndexDecl:
    """A named index declaration."""

    name: str
    columns: tuple[str, ...]
    unique: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UniqueConstraint:
    """A table-level unique constraint."""

    columns: tuple[str, ...]
    span: SourceSpan


type SchemaItem = Attribute | IndexDecl | UniqueConstraint


@dataclass(frozen=True, slots=True)
class TableDecl:
    """A table declaration in either syntax."""

    name: str
    attributes: tuple[Attribute, ...]
    indexes: tuple[IndexDecl, ...]
    unique_constraints: tuple[UniqueConstraint, ...]
    syntax: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class QueryDecl:
    """A named (structured) or anonymous (English) query declaration."""

    name: str | None
    parameters: tuple[tuple[str, TypeExpr], ...]
    statement: Statement
    syntax: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class TransactionDecl:
    """A transaction containing mutations or queries."""

    name: str
    statements: tuple[Statement, ...]
    syntax: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class AlterAction:
    """One action inside ``alter table``."""

    kind: str
    column: str | None
    new_name: str | None
    type: TypeExpr | None
    attribute: Attribute | None
    default: Expression | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class AlterTable:
    """``alter table`` with one or more actions."""

    table: str
    actions: tuple[AlterAction, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class CreateIndexStatement:
    """``create index`` inside a migration."""

    index: IndexDecl
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DropIndexStatement:
    """``drop index`` inside a migration."""

    name: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DropTableStatement:
    """``drop table`` inside a migration."""

    name: str
    span: SourceSpan


type MigrationStep = (
    TableDecl | AlterTable | DropTableStatement | CreateIndexStatement | DropIndexStatement
)


@dataclass(frozen=True, slots=True)
class MigrationDecl:
    """A versioned migration with optional up/down bodies."""

    name: str
    version: int
    entity: str | None
    description: str | None
    up: tuple[MigrationStep, ...]
    down: tuple[MigrationStep, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ConversionDecl:
    """A schema conversion request."""

    source_adapter: str
    target_adapter: str
    profile: str
    entities: tuple[str, ...] | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ImportDecl:
    """A module import with an optional alias."""

    module: str
    alias: str | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Program:
    """A whole source file."""

    declarations: tuple[Declaration, ...]
    span: SourceSpan


type Declaration = (
    ImportDecl | TableDecl | QueryDecl | TransactionDecl | MigrationDecl | ConversionDecl
)


def _span_to_data(span: SourceSpan) -> dict[str, object]:
    return {
        "file": span.file,
        "start": {
            "offset": span.start.offset,
            "line": span.start.line,
            "column": span.start.column,
        },
        "end": {
            "offset": span.end.offset,
            "line": span.end.line,
            "column": span.end.column,
        },
    }


def to_data(value: object) -> object:
    """Convert an AST value into JSON-safe, deterministically ordered data."""
    if isinstance(value, SourceSpan):
        return _span_to_data(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_data(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple | list):
        return [to_data(item) for item in value]
    return value


def to_json(node: Program, *, indent: int = 2) -> str:
    """Serialize a program to deterministic JSON with a trailing newline."""
    return json.dumps(to_data(node), indent=indent, ensure_ascii=False) + "\n"


SEMANTIC_OMIT: Final[frozenset[str]] = frozenset({"span", "syntax"})
"""Fields that describe presentation, not meaning."""


def semantic_view(value: object) -> object:
    """Return the meaning-bearing part of an AST value.

    Spans and the surface syntax marker are removed so that the English-like and
    structured spellings of the same program compare equal. Everything else,
    including declaration order, is preserved.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: semantic_view(getattr(value, field.name))
            for field in fields(value)
            if field.name not in SEMANTIC_OMIT
        }
    if isinstance(value, tuple | list):
        return [semantic_view(item) for item in value]
    return value


def walk(node: object) -> Iterator[object]:
    """Yield every AST node reachable from ``node`` in declaration order."""
    if is_dataclass(node) and not isinstance(node, type):
        yield node
        for field in fields(node):
            yield from walk(getattr(node, field.name))
    elif isinstance(node, tuple | list):
        for item in node:
            yield from walk(item)
