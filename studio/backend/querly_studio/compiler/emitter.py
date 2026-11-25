"""SQL emission for the Studio compiler bridge.

The emitter is deliberately separate from parsing.  It turns the upstream AST
into an adapter-ready plan artifact and records parameter order, safety
classification and warnings.  It does not execute anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oq.core.ast import (
    Assignment,
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
    IndexDecl,
    InsertStmt,
    IsPredicate,
    Join,
    ListLiteral,
    Literal,
    OrderItem,
    Parameter,
    Program,
    Projection,
    QueryDecl,
    SelectStmt,
    Star,
    Statement,
    TableDecl,
    TableRef,
    TypeExpr,
    UnaryOp,
    UniqueConstraint,
    UpdateStmt,
)
from oq.core.diagnostics import SourceSpan
from oq.ir import Safety

DIALECT_QUOTES: dict[str, tuple[str, str]] = {
    "sqlite": ('"', '"'),
    "postgres": ('"', '"'),
    "cockroachdb": ('"', '"'),
    "mysql": ("`", "`"),
    "mariadb": ("`", "`"),
    "clickhouse": ("`", "`"),
    "sqlserver": ("[", "]"),
    "oracle": ('"', '"'),
    "db2": ('"', '"'),
}

TYPE_MAPS: dict[str, dict[str, str]] = {
    "sqlite": {
        "integer": "INTEGER",
        "float": "REAL",
        "decimal": "NUMERIC",
        "text": "TEXT",
        "boolean": "INTEGER",
        "date": "TEXT",
        "time": "TEXT",
        "timestamp": "TEXT",
        "duration": "TEXT",
        "uuid": "TEXT",
        "email": "TEXT",
        "url": "TEXT",
        "json": "TEXT",
        "binary": "BLOB",
        "geo": "TEXT",
        "vector": "TEXT",
        "reference": "INTEGER",
    },
    "postgres": {
        "integer": "INTEGER",
        "float": "DOUBLE PRECISION",
        "decimal": "NUMERIC",
        "text": "TEXT",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "time": "TIME",
        "timestamp": "TIMESTAMP",
        "duration": "INTERVAL",
        "uuid": "UUID",
        "email": "TEXT",
        "url": "TEXT",
        "json": "JSONB",
        "binary": "BYTEA",
        "geo": "GEOGRAPHY",
        "vector": "REAL[]",
        "reference": "INTEGER",
    },
    "mysql": {
        "integer": "INT",
        "float": "DOUBLE",
        "decimal": "DECIMAL",
        "text": "TEXT",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "time": "TIME",
        "timestamp": "DATETIME",
        "duration": "TIME",
        "uuid": "CHAR(36)",
        "email": "VARCHAR(320)",
        "url": "TEXT",
        "json": "JSON",
        "binary": "BLOB",
        "geo": "GEOMETRY",
        "vector": "TEXT",
        "reference": "INT",
    },
    "sqlserver": {
        "integer": "INT",
        "float": "FLOAT",
        "decimal": "DECIMAL",
        "text": "NVARCHAR(MAX)",
        "boolean": "BIT",
        "date": "DATE",
        "time": "TIME",
        "timestamp": "DATETIME2",
        "duration": "NVARCHAR(64)",
        "uuid": "UNIQUEIDENTIFIER",
        "email": "NVARCHAR(320)",
        "url": "NVARCHAR(2048)",
        "json": "NVARCHAR(MAX)",
        "binary": "VARBINARY(MAX)",
        "geo": "GEOGRAPHY",
        "vector": "NVARCHAR(MAX)",
        "reference": "INT",
    },
    "oracle": {
        "integer": "NUMBER(19)",
        "float": "BINARY_DOUBLE",
        "decimal": "NUMBER",
        "text": "CLOB",
        "boolean": "NUMBER(1)",
        "date": "DATE",
        "time": "VARCHAR2(32)",
        "timestamp": "TIMESTAMP",
        "duration": "VARCHAR2(64)",
        "uuid": "RAW(16)",
        "email": "VARCHAR2(320)",
        "url": "VARCHAR2(2048)",
        "json": "JSON",
        "binary": "BLOB",
        "geo": "SDO_GEOMETRY",
        "vector": "VECTOR",
        "reference": "NUMBER(19)",
    },
    "clickhouse": {
        "integer": "Int64",
        "float": "Float64",
        "decimal": "Decimal(38, 9)",
        "text": "String",
        "boolean": "UInt8",
        "date": "Date",
        "time": "String",
        "timestamp": "DateTime64(3)",
        "duration": "String",
        "uuid": "UUID",
        "email": "String",
        "url": "String",
        "json": "String",
        "binary": "String",
        "geo": "String",
        "vector": "Array(Float32)",
        "reference": "Int64",
    },
}

SAFE_IDENTIFIER = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


@dataclass(slots=True)
class CompiledStatement:
    """One executable unit produced from a declaration."""

    id: str
    kind: str
    sql: str
    safety: Safety
    span: SourceSpan
    parameters: list[dict[str, Any]] = field(default_factory=list)
    parameter_values: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_name: str | None = None

    @property
    def is_ddl(self) -> bool:
        return self.kind in {"create_table", "create_index", "drop_table", "alter_table"}


class EmitContext:
    def __init__(self, dialect: str, *, bind_literals: bool = False) -> None:
        normalized = dialect.lower()
        if normalized not in TYPE_MAPS:
            normalized = "sqlite"
        self.dialect = normalized
        self.bind_literals = bind_literals
        self.open_quote, self.close_quote = DIALECT_QUOTES.get(normalized, ('"', '"'))
        self.parameters: list[dict[str, Any]] = []
        self.parameter_values: list[Any] = []
        self.warnings: list[str] = []

    def quote(self, identifier: str) -> str:
        if "\x00" in identifier:
            raise ValueError("identifier contains a NUL byte")
        escaped = identifier.replace(self.close_quote, self.close_quote * 2)
        return f"{self.open_quote}{escaped}{self.close_quote}"

    def add_parameter(self, name: str, *, value: Any = None, source: str = "named") -> str:
        self.parameters.append(
            {"name": name, "index": len(self.parameters), "source": source}
        )
        self.parameter_values.append(value)
        return "?"

    def map_type(self, type_expr: TypeExpr) -> str:
        canonical = type_expr.canonical_name
        mapping = TYPE_MAPS[self.dialect]
        native = mapping.get(canonical, "TEXT")
        if canonical == "text" and type_expr.arguments:
            length = type_expr.arguments[0]
            if isinstance(length, int) and length > 0 and self.dialect in ("mysql", "sqlserver", "oracle", "db2", "postgres"):
                return f"VARCHAR({length})" if self.dialect != "sqlserver" else f"NVARCHAR({length})"
        if canonical == "decimal" and type_expr.arguments:
            precision = type_expr.arguments[0]
            scale = type_expr.arguments[1] if len(type_expr.arguments) > 1 else None
            if isinstance(precision, int):
                return f"NUMERIC({precision},{scale if isinstance(scale, int) else 0})"
        if canonical == "vector" and type_expr.arguments:
            dimension = type_expr.arguments[0]
            if self.dialect == "postgres" and isinstance(dimension, int):
                return f"vector({dimension})"
            if self.dialect == "clickhouse" and isinstance(dimension, int):
                return f"Array(Float32)"
        return native


def _literal_sql(value: Any, kind: str, dialect: str) -> str:
    if kind == "null" or value is None:
        return "NULL"
    if kind == "boolean":
        if dialect in ("sqlite", "sqlserver"):
            return "1" if value else "0"
        return "TRUE" if value else "FALSE"
    if kind in ("integer", "decimal", "float"):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def emit_expression(expression: Expression, context: EmitContext) -> str:
    if isinstance(expression, Literal):
        if context.bind_literals and expression.kind != "null":
            return context.add_parameter(
                f"lit{len(context.parameters)}",
                value=expression.value,
                source="literal",
            )
        return _literal_sql(expression.value, expression.kind, context.dialect)
    if isinstance(expression, ColumnRef):
        return ".".join(context.quote(part) for part in expression.parts)
    if isinstance(expression, Parameter):
        return context.add_parameter(expression.name)
    if isinstance(expression, Star):
        return "*"
    if isinstance(expression, UnaryOp):
        operand = emit_expression(expression.operand, context)
        if expression.operator == "not":
            return f"NOT ({operand})"
        return f"{expression.operator}{operand}"
    if isinstance(expression, BinaryOp):
        operator = expression.operator
        if operator == "!=":
            operator = "<>"
        left = emit_expression(expression.left, context)
        right = emit_expression(expression.right, context)
        if operator in ("and", "or"):
            return f"({left} {operator.upper()} {right})"
        if operator == "like":
            return f"{left} LIKE {right}"
        return f"{left} {operator} {right}"
    if isinstance(expression, InList):
        values = ", ".join(emit_expression(item, context) for item in expression.items)
        negation = "NOT " if expression.negated else ""
        return f"{emit_expression(expression.expression, context)} {negation}IN ({values})"
    if isinstance(expression, Between):
        negation = "NOT " if expression.negated else ""
        return (
            f"{emit_expression(expression.expression, context)} {negation}BETWEEN "
            f"{emit_expression(expression.low, context)} AND {emit_expression(expression.high, context)}"
        )
    if isinstance(expression, IsPredicate):
        negation = "NOT " if expression.negated else ""
        value = emit_expression(expression.expression, context)
        if expression.test == "null":
            return f"{value} IS {negation}NULL"
        if expression.test == "empty":
            if context.dialect == "sqlite":
                return f"({value} IS {negation}NULL OR {value} = '')"
            return f"{value} IS {negation}NULL"
        return f"{value} IS {negation}NULL"
    if isinstance(expression, FunctionCall):
        name = ".".join(expression.name).lower()
        if name == "now":
            return "CURRENT_TIMESTAMP"
        if name in ("current_timestamp", "current_date", "current_time"):
            return name.upper()
        args = ", ".join(emit_expression(argument, context) for argument in expression.arguments)
        return f"{'.'.join(expression.name)}({args})"
    if isinstance(expression, Cast):
        return f"CAST({emit_expression(expression.expression, context)} AS {context.map_type(expression.target)})"
    if isinstance(expression, Case):
        parts = ["CASE"]
        for when in expression.whens:
            parts.append(
                f"WHEN {emit_expression(when.condition, context)} THEN {emit_expression(when.result, context)}"
            )
        if expression.otherwise is not None:
            parts.append(f"ELSE {emit_expression(expression.otherwise, context)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(expression, ListLiteral):
        values = ", ".join(emit_expression(item, context) for item in expression.items)
        return f"({values})"
    return "NULL"


def emit_projection(projection: Projection, context: EmitContext) -> str:
    sql = emit_expression(projection.expression, context)
    if projection.alias:
        sql += f" AS {context.quote(projection.alias)}"
    return sql


def emit_table_ref(reference: TableRef, context: EmitContext) -> str:
    sql = context.quote(reference.name)
    if reference.alias:
        sql += f" AS {context.quote(reference.alias)}"
    return sql


def emit_join(join: Join, context: EmitContext) -> str:
    kind = join.kind.upper()
    if kind == "INNER":
        kind = "INNER"
    return (
        f"{kind} JOIN {emit_table_ref(join.table, context)} "
        f"ON {emit_expression(join.condition, context)}"
    )


def _emit_order_item(item: OrderItem, context: EmitContext) -> str:
    direction = "DESC" if item.direction == "descending" else "ASC"
    sql = f"{emit_expression(item.expression, context)} {direction}"
    if item.nulls:
        sql += f" NULLS {'FIRST' if item.nulls == 'first' else 'LAST'}"
    return sql


def emit_select(statement: SelectStmt, context: EmitContext) -> str:
    projections = ", ".join(emit_projection(item, context) for item in statement.projections)
    sql = f"SELECT {projections} FROM {emit_table_ref(statement.source, context)}"
    for join in statement.joins:
        sql += " " + emit_join(join, context)
    if statement.where is not None:
        sql += f" WHERE {emit_expression(statement.where, context)}"
    if statement.group_by:
        sql += " GROUP BY " + ", ".join(emit_expression(item, context) for item in statement.group_by)
    if statement.having is not None:
        sql += f" HAVING {emit_expression(statement.having, context)}"
    if statement.order_by:
        sql += " ORDER BY " + ", ".join(_emit_order_item(item, context) for item in statement.order_by)
    if statement.limit is not None:
        sql += f" LIMIT {statement.limit}"
    if statement.offset is not None:
        if statement.limit is None and context.dialect not in ("sqlite", "postgres", "mysql"):
            context.warnings.append(
                f"OFFSET without LIMIT may not be supported by {context.dialect}"
            )
        sql += f" OFFSET {statement.offset}"
    return sql


def _can_return(dialect: str) -> bool:
    return dialect in ("sqlite", "postgres", "cockroachdb", "sqlserver")


def emit_insert(statement: InsertStmt, context: EmitContext) -> str:
    columns = ", ".join(context.quote(column) for column in statement.columns)
    rows = []
    for row in statement.rows:
        if len(row) != len(statement.columns):
            raise ValueError(
                f"insert has {len(statement.columns)} columns but {len(row)} values in one row"
            )
        rows.append("(" + ", ".join(emit_expression(value, context) for value in row) + ")")
    sql = f"INSERT INTO {context.quote(statement.table)} ({columns}) VALUES {', '.join(rows)}"
    if statement.returning is not None:
        if not _can_return(context.dialect):
            context.warnings.append(
                f"{context.dialect} does not support RETURNING in this compiler profile; the clause was omitted"
            )
        else:
            sql += " RETURNING " + ", ".join(
                emit_projection(item, context) for item in statement.returning.projections
            )
    return sql


def emit_update(statement: UpdateStmt, context: EmitContext) -> str:
    assignments = ", ".join(
        f"{context.quote(assignment.column)} = {emit_expression(assignment.value, context)}"
        for assignment in statement.assignments
    )
    sql = f"UPDATE {context.quote(statement.table)} SET {assignments}"
    if statement.where is not None:
        sql += f" WHERE {emit_expression(statement.where, context)}"
    if statement.returning is not None:
        if _can_return(context.dialect):
            sql += " RETURNING " + ", ".join(
                emit_projection(item, context) for item in statement.returning.projections
            )
        else:
            context.warnings.append(
                f"{context.dialect} does not support RETURNING in this compiler profile; the clause was omitted"
            )
    return sql


def emit_delete(statement: DeleteStmt, context: EmitContext) -> str:
    sql = f"DELETE FROM {context.quote(statement.table)}"
    if statement.where is not None:
        sql += f" WHERE {emit_expression(statement.where, context)}"
    if statement.returning is not None:
        if _can_return(context.dialect):
            sql += " RETURNING " + ", ".join(
                emit_projection(item, context) for item in statement.returning.projections
            )
        else:
            context.warnings.append(
                f"{context.dialect} does not support RETURNING in this compiler profile; the clause was omitted"
            )
    return sql


def _emit_attribute(attribute: Attribute, context: EmitContext) -> str:
    parts = [context.quote(attribute.name), context.map_type(attribute.type)]
    if not attribute.nullable:
        parts.append("NOT NULL")
    if attribute.primary:
        if attribute.auto and context.dialect == "sqlite":
            # SQLite AUTOINCREMENT is only valid as INTEGER PRIMARY KEY.
            if context.map_type(attribute.type) == "INTEGER":
                return f"{context.quote(attribute.name)} INTEGER PRIMARY KEY AUTOINCREMENT"
        parts.append("PRIMARY KEY")
    if attribute.auto:
        if context.dialect == "postgres":
            parts.append("GENERATED BY DEFAULT AS IDENTITY")
        elif context.dialect in ("mysql", "mariadb"):
            parts.append("AUTO_INCREMENT")
        elif context.dialect == "sqlserver":
            parts.append("IDENTITY(1,1)")
    if attribute.unique and not attribute.primary:
        parts.append("UNIQUE")
    if attribute.default is not None:
        parts.append(f"DEFAULT {emit_expression(attribute.default, context)}")
    if attribute.check is not None:
        parts.append(f"CHECK ({emit_expression(attribute.check, context)})")
    if attribute.references:
        parts.append(f"REFERENCES {context.quote(attribute.references)}")
    return " ".join(parts)


def emit_create_table(table: TableDecl, context: EmitContext) -> list[str]:
    columns = [_emit_attribute(attribute, context) for attribute in table.attributes]
    primary_columns = [
        attribute.name for attribute in table.attributes if attribute.primary and not attribute.auto
    ]
    if len(primary_columns) > 1:
        columns.append(
            "PRIMARY KEY (" + ", ".join(context.quote(name) for name in primary_columns) + ")"
        )
    for unique in table.unique_constraints:
        columns.append(
            "UNIQUE (" + ", ".join(context.quote(name) for name in unique.columns) + ")"
        )
    statements = [
        f"CREATE TABLE {context.quote(table.name)} (\n  " + ",\n  ".join(columns) + "\n)"
    ]
    for index in table.indexes:
        statements.append(_emit_index(table.name, index, context))
    return statements


def _emit_index(table_name: str, index: IndexDecl, context: EmitContext) -> str:
    unique = "UNIQUE " if index.unique else ""
    columns = ", ".join(context.quote(column) for column in index.columns)
    return (
        f"CREATE {unique}INDEX {context.quote(index.name)} "
        f"ON {context.quote(table_name)} ({columns})"
    )


def classify(statement: Statement) -> Safety:
    if isinstance(statement, SelectStmt):
        return Safety(classification="read", unfiltered=False, destructive=False)
    if isinstance(statement, InsertStmt):
        return Safety(classification="write", unfiltered=False, destructive=False)
    if isinstance(statement, UpdateStmt):
        unfiltered = statement.where is None
        return Safety(
            classification="destructive_write" if unfiltered else "write",
            unfiltered=unfiltered,
            destructive=unfiltered,
        )
    if isinstance(statement, DeleteStmt):
        unfiltered = statement.where is None
        return Safety(
            classification="destructive_write" if unfiltered else "write",
            unfiltered=unfiltered,
            destructive=unfiltered,
        )
    return Safety(classification="write")


def compile_statement(
    statement: Statement,
    context: EmitContext,
    *,
    name: str | None,
    span: SourceSpan,
    index: int,
) -> CompiledStatement:
    if isinstance(statement, SelectStmt):
        sql = emit_select(statement, context)
        kind = "select"
    elif isinstance(statement, InsertStmt):
        sql = emit_insert(statement, context)
        kind = "insert"
    elif isinstance(statement, UpdateStmt):
        sql = emit_update(statement, context)
        kind = "update"
    elif isinstance(statement, DeleteStmt):
        sql = emit_delete(statement, context)
        kind = "delete"
    else:  # pragma: no cover - the AST is closed
        raise TypeError(f"unsupported statement {type(statement).__name__}")

    return CompiledStatement(
        id=f"stmt-{index}",
        kind=kind,
        sql=sql + ";",
        safety=classify(statement),
        span=span,
        parameters=context.parameters,
        parameter_values=context.parameter_values,
        warnings=list(context.warnings),
        source_name=name,
    )


@dataclass(slots=True)
class EmittedProgram:
    statements: list[CompiledStatement]
    warnings: list[str]


def emit_program(program: Program, dialect: str) -> EmittedProgram:
    statements: list[CompiledStatement] = []
    warnings: list[str] = []
    counter = 0

    for declaration in program.declarations:
        if isinstance(declaration, TableDecl):
            context = EmitContext(dialect)
            ddl = emit_create_table(declaration, context)
            for sql in ddl:
                counter += 1
                statements.append(
                    CompiledStatement(
                        id=f"stmt-{counter}",
                        kind="create_index" if sql.lstrip().upper().startswith("CREATE INDEX") or sql.lstrip().upper().startswith("CREATE UNIQUE INDEX") else "create_table",
                        sql=sql + ";",
                        safety=Safety(classification="ddl"),
                        span=declaration.span,
                        warnings=list(context.warnings),
                        source_name=declaration.name,
                    )
                )
            warnings.extend(context.warnings)
        elif isinstance(declaration, QueryDecl):
            context = EmitContext(dialect, bind_literals=True)
            try:
                compiled = compile_statement(
                    declaration.statement,
                    context,
                    name=declaration.name,
                    span=declaration.span,
                    index=counter + 1,
                )
            except ValueError as exc:
                warnings.append(str(exc))
                continue
            counter += 1
            compiled.id = f"stmt-{counter}"
            statements.append(compiled)
            warnings.extend(compiled.warnings)

    for statement in statements:
        if statement.kind == "create_table" and not statement.sql.strip().upper().startswith("CREATE TABLE"):
            statement.kind = "create_index"
    return EmittedProgram(statements=statements, warnings=warnings)
