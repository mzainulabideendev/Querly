"""Milvus adapter for Querly.

Milvus is a distributed vector database with a typed collection schema. A
collection is a Milvus collection, a document is an entity, and the primary key
is ``_id``. Vector search is expressed with ``options["vector"]``; scalar
predicates are compiled into Milvus boolean expressions from a restricted
grammar with validated identifiers and escaped literals, never from raw text.

Milvus has partitions, scalar indexes, and consistency levels but no joins,
GROUP BY, or interactive transactions.

Driver: ``pymilvus`` (optional extra ``driver``), imported lazily.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from typing import Any

from oq.ir import (
    Artifact,
    CollectionSchema,
    DocumentField,
    DocumentQuery,
    DocumentStoreSchema,
    Index,
    PlanDocument,
    Type,
)
from oq.runtime.contract import (
    CAP_AGGREGATION_FRAMEWORK,
    CAP_ARRAY_TYPE,
    CAP_BATCH_WRITES,
    CAP_CHANGE_STREAMS,
    CAP_CHECK_CONSTRAINTS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DOCUMENT_CRUD,
    CAP_DOCUMENT_SCHEMA_VALIDATION,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_GRAPH_TRAVERSAL,
    CAP_GROUP_BY,
    CAP_HAVING,
    CAP_INDEXES,
    CAP_JOINS,
    CAP_JSON,
    CAP_MATERIALIZED_VIEWS,
    CAP_MULTI_KEY_ATOMICITY,
    CAP_NESTED_DOCUMENTS,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_SCHEMA_EVOLUTION,
    CAP_SEARCH_QUERY_DSL,
    CAP_SECONDARY_INDEXES,
    CAP_SERVER_SIDE_SCRIPTING,
    CAP_STRONG_CONSISTENCY,
    CAP_SUBQUERIES,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_TRANSACTIONS_MULTI_DOC,
    CAP_TTL,
    CAP_UNION,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_UPSERT,
    CAP_UPSERT_BY_KEY,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_SEARCH,
    CAP_VECTOR_TYPE,
    CAP_WINDOW_FUNCTIONS,
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    DocumentAdapter,
    DocumentResult,
    EngineVersion,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)
from oq_adapters.kit import build_report, diagnostic, json_safe, type_kind
from oq_adapters.kit.document import (
    check_collection,
    check_document_operation,
    check_unsafe_filter_operators,
    document_safety,
    normalize_document,
    unsupported_document_plan,
)

try:
    from pymilvus import MilvusClient
except ImportError:  # pragma: no cover
    MilvusClient = None

__all__ = ["MilvusAdapter", "MilvusConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "search",
    "near_vector",
    "query",
    "scan",
    "count",
    "get",
    "insert_one",
    "insert_many",
    "upsert",
    "update_one",
    "delete_one",
    "delete_many",
}
_WRITES = {"insert_one", "insert_many", "upsert", "update_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}
_SEARCH_OPS = {"search", "near_vector", "query"}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MilvusConnection(Connection):
    def __init__(self, config: ConnectionConfig, client: Any) -> None:
        self._config = config
        self._client = client
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def client(self) -> Any:
        return self._client

    def ping(self) -> bool:
        try:
            self._client.list_collections()
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True


class MilvusAdapter(DocumentAdapter):
    data_model = "vector"
    ir_subset = ("schema", "vector search", "scalar query", "entity writes", "count")

    @property
    def id(self) -> str:
        return "milvus"

    @property
    def display_name(self) -> str:
        return "Milvus"

    @property
    def package_version(self) -> str:
        from oq_adapters.milvus import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.milvus import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Milvus",
            tested_versions=[],
            capabilities=MILVUS_CAPS,
            types=MILVUS_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "Vector search requires options['vector']; without it, queries use scalar filtering.",
                "Scalar predicates compile to a Milvus expression with validated identifiers and escaped literals.",
                "No joins, GROUP BY, HAVING, or aggregation pipeline.",
                "No interactive transactions; inserts/deletes are atomic per request.",
                "Dynamic fields must be enabled on the collection for untyped payload keys.",
                "Partition and index choices are deployment concerns, not inferred automatically.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "milvus":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a milvus config.")
        if MilvusClient is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "pymilvus is not installed. Install oq-adapter-milvus[driver].",
            )
        uri = config.options.get("uri") or (
            f"{'https' if config.sslmode not in (None, 'disable') else 'http'}"
            f"://{config.host or 'localhost'}:{config.port or 19530}"
        )
        token = secrets.get("token") or (
            f"{config.user}:{secrets.get('password') or ''}" if config.user else None
        )
        database = config.database or config.options.get("db_name", "default")
        try:
            client = MilvusClient(
                uri=uri,
                token=token,
                db_name=database,
                timeout=float(config.options.get("timeout", "30")),
                secure=config.sslmode not in (None, "disable"),
                server_pem_path=config.sslrootcert,
            )
            client.list_collections()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return MilvusConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            raw = str(getattr(conn.client, "server_version", None) or "unknown")
            if raw == "unknown":
                try:
                    raw = str(conn.client.get_server_version())
                except Exception:
                    raw = "unknown"
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="Milvus",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, MilvusConnection):
            with contextlib.suppress(Exception):
                connection.close()

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            collections = []
            for name in conn.client.list_collections():
                description = conn.client.describe_collection(str(name))
                fields: list[DocumentField] = []
                for field in description.get("fields", []):
                    params = field.get("params") or {}
                    kind = _universal(str(field.get("type", "")))
                    fields.append(
                        DocumentField(
                            path=str(field.get("name")),
                            type=Type(kind=kind),
                            nullable=bool(field.get("nullable", False)),
                            frequency=1.0,
                            options={
                                "primary": bool(field.get("is_primary", False)),
                                "partition_key": bool(field.get("is_partition_key", False)),
                                "dimension": params.get("dim"),
                            }
                            if kind == "vector" or field.get("is_primary")
                            else None,
                        )
                    )
                indexes = []
                for item in conn.client.list_indexes(str(name)) or []:
                    indexes.append(
                        Index(
                            name=str(item.get("index_name", "")),
                            columns=[str(item.get("field_name", ""))],
                            unique=False,
                            kind=str(item.get("index_type", "milvus-index")),
                            options={"metric_type": item.get("metric_type")},
                        )
                    )
                try:
                    stats = conn.client.get_collection_stats(str(name))
                    count = int(stats.get("row_count", 0))
                except Exception:
                    count = 0
                collections.append(
                    CollectionSchema(
                        name=str(name),
                        fields=fields,
                        document_count=count,
                        indexes=indexes,
                    )
                )
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        return DocumentStoreSchema(collections=collections)

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        artifacts = []
        for collection in schema.collections:
            fields = []
            for field in collection.fields:
                entry: dict[str, Any] = {
                    "name": field.path,
                    "type": _milvus_type(field.type.kind),
                }
                if field.type.kind == "vector":
                    entry["dim"] = int((field.options or {}).get("dimension", 1536))
                if (field.options or {}).get("primary"):
                    entry["is_primary"] = True
                if (field.options or {}).get("partition_key"):
                    entry["is_partition_key"] = True
                fields.append(entry)
            if not any(item["type"] == "FLOAT_VECTOR" for item in fields):
                raise AdapterError(
                    AdapterErrorCode.CAPABILITY,
                    f"Milvus collection {collection.name!r} needs a vector field.",
                )
            artifacts.append(
                DDLArtifact(
                    sql=json_safe(
                        {
                            "create_collection": collection.name,
                            "schema": {
                                "auto_id": False,
                                "enable_dynamic_field": True,
                                "fields": fields,
                            },
                        }
                    ),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                if not index.columns:
                    continue
                is_vector = any(
                    field.path == index.columns[0] and field.type.kind == "vector"
                    for field in collection.fields
                )
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            {
                                "create_index": index.name,
                                "collection": collection.name,
                                "field": index.columns[0],
                                "index_type": "AUTOINDEX",
                                "metric_type": index.options.get("metric_type", "COSINE"),
                            }
                        ),
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
                if not is_vector:
                    continue
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Milvus")
        diagnostics.extend(
            check_collection(query, {c.name for c in schema.collections}, engine="Milvus")
        )
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "javascript"},
                engine="Milvus",
            )
        )
        vector = (query.options or {}).get("vector")
        if query.operation in _SEARCH_OPS and not vector:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.SYNTAX,
                    "Milvus search requires options['vector'] with a numeric list.",
                    operation=query.operation,
                    feature="vector",
                )
            )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Milvus has no aggregation pipeline.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if query.skip or query.sort:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Milvus does not support portable skip/sort; use a scalar range or vector search.",
                    operation=query.operation,
                    feature="query_shape",
                )
            )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "milvus", query, diagnostics)

        expression = None
        if query.filter:
            try:
                expression = _expression(query.filter)
            except AdapterError as exc:
                diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.SYNTAX,
                        exc.message,
                        operation=query.operation,
                        feature="filter",
                    )
                )
                return unsupported_document_plan(self.id, "milvus", query, diagnostics)

        plan: dict[str, Any] = {
            "collection": query.collection,
            "operation": query.operation,
            "filter": expression,
            "limit": int(query.limit or (1 if query.operation == "find_one" else 10)),
            "output_fields": list(query.projection) if query.projection else ["*"],
        }
        if vector:
            plan["vector"] = [float(value) for value in vector]
            plan["metric_type"] = (query.options or {}).get("metric_type", "COSINE")
            plan["vector_field"] = (query.options or {}).get("vector_field", "vector")
        if query.operation in ("insert_one", "insert_many", "upsert"):
            plan["rows"] = [
                {key: value for key, value in document.items()} for document in query.documents
            ]
        elif query.operation == "update_one":
            plan["rows"] = [dict(query.updates or {})]
        elif query.operation in ("delete_one", "delete_many", "get"):
            plan["ids"] = [
                node.get("eq", node.get("value"))
                for node in _equality_nodes(query.filter)
                if str(node.get("field") or node.get("name")) in ("_id", "id")
            ]

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="milvus-request", text=json_safe(plan), dialect="milvus"),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            required_capabilities=[CAP_VECTOR_SEARCH] if vector else [],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "entity", "type": "map", "nullable": True}],
            engine_plan={"kind": "milvus", **json_safe(plan)},
            diagnostics=diagnostics,
            supported=True,
        )

    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> DocumentResult:
        conn = self._require(connection)
        if not plan.supported or not plan.engine_plan:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Plan is unsupported and was not executed.",
                diagnostics=[item.model_dump(mode="json") for item in plan.diagnostics],
            )
        native = plan.engine_plan
        collection = native["collection"]
        operation = native["operation"]
        client = conn.client
        expression = native.get("filter")
        try:
            if operation == "count":
                count_expr = expression if expression else "*"
                result = client.query(
                    collection_name=collection,
                    filter=count_expr if count_expr != "*" else "",
                    output_fields=["count(*)"],
                )
                total = 0
                if result and isinstance(result, list):
                    total = int(next((item.get("count(*)") for item in result if item), 0) or 0)
                return DocumentResult(documents=[], affected_count=total, warnings=[])
            if operation in _SEARCH_OPS:
                results = client.search(
                    collection_name=collection,
                    data=[native["vector"]],
                    filter=expression or "",
                    limit=int(native["limit"]),
                    output_fields=native.get("output_fields") or ["*"],
                    anns_field=native.get("vector_field", "vector"),
                    search_params={"metric_type": native.get("metric_type", "COSINE")},
                )
                documents = []
                for hit in results[0] if results else []:
                    entity = dict(hit.get("entity", {}) or {})
                    entity["_id"] = hit.get("id")
                    entity["_distance"] = hit.get("distance")
                    documents.append(normalize_document(entity))
                return DocumentResult(documents=documents, warnings=[])
            if operation in ("find", "find_one", "scan"):
                if native.get("vector") and operation == "find":
                    return DocumentResult(
                        documents=[],
                        warnings=["Milvus find with a vector must use operation 'search'."],
                    )
                result = client.query(
                    collection_name=collection,
                    filter=expression or "",
                    output_fields=native.get("output_fields") or ["*"],
                    limit=int(native["limit"]),
                )
                return DocumentResult(
                    documents=[normalize_document(_entity(item)) for item in result or []],
                    warnings=[],
                )
            if operation == "get":
                ids = [item for item in native.get("ids", []) if item is not None]
                if not ids:
                    raise AdapterError(
                        AdapterErrorCode.SYNTAX, "Milvus get requires an id equality filter."
                    )
                result = client.get(
                    collection_name=collection,
                    ids=ids,
                    output_fields=native.get("output_fields") or ["*"],
                )
                return DocumentResult(
                    documents=[normalize_document(_entity(item)) for item in result or []],
                    affected_count=len(result or []),
                    warnings=[],
                )
            if operation in ("insert_one", "insert_many"):
                result = client.insert(collection_name=collection, data=native.get("rows", []))
                return DocumentResult(
                    documents=[],
                    affected_count=int(
                        (result or {}).get("insert_count", len(native.get("rows", [])))
                    ),
                    warnings=[],
                )
            if operation == "upsert":
                result = client.upsert(collection_name=collection, data=native.get("rows", []))
                return DocumentResult(
                    documents=[],
                    affected_count=int(
                        (result or {}).get("upsert_count", len(native.get("rows", [])))
                    ),
                    warnings=[],
                )
            if operation == "update_one":
                rows = native.get("rows", [])
                if not rows:
                    raise AdapterError(
                        AdapterErrorCode.SYNTAX, "Milvus update requires entity values."
                    )
                result = client.upsert(collection_name=collection, data=rows)
                return DocumentResult(
                    documents=[],
                    affected_count=int((result or {}).get("upsert_count", len(rows))),
                    warnings=[],
                )
            if operation in ("delete_one", "delete_many"):
                ids = [item for item in native.get("ids", []) if item is not None]
                if ids:
                    result = client.delete(collection_name=collection, ids=ids)
                elif expression:
                    result = client.delete(collection_name=collection, filter=expression)
                else:
                    raise AdapterError(
                        AdapterErrorCode.UNSUPPORTED,
                        "Milvus delete requires ids or a filter; unfiltered deletes are refused.",
                    )
                return DocumentResult(
                    documents=[],
                    affected_count=int((result or {}).get("delete_count", len(ids)))
                    if isinstance(result, dict)
                    else len(ids),
                    warnings=[],
                )
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"Unsupported Milvus operation {operation!r}."
        )

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Milvus has no interactive multi-entity transactions; each request is atomic.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "Milvus has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> MilvusConnection:
        if not isinstance(connection, MilvusConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Milvus.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        lower = text.lower()
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "auth" in lower or "permission" in lower or "token" in lower:
            code = AdapterErrorCode.AUTH
        elif "not found" in lower or "not exist" in lower or "collection not" in lower:
            code = AdapterErrorCode.CAPABILITY
        elif "invalid" in lower or "syntax" in lower or "dimension" in lower:
            code = AdapterErrorCode.SYNTAX
        elif "timeout" in lower or "unavailable" in lower or "rate limit" in lower:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        return AdapterError(
            code,
            f"Milvus {operation} failed: {text}",
            retryable=retryable,
            cause=exc,
            redacted_cause=text,
        )


def _entity(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        entity = dict(value.get("entity", value))
        if "id" in value and "id" not in entity:
            entity["id"] = value["id"]
        return {str(key): item for key, item in entity.items()}
    return {"value": value}


def _equality_nodes(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return found
    if isinstance(node.get("and"), list):
        for item in node["and"]:
            found.extend(_equality_nodes(item))
    elif ("eq" in node and (node.get("field") or node.get("name"))) or (
        str(node.get("operator", "")).upper() in ("=", "EQ")
        and (node.get("field") or node.get("name"))
    ):
        found.append(node)
    return found


def _expression(node: Any) -> str:
    """Compile an IR filter into a Milvus boolean expression.

    Only identifiers matching ``[A-Za-z_][A-Za-z0-9_]*`` are accepted as field
    names; literals are escaped and typed. No raw user text reaches the
    expression.
    """

    if not isinstance(node, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "Milvus filter must be an object.")
    if isinstance(node.get("and"), list):
        if not node["and"]:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Milvus 'and' filter is empty.")
        return "(" + " and ".join(_expression(item) for item in node["and"]) + ")"
    if isinstance(node.get("or"), list):
        if not node["or"]:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Milvus 'or' filter is empty.")
        return "(" + " or ".join(_expression(item) for item in node["or"]) + ")"
    if "not" in node:
        return f"(not {_expression(node['not'])})"
    field = node.get("field") or node.get("name")
    if not isinstance(field, str) or not _IDENTIFIER.match(field):
        raise AdapterError(
            AdapterErrorCode.SYNTAX, "Milvus filter field must be a simple identifier."
        )
    if "eq" in node:
        return f"{field} == {_literal(node['eq'])}"
    if "in" in node:
        if not isinstance(node["in"], list) or not node["in"]:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Milvus 'in' filter needs values.")
        return f"{field} in [{', '.join(_literal(item) for item in node['in'])}]"
    if "range" in node:
        parts = []
        for key, operator in (("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<=")):
            if key in node["range"]:
                parts.append(f"{field} {operator} {_literal(node['range'][key])}")
        if not parts:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Milvus range filter has no bound.")
        return " and ".join(parts)
    if "exists" in node:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED,
            "Milvus has no portable exists predicate; use a not-empty scalar range.",
        )
    operator = str(node.get("operator") or "").upper()
    value = node.get("value", node.get("operand"))
    mapping = {
        "=": "==",
        "EQ": "==",
        "!=": "!=",
        "<>": "!=",
        "<": "<",
        "<=": "<=",
        ">": ">",
        ">=": ">=",
    }
    if operator not in mapping:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"Milvus filter operator {operator!r} is unsupported."
        )
    return f"{field} {mapping[operator]} {_literal(value)}"


def _literal(value: Any) -> str:
    if value is None:
        raise AdapterError(
            AdapterErrorCode.SYNTAX, "Milvus expressions cannot compare against null."
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _universal(native: str) -> str:
    return {
        "BOOL": "boolean",
        "INT8": "integer",
        "INT16": "integer",
        "INT32": "integer",
        "INT64": "bigint",
        "FLOAT": "float",
        "DOUBLE": "double",
        "VARCHAR": "text",
        "JSON": "json",
        "ARRAY": "list",
        "FLOAT_VECTOR": "vector",
        "BINARY_VECTOR": "vector",
        "SPARSE_FLOAT_VECTOR": "vector",
    }.get(native.upper(), "text")


def _milvus_type(kind: str) -> str:
    return {
        "integer": "INT64",
        "bigint": "INT64",
        "float": "FLOAT",
        "double": "DOUBLE",
        "decimal": "DOUBLE",
        "text": "VARCHAR",
        "boolean": "BOOL",
        "date": "VARCHAR",
        "time": "VARCHAR",
        "timestamp": "VARCHAR",
        "datetime": "VARCHAR",
        "duration": "INT64",
        "uuid": "VARCHAR",
        "email": "VARCHAR",
        "url": "VARCHAR",
        "json": "JSON",
        "binary": "VARCHAR",
        "geo": "JSON",
        "list": "ARRAY",
        "set": "ARRAY",
        "map": "JSON",
        "vector": "FLOAT_VECTOR",
    }.get(kind, "VARCHAR")


MILVUS_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "INT64"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "INT64"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "FLOAT"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "DOUBLE"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "DOUBLE", notes="No decimal scalar type."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "VARCHAR"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "BOOL"),
    "date": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "VARCHAR", notes="ISO-8601 string by convention."
    ),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR"),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR"),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "INT64"),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "VARCHAR"),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "JSON"),
    "binary": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "VARCHAR",
        notes="Base64 string; BINARY_VECTOR is vector-only.",
    ),
    "geo": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON"),
    "list": TypeSupport(TypeSupportLevel.COMPATIBLE, "ARRAY"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "ARRAY"),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "JSON"),
    "vector": TypeSupport(
        TypeSupportLevel.EXACT, "FLOAT_VECTOR", notes="Dense and sparse vectors are supported."
    ),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No foreign keys."),
}

MILVUS_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No interactive transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(True, "Dense, sparse, and binary vectors."),
    CAP_JSON: Capability(True, "JSON scalar field."),
    CAP_ARRAY_TYPE: Capability(True),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(False),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(True, "Primary key uniqueness."),
    CAP_INDEXES: Capability(True, "Vector, scalar, and full-text indexes."),
    CAP_FULL_TEXT_SEARCH: Capability(True, "BM25 sparse search with a full-text index."),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Partitions and partition keys."),
    CAP_REPLICATION: Capability(True, "Distributed cluster replication."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(False),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True),
    CAP_TTL: Capability(True, "Collection-level TTL on a timestamp field."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(True, "ANN search with configurable metric."),
    CAP_SEARCH_QUERY_DSL: Capability(False, "Boolean expression language only."),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False),
    CAP_BATCH_WRITES: Capability(True),
    CAP_STRONG_CONSISTENCY: Capability(True, "Consistency level is configurable per deployment."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Dynamic fields cover additional keys."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(True, "Typed collection schema."),
    CAP_NESTED_DOCUMENTS: Capability(True, "JSON fields."),
    CAP_CHANGE_STREAMS: Capability(True, "CDC via the message queue integration; not built in."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
