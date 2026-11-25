"""Deterministic formatting for Querly source.

Formatting works from the parsed AST when the source is valid, so the output is
canonical.  When parsing fails it falls back to conservative whitespace cleanup
instead of guessing at the author's intent.
"""

from __future__ import annotations

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
    TypeExpr,
    UnaryOp,
    UpdateStmt,
)


def _type(type_expr: TypeExpr) -> str:
    name = type_expr.name
    if not type_expr.arguments:
        return name
    if name == "decimal":
        return "decimal(" + ", ".join(str(argument) for argument in type_expr.arguments) + ")"
    if name in ("text", "binary", "vector"):
        return f"{name}({type_expr.arguments[0]})"
    if name in ("list", "set"):
        return f"{name}<{_type(type_expr.arguments[0]) if isinstance(type_expr.arguments[0], TypeExpr) else type_expr.arguments[0]}>"
    if name == "map":
        key = _type(type_expr.arguments[0]) if isinstance(type_expr.arguments[0], TypeExpr) else type_expr.arguments[0]
        value = _type(type_expr.arguments[1]) if len(type_expr.arguments) > 1 and isinstance(type_expr.arguments[1], TypeExpr) else ""
        return f"map<{key}, {value}>"
    if name == "reference":
        return f"reference({type_expr.arguments[0]})"
    return name


def _expression(expression: Expression) -> str:
    if isinstance(expression, Literal):
        if expression.kind == "string":
            escaped = str(expression.value).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if expression.kind == "null":
            return "null"
        if expression.kind == "boolean":
            return "true" if expression.value else "false"
        return str(expression.value)
    if isinstance(expression, ColumnRef):
        return ".".join(expression.parts)
    if isinstance(expression, Parameter):
        return f"${expression.name}"
    if isinstance(expression, Star):
        return "*"
    if isinstance(expression, UnaryOp):
        if expression.operator == "not":
            return f"not {_expression(expression.operand)}"
        return f"{expression.operator}{_expression(expression.operand)}"
    if isinstance(expression, BinaryOp):
        if expression.operator in ("and", "or"):
            return f"{_expression(expression.left)} {expression.operator} {_expression(expression.right)}"
        return f"{_expression(expression.left)} {expression.operator} {_expression(expression.right)}"
    if isinstance(expression, InList):
        values = ", ".join(_expression(item) for item in expression.items)
        return f"{_expression(expression.expression)} {'not ' if expression.negated else ''}in ({values})"
    if isinstance(expression, Between):
        return (
            f"{_expression(expression.expression)} {'not ' if expression.negated else ''}"
            f"between {_expression(expression.low)} and {_expression(expression.high)}"
        )
    if isinstance(expression, IsPredicate):
        return f"{_expression(expression.expression)} is {'not ' if expression.negated else ''}{expression.test}"
    if isinstance(expression, FunctionCall):
        return f"{'.'.join(expression.name)}(" + ", ".join(_expression(argument) for argument in expression.arguments) + ")"
    if isinstance(expression, Cast):
        return f"cast({_expression(expression.expression)} as {_type(expression.target)})"
    if isinstance(expression, Case):
        parts = ["case"]
        for when in expression.whens:
            parts.append(f"when {_expression(when.condition)} then {_expression(when.result)}")
        if expression.otherwise is not None:
            parts.append(f"else {_expression(expression.otherwise)}")
        parts.append("end")
        return " ".join(parts)
    if isinstance(expression, ListLiteral):
        return "[" + ", ".join(_expression(item) for item in expression.items) + "]"
    return "<unsupported>"


def _projection(projection: Projection) -> str:
    value = _expression(projection.expression)
    if projection.alias:
        value += f" as {projection.alias}"
    return value


def _attribute(attribute) -> str:
    parts = [f"{attribute.name}: {_type(attribute.type)}"]
    if attribute.nullable:
        parts[0] += "?"
    if attribute.primary:
        parts.append("@primary")
    if attribute.unique:
        parts.append("@unique")
    if attribute.auto:
        parts.append("@auto")
    if attribute.indexed:
        parts.append("@index")
    if attribute.default is not None:
        parts.append(f"@default({_expression(attribute.default)})")
    if attribute.check is not None:
        parts.append(f"@check({_expression(attribute.check)})")
    if attribute.references:
        parts.append(f"@references({attribute.references})")
    return " ".join(parts)


def _statement(statement: object, indent: str) -> list[str]:
    if isinstance(statement, SelectStmt):
        lines = [f"{indent}from {statement.source.name}" + (f" as {statement.source.alias}" if statement.source.alias else "")]
        lines.append(f"{indent}select " + ", ".join(_projection(item) for item in statement.projections))
        for join in statement.joins:
            condition = _expression(join.condition)
            lines.append(
                f"{indent}{join.kind} join {join.table.name}"
                + (f" as {join.table.alias}" if join.table.alias else "")
                + f" on {condition}"
            )
        if statement.where is not None:
            lines.append(f"{indent}where {_expression(statement.where)}")
        if statement.group_by:
            lines.append(f"{indent}group by " + ", ".join(_expression(item) for item in statement.group_by))
        if statement.having is not None:
            lines.append(f"{indent}having {_expression(statement.having)}")
        if statement.order_by:
            order = ", ".join(
                f"{_expression(item.expression)} {'desc' if item.direction == 'descending' else 'asc'}"
                for item in statement.order_by
            )
            lines.append(f"{indent}order by {order}")
        if statement.limit is not None:
            lines.append(f"{indent}limit {statement.limit}")
        if statement.offset is not None:
            lines.append(f"{indent}offset {statement.offset}")
        return lines
    if isinstance(statement, InsertStmt):
        columns = ", ".join(statement.columns)
        rows = "; ".join(
            "(" + ", ".join(_expression(value) for value in row) + ")" for row in statement.rows
        )
        return [f"{indent}insert into {statement.table} ({columns}) values {rows}"]
    if isinstance(statement, UpdateStmt):
        assignments = ", ".join(
            f"{assignment.column} = {_expression(assignment.value)}" for assignment in statement.assignments
        )
        line = f"{indent}update {statement.table} set {assignments}"
        if statement.where is not None:
            line += f" where {_expression(statement.where)}"
        return [line]
    if isinstance(statement, DeleteStmt):
        line = f"{indent}delete from {statement.table}"
        if statement.where is not None:
            line += f" where {_expression(statement.where)}"
        return [line]
    return [f"{indent}<unsupported statement>"]


def format_program(program: Program) -> str:
    blocks: list[str] = []
    for declaration in program.declarations:
        if isinstance(declaration, TableDecl):
            lines = [f"table {declaration.name} {{"]
            for attribute in declaration.attributes:
                lines.append(f"  {_attribute(attribute)}")
            for index in declaration.indexes:
                suffix = " @unique" if index.unique else ""
                lines.append(f"  index {index.name} ({', '.join(index.columns)}){suffix}")
            for unique in declaration.unique_constraints:
                lines.append(f"  unique ({', '.join(unique.columns)})")
            lines.append("}")
            blocks.append("\n".join(lines))
        elif isinstance(declaration, QueryDecl):
            if declaration.name:
                parameters = ""
                if declaration.parameters:
                    parameters = "(" + ", ".join(f"{name}: {_type(type_expr)}" for name, type_expr in declaration.parameters) + ")"
                lines = [f"query {declaration.name}{parameters} {{"]
                lines.extend(_statement(declaration.statement, "  "))
                lines.append("}")
                blocks.append("\n".join(lines))
            else:
                lines = _statement(declaration.statement, "")
                blocks.append("\n".join(lines) + ";")
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def conservative_format(text: str) -> str:
    lines = []
    depth = 0
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("}"):
            depth = max(0, depth - 1)
        line = "  " * depth + stripped
        line = line.replace(" ,", ",")
        lines.append(line.rstrip())
        depth += stripped.count("{") - stripped.count("}")
        depth = max(0, depth)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"
