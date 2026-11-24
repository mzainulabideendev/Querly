"""CQL compiler shared by the Cassandra and ScyllaDB adapters.

CQL looks like SQL but is not SQL. It has no joins, no ``GROUP BY``/``HAVING``,
no ``OFFSET``, no arbitrary ``ORDER BY``, and partition keys must be restricted
for efficient queries. This compiler encodes those rules instead of delegating
to the SQL compiler, which would happily produce invalid CQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oq.ir import AdapterDiagnostic, Entity, QueryDocument, SchemaDocument
from oq.runtime.contract import AdapterErrorCode, DDLArtifact

from oq_adapters.kit.common import diagnostic, format_default, quote_identifier, type_attr, type_kind
from oq_adapters.kit.sql import SqlCompileError, compile_expression

CQL_TYPES: dict[str, str] = {
    "integer": "int",
    "int": "int",
    "bigint": "bigint",
    "float": "float",
    "double": "double",
    "decimal": "decimal",
    "text": "text",
    "boolean": "boolean",
    "date": "date",
    "time": "time",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "duration": "duration",
    "uuid": "uuid",
    "email": "text",
    "url": "text",
    "json": "text",
    "binary": "blob",
    "geo": "text",
    "list": "list<text>",
    "array": "list<text>",
    "set": "set<text>",
    "map": "map<text, text>",
    "enum": "text",
    "reference": "uuid",
}


@dataclass
class CqlCompileResult:
    cql: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[AdapterDiagnostic] = field(default_factory=list)
    result_columns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


@dataclass
class CqlSchemaResult:
    artifacts: list[DDLArtifact] = field(default_factory=list)
    diagnostics: list[AdapterDiagnostic] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


def quote_cql(name: str) -> str:
    return quote_identifier(name, open_quote='"', close_quote='"')


def cql_type(type_obj: Any) -> str | None:
    kind = type_kind(type_obj)
    if kind == "vector":
        dimension = type_attr(type_obj, "dimension")
        return f"vector<float, {int(dimension)}>" if dimension else None
    return CQL_TYPES.get(kind)


def compile_cql_schema(
    schema: SchemaDocument,
    *,
    engine: str,
    keyspace: str | None,
    supports_vector: bool,
    supports_materialized_views: bool,
) -> CqlSchemaResult:
    result = CqlSchemaResult()
    for entity in schema.entities:
        columns: list[str] = []
        for attribute in entity.attributes:
            native = cql_type(attribute.type)
            if native is None:
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        f"{engine} has no mapping for type {type_kind(attribute.type)!r}.",
                        feature=type_kind(attribute.type),
                        path=f"{entity.name}.{attribute.name}",
                    )
                )
                continue
            if native.startswith("vector") and not supports_vector:
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        f"{engine} vector columns are version-dependent and not advertised by this adapter.",
                        feature="vector",
                        path=f"{entity.name}.{attribute.name}",
                    )
                )
                continue
            line = f"{quote_cql(attribute.name)} {native}"
            if attribute.default is not None:
                try:
                    line += f" DEFAULT {format_default(attribute.default)}"
                except ValueError as exc:
                    result.diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.SYNTAX,
                            str(exc),
                            path=f"{entity.name}.{attribute.name}",
                        )
                    )
            columns.append(line)

        primary = list(entity.primary_key)
        if not primary:
            result.diagnostics.append(
                diagnostic(
                    AdapterErrorCode.CAPABILITY,
                    "CQL requires a PRIMARY KEY; the entity declares none.",
                    path=entity.name,
                )
            )
            continue
        # First key column is the partition key; the remainder are clustering
        # columns. A composite partition key must be declared explicitly by the
        # caller through ``partition_key`` in the entity extension because the
        # portable IR has only one flat primary key list.
        extra = getattr(entity, "model_extra", {}) or {}
        partition = list(extra.get("partition_key") or primary[:1])
        clustering = [column for column in primary if column not in partition]
        key_sql = "(" + ", ".join(quote_cql(column) for column in partition) + ")"
        if clustering:
            key_sql += ", " + ", ".join(quote_cql(column) for column in clustering)
        columns.append(f"PRIMARY KEY ({key_sql})")
        table = (
            f"{quote_cql(keyspace)}.{quote_cql(entity.name)}" if keyspace else quote_cql(entity.name)
        )
        options: list[str] = []
        if clustering:
            options.append("CLUSTERING ORDER BY ("
                           + ", ".join(
                               f"{quote_cql(column)} {str(extra.get('clustering_order', {}).get(column, 'ASC')).upper()}"
                               for column in clustering
                           )
                           + ")")
        if extra.get("compaction"):
            options.append(f"COMPACTION = {extra['compaction']}")
        if extra.get("ttl"):
            options.append(f"default_time_to_live = {int(extra['ttl'])}")
        statement = f"CREATE TABLE {table} (\n  " + ",\n  ".join(columns) + "\n)"
        if options:
            statement += "\nWITH " + "\n  AND ".join(options)
        result.artifacts.append(
            DDLArtifact(sql=statement, statement_type="create_table", entity=entity.name)
        )

        for index in entity.indexes:
            if index.kind.lower() in ("materialized_view", "materialized-view"):
                if not supports_materialized_views:
                    result.diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.CAPABILITY,
                            f"{engine} does not support materialized views on this adapter.",
                            feature="materialized_views",
                            path=entity.name,
                        )
                    )
                    continue
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.UNSUPPORTED,
                        "Materialized views need a full CQL SELECT; the portable IR cannot express it.",
                        feature="materialized_views",
                        path=entity.name,
                    )
                )
                continue
            columns_sql = ", ".join(quote_cql(column) for column in index.columns)
            result.artifacts.append(
                DDLArtifact(
                    sql=(
                        f"CREATE INDEX {quote_cql(index.name)} ON {table} ({columns_sql})"
                    ),
                    statement_type="create_index",
                    entity=entity.name,
                    depends_on=[entity.name],
                )
            )
    return result


def compile_cql_select(
    query: QueryDocument,
    schema: SchemaDocument,
    *,
    engine: str,
    keyspace: str | None,
    supports_offset: bool = False,
) -> CqlCompileResult:
    result = CqlCompileResult(cql="")
    if query.operation != "select":
        result.diagnostics.append(
            diagnostic(
                AdapterErrorCode.UNSUPPORTED,
                f"{engine} adapter supports the schema and CQL select subset; IR {query.operation!r} has no values payload.",
                operation=query.operation,
                feature=query.operation,
            )
        )
        return result
    if query.joins:
        result.diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                f"{engine} CQL has no joins.",
                operation="select",
                feature="joins",
            )
        )
    if query.group_by or query.having:
        result.diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                f"{engine} CQL has no GROUP BY/HAVING.",
                operation="select",
                feature="group_by",
            )
        )
    if query.offset and not supports_offset:
        result.diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                f"{engine} CQL has no OFFSET.",
                operation="select",
                feature="offset",
            )
        )
    if result.diagnostics:
        return result

    entities = schema.entity_map
    source = query.source.entity
    entity = entities.get(source) if entities else None
    if entities and entity is None:
        result.diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                f"Table {source!r} is not present in the schema.",
                operation="select",
                path=source,
            )
        )
        return result

    state = _CqlBindState()
    aliases = {query.source.alias or source: quote_cql(query.source.alias or source)}
    projection = "*"
    if query.projection:
        parts = []
        for item in query.projection:
            try:
                parts.append(compile_expression(item.expression, _CqlDialectProxy(), state, aliases=aliases))
            except SqlCompileError as exc:
                result.diagnostics.append(
                    diagnostic(AdapterErrorCode.CAPABILITY, str(exc), operation="select", feature=exc.feature)
                )
        if parts:
            projection = ", ".join(parts)
    table = f"{quote_cql(keyspace)}.{quote_cql(source)}" if keyspace else quote_cql(source)
    cql = f"SELECT {projection} FROM {table}"
    if query.filter is not None:
        try:
            cql += " WHERE " + compile_expression(
                query.filter, _CqlDialectProxy(), state, aliases=aliases
            )
        except SqlCompileError as exc:
            result.diagnostics.append(
                diagnostic(AdapterErrorCode.CAPABILITY, str(exc), operation="select", feature=exc.feature)
            )
            return result
        if entity is not None and entity.primary_key:
            if not _mentions_key(query.filter, set(entity.primary_key)):
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        "Query filters on non-key columns; CQL requires ALLOW FILTERING for that.",
                        operation="select",
                        feature="allow_filtering",
                        severity="warning",
                    )
                )
    if query.order_by:
        for item in query.order_by:
            name = str((item.expression or {}).get("name", "")) if isinstance(item.expression, dict) else ""
            if entity is not None and name and name not in entity.primary_key:
                result.diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        f"CQL can only order by clustering columns; {name!r} is not in the primary key.",
                        operation="select",
                        feature="order_by",
                    )
                )
        if not result.diagnostics:
            cql += " ORDER BY " + ", ".join(
                f"{quote_cql(str((item.expression or {}).get('name', '')))} "
                + ("DESC" if item.direction.lower().startswith("desc") else "ASC")
                for item in query.order_by
            )
    if query.distinct:
        result.diagnostics.append(
            diagnostic(
                AdapterErrorCode.CAPABILITY,
                "CQL SELECT DISTINCT is only valid for partition keys; not compiled generically.",
                operation="select",
                feature="distinct",
            )
        )
    if query.limit:
        if result.diagnostics:
            return result
        cql += f" LIMIT {int(query.limit)}"
    result.cql = cql
    result.parameters = list(state.parameters)
    return result


class _CqlBindState:
    def __init__(self) -> None:
        self.parameters: list[dict[str, Any]] = []

    def bind(self, value: Any, hint: str, *, name: str | None = None) -> str:
        index = len(self.parameters) + 1
        self.parameters.append({"index": index, "name": name or f"p{index}", "type": hint})
        return "%s"


class _CqlDialectProxy:
    """Minimal dialect surface used by the shared expression compiler."""

    quote_open = '"'
    quote_close = '"'
    max_identifier_length = 48
    supports_ilike = False
    supports_null_ordering = False

    def quote(self, name: str) -> str:
        return quote_cql(name)


def _mentions_key(node: Any, keys: set[str]) -> bool:
    if isinstance(node, dict):
        name = node.get("name") or node.get("field")
        if isinstance(name, str) and name in keys:
            return True
        return any(_mentions_key(value, keys) for value in node.values())
    if isinstance(node, list):
        return any(_mentions_key(item, keys) for item in node)
    return False
