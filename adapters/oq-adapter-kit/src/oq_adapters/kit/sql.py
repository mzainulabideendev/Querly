"""Native SQL compiler for the relational Querly IR subset.

The compiler is dialect-driven. Every engine adapter supplies a :class:`SqlDialect`
with its identifier quoting, type map, placeholder style, limit syntax, and
capability gates. The compiler itself contains no engine-specific SQL and no
driver import.

Supported query IR subset (Phase 8):

* ``select`` with projections, inner/left/right/full joins, a filter tree,
  ``group_by``/``having``, ``order_by``, ``distinct``, ``limit``/``offset``.

Insert/update/delete documents carry no assignment payload in IR format 0.1, so
they are rejected with a capability diagnostic rather than guessed. Values are
always bound placeholders; literals supplied in a filter are also turned into
bind parameters so a value can never change the artifact text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from oq.ir import AdapterDiagnostic, Entity, QueryDocument, SchemaDocument
from oq.runtime.contract import AdapterErrorCode, CompileOptions, DDLArtifact
from oq_adapters.kit.common import (
    diagnostic,
    format_default,
    quote_identifier,
    type_attr,
    type_kind,
    validate_identifier,
)

_SAFE_OPERATORS = {
    "=",
    "<>",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "LIKE",
    "NOT LIKE",
    "IS",
    "IS NOT",
}
_SAFE_FUNCTIONS = {
    "abs",
    "avg",
    "coalesce",
    "count",
    "lower",
    "length",
    "max",
    "min",
    "nullif",
    "round",
    "sum",
    "trim",
    "upper",
}
_AGGREGATE_FUNCTIONS = {"avg", "count", "max", "min", "sum"}
_STAR = object()


class SqlCompileError(ValueError):
    """An expression or statement cannot be compiled for this dialect."""

    def __init__(self, message: str, *, feature: str | None = None) -> None:
        super().__init__(message)
        self.feature = feature


@dataclass
class SqlCompileResult:
    """Compiled SQL plus bind plan and diagnostics."""

    sql: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[AdapterDiagnostic] = field(default_factory=list)
    result_columns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


@dataclass
class SchemaCompileResult:
    """Compiled DDL artifacts plus findings."""

    artifacts: list[DDLArtifact] = field(default_factory=list)
    diagnostics: list[AdapterDiagnostic] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


class _BindState:
    def __init__(self, dialect: SqlDialect) -> None:
        self.dialect = dialect
        self.parameters: list[dict[str, Any]] = []

    def bind(self, value: Any, hint: str, *, name: str | None = None) -> str:
        index = len(self.parameters) + 1
        label = name or f"p{index}"
        placeholder = self.dialect.bind_placeholder(index, hint, label)
        self.parameters.append(
            {
                "index": index,
                "name": label,
                "type": hint,
                "value": value,
            }
        )
        return placeholder


class SqlDialect:
    """Base dialect hooks. Subclasses are engine adapters, not copies."""

    adapter_id: str = ""
    engine: str = ""
    quote_open: str = '"'
    quote_close: str = '"'
    max_identifier_length: int | None = None
    placeholder_style: str = "qmark"
    supports_full_join: bool = True
    supports_null_ordering: bool = True
    supports_ilike: bool = False
    supports_returning: bool = False
    supports_auto_increment: bool = True
    supports_transactions: bool = True

    # -- identifiers ---------------------------------------------------------

    def quote(self, name: str) -> str:
        return quote_identifier(
            name,
            open_quote=self.quote_open,
            close_quote=self.quote_close,
            max_length=self.max_identifier_length,
        )

    # -- native types --------------------------------------------------------

    def type_sql(self, type_obj: Any) -> str | None:
        """Map a universal type to a native type, or None when unsupported."""

        kind = type_kind(type_obj)
        precision = type_attr(type_obj, "precision")
        scale = type_attr(type_obj, "scale")
        max_length = type_attr(type_obj, "max_length")
        dimension = type_attr(type_obj, "dimension")

        if kind in ("integer", "int", "bigint", "reference"):
            return "BIGINT" if kind == "bigint" else "INTEGER"
        if kind in ("float", "double"):
            return "DOUBLE PRECISION"
        if kind == "decimal":
            if precision and scale is not None:
                return f"DECIMAL({precision},{scale})"
            return "DECIMAL"
        if kind == "text":
            if max_length and max_length <= 65535:
                return f"VARCHAR({max_length})"
            return "TEXT"
        if kind == "boolean":
            return "BOOLEAN"
        if kind == "date":
            return "DATE"
        if kind == "time":
            return "TIME"
        if kind in ("timestamp", "datetime"):
            has_zone = bool(type_attr(type_obj, "has_timezone", False))
            return "TIMESTAMP WITH TIME ZONE" if has_zone else "TIMESTAMP"
        if kind == "duration":
            return "BIGINT"
        if kind == "uuid":
            return "UUID"
        if kind == "email":
            return f"VARCHAR({max_length or 320})"
        if kind == "url":
            return f"VARCHAR({max_length or 2048})"
        if kind == "json":
            return "JSON"
        if kind == "binary":
            return "BLOB"
        if kind == "geo":
            return None
        if kind in ("list", "array"):
            return None
        if kind == "set":
            return None
        if kind == "map":
            return "JSON"
        if kind == "vector":
            if dimension:
                return f"VECTOR({dimension})"
            return None
        if kind == "enum":
            return "VARCHAR(255)"
        return None

    # -- DDL hooks -----------------------------------------------------------

    def auto_sql(self, attr: Any, type_sql: str) -> str | None:
        """Return the identity/auto-increment clause, or None if unsupported."""

        return "GENERATED BY DEFAULT AS IDENTITY" if self.supports_auto_increment else None

    def table_suffix(self, entity: Entity) -> str:
        return ""

    def column_suffix(self, attr: Any) -> str:
        return ""

    def create_table_prefix(self) -> str:
        return "CREATE TABLE"

    def create_table_sql(
        self,
        entity: Entity,
        columns: list[str],
        primary_columns: list[str],
    ) -> str:
        """Render a CREATE TABLE statement.

        Engine dialects with mandatory table clauses (for example ClickHouse's
        ``ENGINE ... ORDER BY``) override this instead of abusing a suffix.
        """

        if primary_columns:
            columns = [
                *columns,
                "PRIMARY KEY (" + ", ".join(self.quote(column) for column in primary_columns) + ")",
            ]
        return (
            f"{self.create_table_prefix()} {self.quote(entity.name)} (\n  "
            + ",\n  ".join(columns)
            + "\n)"
            + self.table_suffix(entity)
        )

    def index_prefix(self) -> str:
        return "CREATE INDEX"

    def unique_index_sql(self, name: str, entity: str, columns: list[str]) -> str:
        cols = ", ".join(self.quote(column) for column in columns)
        return f"{self.index_prefix()} {self.quote(name)} ON {self.quote(entity)} ({cols})"

    def alter_foreign_key_sql(
        self,
        *,
        constraint_name: str,
        entity: str,
        columns: list[str],
        target: str,
        target_columns: list[str],
        on_delete: str | None,
        on_update: str | None,
    ) -> str:
        cols = ", ".join(self.quote(column) for column in columns)
        target_cols = ", ".join(self.quote(column) for column in target_columns)
        sql = (
            f"ALTER TABLE {self.quote(entity)} ADD CONSTRAINT {self.quote(constraint_name)} "
            f"FOREIGN KEY ({cols}) REFERENCES {self.quote(target)} ({target_cols})"
        )
        if on_delete and on_delete.lower() != "no action":
            sql += f" ON DELETE {on_delete.upper()}"
        if on_update and on_update.lower() != "no action":
            sql += f" ON UPDATE {on_update.upper()}"
        return sql

    # -- bind parameters -----------------------------------------------------

    def bind_placeholder(self, index: int, hint: str, label: str) -> str:
        if self.placeholder_style == "qmark":
            return "?"
        if self.placeholder_style == "format":
            return "%s"
        if self.placeholder_style == "numeric":
            return f"${index}"
        if self.placeholder_style == "named":
            return f":{label}"
        if self.placeholder_style == "brace":
            return f"{{{label}:{self.brace_type(hint)}}}"
        raise SqlCompileError(f"unknown placeholder style {self.placeholder_style!r}")

    def brace_type(self, hint: str) -> str:
        return {
            "integer": "Int64",
            "bigint": "Int64",
            "float": "Float64",
            "double": "Float64",
            "decimal": "Decimal(38, 9)",
            "boolean": "Bool",
            "date": "Date",
            "time": "String",
            "timestamp": "DateTime64(6)",
            "datetime": "DateTime64(6)",
            "binary": "String",
            "uuid": "UUID",
            "json": "String",
        }.get(hint, "String")

    # -- SELECT hooks --------------------------------------------------------

    def select_prefix(self) -> str:
        return "SELECT"

    def select_modifiers(self, distinct: bool) -> str:
        return " DISTINCT" if distinct else ""

    def select_limit(
        self, sql: str, limit: int | None, offset: int | None, has_order_by: bool
    ) -> tuple[str, list[AdapterDiagnostic]]:
        diagnostics: list[AdapterDiagnostic] = []
        if limit is None and offset is None:
            return sql, diagnostics
        suffix = ""
        if limit is not None:
            suffix += f" LIMIT {int(limit)}"
        if offset is not None:
            suffix += f" OFFSET {int(offset)}"
        return sql + suffix, diagnostics

    def nulls_clause(self, nulls: str) -> str:
        return " NULLS FIRST" if nulls == "first" else " NULLS LAST"


# ---------------------------------------------------------------------------
# Expression compiler
# ---------------------------------------------------------------------------


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    raise SqlCompileError(f"expression must be a mapping, got {type(value).__name__}")


def _infer_kind(expression: dict[str, Any]) -> str:
    explicit = expression.get("kind")
    if explicit:
        return str(explicit)
    if "literal_value" in expression:
        return "literal"
    if "function" in expression:
        return "function"
    if expression.get("test") in ("null", "not_null"):
        return "is_null"
    if "items" in expression and expression.get("operator") in ("in", "not_in", "and", "or"):
        return str(expression.get("operator"))
    if "operator" in expression and "left" in expression:
        return "binary"
    if expression.get("whens"):
        return "case"
    if "name" in expression:
        return "column"
    if expression.get("star"):
        return "star"
    raise SqlCompileError(f"cannot infer expression kind from keys {sorted(expression)}")


def _literal_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double"
    if value is None:
        return "null"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "binary"
    return "text"


def compile_expression(
    expression: Any,
    dialect: SqlDialect,
    state: _BindState,
    *,
    aliases: dict[str, str] | None = None,
) -> str:
    """Compile a filter/projection expression to SQL with bound values."""

    node = _as_mapping(expression)
    kind = _infer_kind(node)
    aliases = aliases or {}

    if kind in ("column", "field"):
        name = str(node.get("name") or node.get("field") or "")
        if not name:
            raise SqlCompileError("column expression has no name")
        entity = node.get("entity")
        if entity:
            return f"{dialect.quote(str(entity))}.{dialect.quote(name)}"
        if name in aliases:
            return aliases[name]
        return dialect.quote(name)

    if kind == "star":
        return "*"

    if kind == "literal":
        value = node.get("literal_value")
        hint = (
            type_kind(node.get("literal_type"))
            if node.get("literal_type")
            else _literal_kind(value)
        )
        return state.bind(value, hint)

    if kind == "parameter":
        name = str(node.get("name") or "")
        if not name:
            raise SqlCompileError("parameter expression has no name")
        hint = type_kind(node.get("type")) if node.get("type") else "text"
        return state.bind(None, hint, name=name)

    if kind == "binary":
        operator = str(node.get("operator") or "").upper()
        if operator == "ILIKE":
            if not dialect.supports_ilike:
                raise SqlCompileError("ILIKE is not supported by this dialect", feature="ilike")
            operator = "ILIKE"
        elif operator not in _SAFE_OPERATORS:
            raise SqlCompileError(f"operator {operator!r} is not in the safe set", feature=operator)
        left = compile_expression(node.get("left"), dialect, state, aliases=aliases)
        right = compile_expression(node.get("right"), dialect, state, aliases=aliases)
        return f"({left} {operator} {right})"

    if kind in ("and", "or", "logical"):
        operator = str(node.get("operator") or kind).upper()
        if operator not in ("AND", "OR"):
            raise SqlCompileError(f"logical operator {operator!r} is not supported")
        items = node.get("items") or []
        if not items:
            # Accept binary left/right logical nodes.
            items = [node.get("left"), node.get("right")]
        parts = [
            compile_expression(item, dialect, state, aliases=aliases)
            for item in items
            if item is not None
        ]
        if not parts:
            raise SqlCompileError("logical expression has no operands")
        return "(" + f" {operator} ".join(parts) + ")"

    if kind == "not":
        operand = node.get("operand") or node.get("left") or node.get("expression")
        return f"(NOT {compile_expression(operand, dialect, state, aliases=aliases)})"

    if kind in ("in", "not_in"):
        left = compile_expression(
            node.get("left") or node.get("operand"), dialect, state, aliases=aliases
        )
        items = node.get("items") or node.get("values") or []
        if not items:
            raise SqlCompileError("IN expression has no values")
        rendered = [
            compile_expression(_literal_node(item), dialect, state, aliases=aliases)
            for item in items
        ]
        operator = "NOT IN" if kind == "not_in" else "IN"
        return f"({left} {operator} ({', '.join(rendered)}))"

    if kind == "between":
        operand = compile_expression(
            node.get("operand") or node.get("left"), dialect, state, aliases=aliases
        )
        low = compile_expression(node.get("low"), dialect, state, aliases=aliases)
        high = compile_expression(node.get("high"), dialect, state, aliases=aliases)
        prefix = "NOT BETWEEN" if node.get("negated") else "BETWEEN"
        return f"({operand} {prefix} {low} AND {high})"

    if kind == "is_null":
        operand = compile_expression(
            node.get("operand") or node.get("left") or node.get("expression"),
            dialect,
            state,
            aliases=aliases,
        )
        negated = bool(node.get("negated")) or node.get("test") == "not_null"
        return f"({operand} IS {'NOT ' if negated else ''}NULL)"

    if kind == "function":
        name = str(node.get("function") or node.get("name2") or "").lower()
        if name not in _SAFE_FUNCTIONS:
            raise SqlCompileError(f"function {name!r} is not in the safe set", feature=name)
        arguments = node.get("arguments") or []
        rendered = [
            compile_expression(argument, dialect, state, aliases=aliases) for argument in arguments
        ]
        return f"{name}({', '.join(rendered)})"

    if kind == "cast":
        operand = compile_expression(
            node.get("operand") or node.get("left"), dialect, state, aliases=aliases
        )
        target = dialect.type_sql(node.get("cast_type"))
        if target is None:
            raise SqlCompileError("cast target type is unsupported", feature="cast")
        return f"CAST({operand} AS {target})"

    if kind == "case":
        whens = node.get("whens") or []
        parts = ["CASE"]
        for branch in whens:
            condition = branch.get("when") or branch.get("condition")
            result = branch.get("then") or branch.get("result")
            parts.append(
                "WHEN "
                + compile_expression(condition, dialect, state, aliases=aliases)
                + " THEN "
                + compile_expression(result, dialect, state, aliases=aliases)
            )
        otherwise = node.get("otherwise")
        if otherwise is not None:
            parts.append("ELSE " + compile_expression(otherwise, dialect, state, aliases=aliases))
        parts.append("END")
        return " ".join(parts)

    raise SqlCompileError(f"expression kind {kind!r} is not supported", feature=kind)


def _literal_node(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and ("kind" in value or "literal_value" in value or "name" in value):
        return value
    return {"kind": "literal", "literal_value": value}


# ---------------------------------------------------------------------------
# SELECT compiler
# ---------------------------------------------------------------------------


def compile_select_sql(
    dialect: SqlDialect,
    query: QueryDocument,
    schema: SchemaDocument,
    *,
    options: CompileOptions | None = None,
) -> SqlCompileResult:
    """Compile a relational select into a dialect artifact."""

    diagnostics: list[AdapterDiagnostic] = []
    state = _BindState(dialect)

    if query.operation != "select":
        diagnostics.append(
            diagnostic(
                AdapterErrorCode.UNSUPPORTED,
                (
                    f"IR operation {query.operation!r} has no assignment/values payload in "
                    "format 0.1; this adapter supports the schema and select subset."
                ),
                operation=query.operation,
                feature=query.operation,
                hint="Use a native artifact for mutations until the mutation IR is specified.",
            )
        )
        return SqlCompileResult(sql="", diagnostics=diagnostics)

    entities = schema.entity_map if schema.entities else {}
    source_name = query.source.entity
    if entities and source_name not in entities:
        diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                f"Entity {source_name!r} is not present in the schema.",
                operation="select",
                feature="source",
                path=source_name,
            )
        )
        return SqlCompileResult(sql="", diagnostics=diagnostics)

    aliases: dict[str, str] = {}
    source_alias = query.source.alias or source_name
    aliases[source_alias] = dialect.quote(source_alias)
    for join in query.joins:
        alias = join.alias or join.entity
        aliases[alias] = dialect.quote(alias)

    from_sql = dialect.quote(source_name)
    if query.source.alias:
        from_sql += " " + dialect.quote(query.source.alias)

    join_sql: list[str] = []
    for join in query.joins:
        kind = join.kind.lower()
        if kind == "full" and not dialect.supports_full_join:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.CAPABILITY,
                    "FULL OUTER JOIN is not supported by this engine.",
                    operation="select",
                    feature="full_join",
                )
            )
            continue
        keyword = {
            "inner": "INNER JOIN",
            "left": "LEFT JOIN",
            "right": "RIGHT JOIN",
            "full": "FULL OUTER JOIN",
        }.get(kind)
        if keyword is None:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.CAPABILITY,
                    f"Join kind {join.kind!r} is not supported.",
                    operation="select",
                    feature="join",
                )
            )
            continue
        try:
            condition = compile_expression(join.condition, dialect, state, aliases=aliases)
        except SqlCompileError as exc:
            diagnostics.append(_expression_diagnostic(exc, "join"))
            continue
        target = dialect.quote(join.entity)
        if join.alias:
            target += " " + dialect.quote(join.alias)
        join_sql.append(f"{keyword} {target} ON {condition}")

    # Projection
    projection_sql: list[str] = []
    result_columns: list[dict[str, Any]] = []
    if not query.projection:
        projection_sql.append("*")
        entity = entities.get(source_name)
        if entity is not None:
            for attr in entity.attributes:
                result_columns.append(
                    {
                        "name": attr.name,
                        "type": type_kind(attr.type),
                        "nullable": bool(attr.nullable),
                    }
                )
        else:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.CAPABILITY,
                    "SELECT * cannot report column metadata without a schema document.",
                    operation="select",
                    feature="star_projection",
                    severity="warning",
                )
            )
    else:
        for item in query.projection:
            expression = item.expression
            try:
                rendered = compile_expression(expression, dialect, state, aliases=aliases)
            except SqlCompileError as exc:
                diagnostics.append(_expression_diagnostic(exc, "projection"))
                continue
            alias = item.alias
            if alias:
                rendered += " AS " + dialect.quote(alias)
            projection_sql.append(rendered)
            node = _as_mapping(expression)
            name = alias or node.get("name") or node.get("field") or "expression"
            result_columns.append(
                {
                    "name": str(name),
                    "type": type_kind(
                        node.get("literal_type") or node.get("type") or {"kind": "unknown"}
                    ),
                    "nullable": True,
                }
            )

    if not projection_sql:
        projection_sql.append("*")

    sql_parts = [
        dialect.select_prefix(),
        dialect.select_modifiers(query.distinct),
        ", ".join(projection_sql),
        " FROM ",
        from_sql,
    ]
    if join_sql:
        sql_parts.append(" " + " ".join(join_sql))
    if query.filter is not None:
        try:
            sql_parts.append(
                " WHERE " + compile_expression(query.filter, dialect, state, aliases=aliases)
            )
        except SqlCompileError as exc:
            diagnostics.append(_expression_diagnostic(exc, "filter"))

    if query.group_by:
        try:
            group_sql = ", ".join(
                compile_expression(item, dialect, state, aliases=aliases) for item in query.group_by
            )
            sql_parts.append(" GROUP BY " + group_sql)
        except SqlCompileError as exc:
            diagnostics.append(_expression_diagnostic(exc, "group_by"))

    if query.having is not None:
        try:
            sql_parts.append(
                " HAVING " + compile_expression(query.having, dialect, state, aliases=aliases)
            )
        except SqlCompileError as exc:
            diagnostics.append(_expression_diagnostic(exc, "having"))

    if query.order_by:
        order_parts: list[str] = []
        for item in query.order_by:
            try:
                rendered = compile_expression(item.expression, dialect, state, aliases=aliases)
            except SqlCompileError as exc:
                diagnostics.append(_expression_diagnostic(exc, "order_by"))
                continue
            direction = "DESC" if item.direction.lower().startswith("desc") else "ASC"
            rendered += " " + direction
            if item.nulls and item.nulls != "last":
                if dialect.supports_null_ordering:
                    rendered += dialect.nulls_clause(item.nulls)
                else:
                    diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.CAPABILITY,
                            "NULLS FIRST is not supported by this dialect.",
                            operation="select",
                            feature="nulls_first",
                        )
                    )
            elif item.nulls == "last" and not dialect.supports_null_ordering:
                # The engine default differs; report rather than silently reorder.
                diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        "NULLS LAST cannot be guaranteed by this dialect.",
                        operation="select",
                        feature="null_ordering",
                        severity="warning",
                    )
                )
            order_parts.append(rendered)
        if order_parts:
            sql_parts.append(" ORDER BY " + ", ".join(order_parts))

    sql = "".join(sql_parts)
    sql, limit_diagnostics = dialect.select_limit(
        sql,
        query.limit,
        query.offset,
        bool(query.order_by),
    )
    diagnostics.extend(limit_diagnostics)

    if query.returning is not None and not dialect.supports_returning:
        diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                "RETURNING is not supported by this dialect.",
                operation="select",
                feature="returning",
            )
        )

    return SqlCompileResult(
        sql=sql,
        parameters=[
            {k: v for k, v in parameter.items() if k != "value"} for parameter in state.parameters
        ],
        diagnostics=diagnostics,
        result_columns=result_columns,
    )


def _expression_diagnostic(exc: SqlCompileError, context: str) -> AdapterDiagnostic:
    return diagnostic(
        AdapterErrorCode.CAPABILITY,
        f"{context}: {exc}",
        feature=exc.feature or context,
    )


# ---------------------------------------------------------------------------
# DDL compiler
# ---------------------------------------------------------------------------


def compile_schema_sql(
    dialect: SqlDialect,
    schema: SchemaDocument,
    options: CompileOptions | None = None,
) -> SchemaCompileResult:
    """Compile universal entities into dialect DDL artifacts.

    Tables are emitted first, then indexes, then foreign keys as ``ALTER TABLE``
    statements. This ordering works on engines that require the referenced table
    to exist, without guessing an entity order.
    """

    result = SchemaCompileResult()
    entities = schema.entity_map
    primary_columns: dict[str, list[str]] = {}

    for entity in schema.entities:
        columns: list[str] = []
        pk_columns = list(entity.primary_key)
        for attr in entity.attributes:
            native = dialect.type_sql(attr.type)
            if native is None:
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        f"Type {type_kind(attr.type)!r} has no native mapping.",
                        feature=type_kind(attr.type),
                        path=f"{entity.name}.{attr.name}",
                    )
                )
                native = "TEXT"
            parts = [f"{dialect.quote(attr.name)} {native}"]
            if not attr.nullable and not attr.primary:
                parts.append("NOT NULL")
            if attr.auto:
                auto_clause = dialect.auto_sql(attr, native)
                if auto_clause is None:
                    result.diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.CAPABILITY,
                            "Auto-generated keys are not supported for this type/dialect.",
                            feature="auto_increment",
                            path=f"{entity.name}.{attr.name}",
                        )
                    )
                else:
                    parts.append(auto_clause)
            if attr.unique and not attr.primary:
                parts.append("UNIQUE")
            if attr.default is not None:
                try:
                    parts.append(
                        "DEFAULT " + format_default(attr.default, kind=type_kind(attr.type))
                    )
                except ValueError as exc:
                    result.diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.CAPABILITY,
                            str(exc),
                            feature="default",
                            path=f"{entity.name}.{attr.name}",
                        )
                    )
            if attr.check is not None:
                if isinstance(attr.check, str) and attr.check.strip():
                    parts.append(f"CHECK ({attr.check})")
                else:
                    result.diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.UNSUPPORTED,
                            "Only a literal CHECK expression string can be compiled.",
                            feature="check",
                            path=f"{entity.name}.{attr.name}",
                        )
                    )
            suffix = dialect.column_suffix(attr)
            if suffix:
                parts.append(suffix)
            columns.append(" ".join(parts))
            if attr.primary and attr.name not in pk_columns:
                pk_columns.append(attr.name)

        primary_columns[entity.name] = pk_columns

        for constraint in entity.unique_constraints:
            if constraint.kind != "unique" or not constraint.columns:
                continue
            columns.append(
                "UNIQUE (" + ", ".join(dialect.quote(column) for column in constraint.columns) + ")"
            )

        table_sql = dialect.create_table_sql(entity, columns, pk_columns)
        result.artifacts.append(
            DDLArtifact(sql=table_sql, statement_type="create_table", entity=entity.name)
        )

    # Indexes after tables.
    for entity in schema.entities:
        for index in entity.indexes:
            if not index.columns:
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.SYNTAX,
                        "Index has no columns.",
                        feature="index",
                        path=entity.name,
                    )
                )
                continue
            prefix = "CREATE UNIQUE INDEX" if index.unique else "CREATE INDEX"
            cols = ", ".join(dialect.quote(column) for column in index.columns)
            sql = f"{prefix} {dialect.quote(index.name)} ON {dialect.quote(entity.name)} ({cols})"
            result.artifacts.append(
                DDLArtifact(
                    sql=sql,
                    statement_type="create_index",
                    entity=entity.name,
                    depends_on=[entity.name],
                )
            )

    # Foreign keys last, after every table exists.
    for entity in schema.entities:
        for position, constraint in enumerate(entity.constraints):
            if constraint.kind != "foreign_key" or not constraint.reference:
                continue
            target_name = constraint.reference
            target = entities.get(target_name)
            target_columns = list(constraint.referenced_columns)
            if not target_columns and target is not None:
                target_columns = list(target.primary_key) or [
                    attr.name for attr in target.attributes if attr.primary
                ]
            if target is None or not target_columns:
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.UNSUPPORTED,
                        "Foreign key target or referenced columns cannot be resolved.",
                        feature="foreign_key",
                        path=f"{entity.name}.{constraint.columns}",
                    )
                )
                continue
            name = (
                str(
                    constraint.model_extra.get("name")
                    if getattr(constraint, "model_extra", None)
                    else None
                )
                or f"fk_{entity.name}_{target_name}_{position}"
            )
            result.artifacts.append(
                DDLArtifact(
                    sql=dialect.alter_foreign_key_sql(
                        constraint_name=name,
                        entity=entity.name,
                        columns=list(constraint.columns),
                        target=target_name,
                        target_columns=target_columns,
                        on_delete=_optional_str(
                            constraint.model_extra.get("on_delete")
                            if getattr(constraint, "model_extra", None)
                            else None
                        ),
                        on_update=_optional_str(
                            constraint.model_extra.get("on_update")
                            if getattr(constraint, "model_extra", None)
                            else None
                        ),
                    ),
                    statement_type="alter_table",
                    entity=entity.name,
                    depends_on=[entity.name, target_name],
                )
            )

    return result


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def validate_query_parameters(query: QueryDocument) -> list[AdapterDiagnostic]:
    """Check that declared parameter names are unique and safe."""

    diagnostics: list[AdapterDiagnostic] = []
    seen: set[str] = set()
    for parameter in query.parameters:
        name = parameter.name
        if name in seen:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.SYNTAX,
                    f"Duplicate parameter name {name!r}.",
                    feature="parameter",
                    path=name,
                )
            )
        seen.add(name)
        try:
            validate_identifier(name)
        except ValueError as exc:
            diagnostics.append(
                diagnostic(AdapterErrorCode.SYNTAX, str(exc), feature="parameter", path=name)
            )
    return diagnostics


def iter_expressions(value: Any) -> Iterable[Any]:
    """Yield nested expression nodes (utility for conformance tests)."""

    if isinstance(value, dict):
        if "kind" in value or "literal_value" in value:
            yield value
        for item in value.values():
            yield from iter_expressions(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_expressions(item)
