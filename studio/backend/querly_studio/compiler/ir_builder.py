"""Build real upstream IR documents from Studio-parsed AST nodes."""

from __future__ import annotations

from typing import Any

from oq.core.ast import (
    Attribute,
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
    TypeExpr,
    UnaryOp,
    UpdateStmt,
)
from oq.ir import (
    Attribute as IRAttribute,
    Constraint,
    Entity,
    Index as IRIndex,
    Projection as IRProjection,
    QueryDocument,
    Safety,
    SchemaDocument,
    TableRef as IRTableRef,
    Type as IRType,
)


def type_to_ir(type_expr: TypeExpr) -> IRType:
    canonical = type_expr.canonical_name
    arguments = type_expr.arguments
    if canonical == "decimal":
        return IRType(
            kind="decimal",
            precision=arguments[0] if len(arguments) > 0 and isinstance(arguments[0], int) else None,
            scale=arguments[1] if len(arguments) > 1 and isinstance(arguments[1], int) else None,
        )
    if canonical in ("text", "binary"):
        return IRType(
            kind=canonical,
            max_length=arguments[0] if arguments and isinstance(arguments[0], int) else None,
        )
    if canonical == "vector":
        return IRType(
            kind="vector",
            dimension=arguments[0] if arguments and isinstance(arguments[0], int) else None,
        )
    if canonical in ("list", "set"):
        return IRType(kind=canonical, element=type_to_ir(arguments[0]) if arguments and isinstance(arguments[0], TypeExpr) else None)
    if canonical == "map":
        return IRType(
            kind="map",
            key=type_to_ir(arguments[0]) if arguments and isinstance(arguments[0], TypeExpr) else None,
            value=type_to_ir(arguments[1]) if len(arguments) > 1 and isinstance(arguments[1], TypeExpr) else None,
        )
    if canonical == "reference":
        return IRType(kind="reference", entity=str(arguments[0]) if arguments else None)
    return IRType(kind=canonical)


def attribute_to_ir(attribute: Attribute) -> IRAttribute:
    return IRAttribute(
        name=attribute.name,
        type=type_to_ir(attribute.type),
        nullable=attribute.nullable,
        primary=attribute.primary,
        unique=attribute.unique,
        auto=attribute.auto,
        indexed=attribute.indexed,
        default=expression_to_ir(attribute.default) if attribute.default is not None else None,
        check=expression_to_ir(attribute.check) if attribute.check is not None else None,
        references=attribute.references,
        span={
            "start": attribute.span.start.offset,
            "end": attribute.span.end.offset,
        },
    )


def table_to_entity(table: TableDecl) -> Entity:
    attributes = [attribute_to_ir(attribute) for attribute in table.attributes]
    primary_key = [attribute.name for attribute in table.attributes if attribute.primary]
    indexes = [
        IRIndex(name=index.name, columns=list(index.columns), unique=index.unique)
        for index in table.indexes
    ]
    unique_constraints = [
        Constraint(kind="unique", columns=list(unique.columns))
        for unique in table.unique_constraints
    ]
    for attribute in table.attributes:
        if attribute.unique and not attribute.primary:
            unique_constraints.append(Constraint(kind="unique", columns=[attribute.name]))
    return Entity(
        name=table.name,
        attributes=attributes,
        primary_key=primary_key,
        indexes=indexes,
        unique_constraints=unique_constraints,
    )


def schema_document(program: Program) -> SchemaDocument | None:
    tables = [declaration for declaration in program.declarations if isinstance(declaration, TableDecl)]
    if not tables:
        return None
    return SchemaDocument(entities=[table_to_entity(table) for table in tables])


def _expression_ref(expression: Expression) -> dict[str, Any] | None:
    if isinstance(expression, ColumnRef):
        return {"kind": "column", "name": ".".join(expression.parts)}
    return None


def expression_to_ir(expression: Expression) -> dict[str, Any]:
    if expression is None:
        return {"kind": "literal", "literal_value": None}
    if isinstance(expression, Literal):
        return {
            "kind": "literal",
            "literal_type": {"kind": expression.kind},
            "literal_value": expression.value,
        }
    if isinstance(expression, ColumnRef):
        return {"kind": "column", "name": ".".join(expression.parts)}
    if isinstance(expression, Parameter):
        return {"kind": "parameter", "name": expression.name}
    if isinstance(expression, Star):
        return {"kind": "star"}
    if isinstance(expression, UnaryOp):
        return {
            "kind": "unary",
            "operator": expression.operator,
            "right": expression_to_ir(expression.operand),
        }
    if isinstance(expression, BinaryOp):
        ref = _expression_ref(expression.left)
        return {
            "kind": "binary",
            "operator": expression.operator,
            "left": expression_to_ir(expression.left),
            "right": expression_to_ir(expression.right),
            **({"name": ref["name"]} if ref else {}),
        }
    if isinstance(expression, InList):
        return {
            "kind": "in",
            "left": expression_to_ir(expression.expression),
            "items": [expression_to_ir(item) for item in expression.items],
            "negated": expression.negated,
        }
    if isinstance(expression, Between):
        return {
            "kind": "between",
            "left": expression_to_ir(expression.expression),
            "low": expression_to_ir(expression.low),
            "high": expression_to_ir(expression.high),
            "negated": expression.negated,
        }
    if isinstance(expression, IsPredicate):
        return {
            "kind": "is",
            "left": expression_to_ir(expression.expression),
            "test": expression.test,
            "negated": expression.negated,
        }
    if isinstance(expression, FunctionCall):
        return {
            "kind": "call",
            "function": ".".join(expression.name),
            "arguments": [expression_to_ir(argument) for argument in expression.arguments],
        }
    if isinstance(expression, Cast):
        return {
            "kind": "cast",
            "left": expression_to_ir(expression.expression),
            "cast_type": {"kind": expression.target.canonical_name},
        }
    if isinstance(expression, Case):
        return {
            "kind": "case",
            "whens": [
                {
                    "when": expression_to_ir(when.condition),
                    "then": expression_to_ir(when.result),
                }
                for when in expression.whens
            ],
            "otherwise": expression_to_ir(expression.otherwise) if expression.otherwise else None,
        }
    if isinstance(expression, ListLiteral):
        return {"kind": "list", "items": [expression_to_ir(item) for item in expression.items]}
    return {"kind": "unknown"}


def _projection_to_ir(projection: Projection) -> IRProjection:
    return IRProjection(expression=expression_to_ir(projection.expression), alias=projection.alias)


def _safety_for(statement: object) -> Safety:
    if isinstance(statement, (InsertStmt,)):
        return Safety(classification="write")
    if isinstance(statement, UpdateStmt):
        return Safety(classification="write" if statement.where is not None else "destructive_write", unfiltered=statement.where is None, destructive=statement.where is None)
    if isinstance(statement, DeleteStmt):
        return Safety(classification="write" if statement.where is not None else "destructive_write", unfiltered=statement.where is None, destructive=statement.where is None)
    return Safety(classification="read")


def query_document(declaration: QueryDecl) -> QueryDocument | None:
    statement = declaration.statement
    if isinstance(statement, SelectStmt):
        return QueryDocument(
            name=declaration.name,
            operation="select",
            parameters=[],
            source=IRTableRef(entity=statement.source.name, alias=statement.source.alias),
            joins=[],
            projection=[_projection_to_ir(item) for item in statement.projections],
            filter=expression_to_ir(statement.where) if statement.where is not None else None,
            group_by=[expression_to_ir(item) for item in statement.group_by],
            having=expression_to_ir(statement.having) if statement.having is not None else None,
            order_by=[
                {
                    "expression": expression_to_ir(item.expression),
                    "direction": item.direction,
                    "nulls": item.nulls or "last",
                }
                for item in statement.order_by
            ],
            limit=statement.limit,
            offset=statement.offset,
            safety=Safety(classification="read"),
        )
    if isinstance(statement, InsertStmt):
        return QueryDocument(
            name=declaration.name,
            operation="insert",
            source=IRTableRef(entity=statement.table),
            safety=Safety(classification="write"),
        )
    if isinstance(statement, UpdateStmt):
        return QueryDocument(
            name=declaration.name,
            operation="update",
            source=IRTableRef(entity=statement.table),
            filter=expression_to_ir(statement.where) if statement.where is not None else None,
            safety=_safety_for(statement),
        )
    if isinstance(statement, DeleteStmt):
        return QueryDocument(
            name=declaration.name,
            operation="delete",
            source=IRTableRef(entity=statement.table),
            filter=expression_to_ir(statement.where) if statement.where is not None else None,
            safety=_safety_for(statement),
        )
    return None
