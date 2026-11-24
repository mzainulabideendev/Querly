"""Weaviate adapter for Querly.

Weaviate is a vector-native database with a GraphQL/REST API, BM25 full-text
search, and hybrid search. A collection is a Weaviate collection (class), a
document is an object, and ``_id`` is the object UUID. Filters are compiled into
Weaviate's structured ``Filter`` objects; no GraphQL is generated from user
text, which avoids GraphQL injection entirely.

Driver: ``weaviate-client`` v4 (optional extra ``driver``), imported lazily.
"""

from __future__ import annotations

from typing import Any, Mapping

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
    CAP_SEARCH_QUERY_DSL,
    CAP_SECONDARY_INDEXES,
    CAP_SERVER_SIDE_SCRIPTING,
    CAP_STRONG_CONSISTENCY,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_TRANSACTIONS_MULTI_DOC,
    CAP_TTL,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_UPSERT,
    CAP_UPSERT_BY_KEY,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_SEARCH,
    CAP_VECTOR_TYPE,
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
    import weaviate
    from weaviate.classes.query import Filter
except ImportError:  # pragma: no cover
    weaviate = None
    Filter = None

__all__ = ["WeaviateAdapter", "WeaviateConnection"]

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
    "replace_one",
    "delete_one",
    "delete_many",
}
_WRITES = {"insert_one", "insert_many", "upsert", "update_one", "replace_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}

_PROPERTY_TYPES = {
    "text": ["text"],
    "integer": ["int"],
    "bigint": ["int"],
    "float": ["number"],
    "double": ["number"],
    "decimal": ["number"],
    "boolean": ["boolean"],
    "date": ["date"],
    "time": ["date"],
    "timestamp": ["date"],
    "datetime": ["date"],
    "uuid": ["uuid"],
    "email": ["text"],
    "url": ["text"],
    "json": ["object"],
    "geo": ["geoCoordinates"],
    "list": ["text[]"],
    "set": ["text[]"],
    "map": ["object"],
}


class WeaviateConnection(Connection):
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
            return bool(self._client.is_live())
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True


class WeaviateAdapter(DocumentAdapter):
    data_model = "vector"
    ir_subset = ("schema", "vector search", "BM25 search", "object CRUD", "count")

    @property
    def id(self) -> str:
        return "weaviate"

    @property
    def display_name(self) -> str:
        return "Weaviate"

    @property
    def package_version(self) -> str:
        from oq_adapters.weaviate import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.weaviate import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Weaviate",
            tested_versions=[],
            capabilities=WEAVIATE_CAPS,
            types=WEAVIATE_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "Vector search requires options['vector']; BM25 uses options['query_text'].",
                "Filters use the client Filter API, not generated GraphQL.",
                "No joins, GROUP BY, HAVING, or aggregation pipeline.",
                "No multi-object transactions; batch inserts are not atomic.",
                "Cross-references are a native feature but are not expressible in IR 0.1.",
                "Multi-tenancy requires a tenant per collection and is not inferred.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "weaviate":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a weaviate config.")
        if weaviate is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "weaviate-client is not installed. Install oq-adapter-weaviate[driver].",
            )
        host = config.host or "localhost"
        http_port = config.port or 8080
        grpc_port = int(config.options.get("grpc_port", "50051"))
        secure = config.sslmode not in (None, "disable") or bool(config.options.get("secure"))
        headers = dict(config.options.get("headers") or {})
        auth = None
        api_key = secrets.get("api_key") or config.options.get("api_key")
        if api_key:
            from weaviate.classes.init import Auth

            auth = Auth.api_key(api_key)
        elif config.user:
            from weaviate.classes.init import Auth

            auth = Auth.client_password(
                username=config.user, password=secrets.get("password") or ""
            )
        try:
            client = weaviate.connect_to_custom(
                http_host=host,
                http_port=http_port,
                http_secure=secure,
                grpc_host=host,
                grpc_port=grpc_port,
                grpc_secure=secure,
                auth_credentials=auth,
                headers=headers or None,
                skip_init_checks=False,
            )
            if not client.is_live():
                raise AdapterError(AdapterErrorCode.CONNECTION, "Weaviate is not live.")
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return WeaviateConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            meta = conn.client.get_meta()
            raw = str(meta.get("version", "unknown"))
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="Weaviate",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, WeaviateConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            collections = []
            for name in conn.client.collections.list_all().keys():
                handle = conn.client.collections.get(str(name))
                config = handle.config
                fields = [
                    DocumentField(
                        path=str(prop.name),
                        type=Type(kind=_universal(prop.data_type)),
                        nullable=True,
                        frequency=1.0,
                    )
                    for prop in getattr(config, "properties", []) or []
                ]
                vector_config = getattr(config, "vector_config", None)
                if vector_config is not None:
                    fields.append(
                        DocumentField(
                            path="vector",
                            type=Type(kind="vector"),
                            nullable=True,
                            frequency=1.0,
                        )
                    )
                indexes = [
                    Index(
                        name=f"{prop.name}-index",
                        columns=[str(prop.name)],
                        unique=False,
                        kind="weaviate-property-index",
                        options={"index_filterable": getattr(prop, "index_filterable", None)},
                    )
                    for prop in getattr(config, "properties", []) or []
                    if getattr(prop, "index_filterable", False)
                ]
                try:
                    count = int(handle.aggregate.over_all(total_count=True).total_count or 0)
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
            properties = [
                {
                    "name": field.path,
                    "dataType": _PROPERTY_TYPES.get(field.type.kind, ["text"]),
                    "indexFilterable": field.type.kind != "vector",
                }
                for field in collection.fields
                if field.type.kind != "vector"
            ]
            artifacts.append(
                DDLArtifact(
                    sql=json_safe(
                        {
                            "create_collection": collection.name,
                            "properties": properties,
                            "vectorizer": "none",
                        }
                    ),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Weaviate")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="Weaviate"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "graphql"},
                engine="Weaviate",
            )
        )
        options = query.options or {}
        vector = options.get("vector")
        if query.operation in ("search", "near_vector") and not vector:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.SYNTAX,
                    "Weaviate vector search requires options['vector'].",
                    operation=query.operation,
                    feature="vector",
                )
            )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Weaviate has no aggregation pipeline.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if query.skip or query.sort:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Weaviate fetch/search does not support portable skip/sort; use vector or BM25 ranking.",
                    operation=query.operation,
                    feature="query_shape",
                )
            )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "weaviate", query, diagnostics)

        plan: dict[str, Any] = {
            "collection": query.collection,
            "operation": query.operation,
            "filter": _filter_spec(query.filter) if query.filter else None,
            "limit": int(query.limit or (1 if query.operation == "find_one" else 10)),
        }
        if vector:
            plan["vector"] = [float(value) for value in vector]
            plan["target_vector"] = options.get("target_vector")
            plan["distance"] = options.get("distance")
        if options.get("query_text"):
            plan["query_text"] = str(options["query_text"])
            plan["alpha"] = float(options.get("alpha", 0.5))
        if query.projection:
            plan["return_properties"] = [str(key) for key in query.projection if key != "_id"]
        if query.operation in ("insert_one", "insert_many", "upsert", "replace_one"):
            plan["objects"] = [
                {
                    "id": document.get("_id") or document.get("id"),
                    "properties": {
                        key: value
                        for key, value in document.items()
                        if key not in ("_id", "id", "vector")
                    },
                    "vector": document.get("vector") or vector,
                }
                for document in query.documents
            ]
        elif query.operation == "update_one":
            plan["properties"] = dict(query.updates or {})
        elif query.operation in ("delete_one", "delete_many", "get"):
            plan["ids"] = [
                node.get("eq", node.get("value"))
                for node in _equality_nodes(query.filter)
                if str(node.get("field") or node.get("name")) in ("_id", "id")
            ]

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="weaviate-request", text=json_safe(plan), dialect="weaviate"),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            required_capabilities=[CAP_VECTOR_SEARCH] if vector else [],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "object", "type": "map", "nullable": True}],
            engine_plan={"kind": "weaviate", **json_safe(plan)},
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
        operation = native["operation"]
        handle = conn.client.collections.get(str(native["collection"]))
        filter_object = _to_filter(native.get("filter")) if native.get("filter") else None
        try:
            if operation in ("search", "near_vector", "query"):
                if native.get("query_text") and native.get("vector"):
                    response = handle.query.hybrid(
                        query=native["query_text"],
                        vector=native["vector"],
                        alpha=float(native.get("alpha", 0.5)),
                        limit=int(native["limit"]),
                        filters=filter_object,
                        return_properties=native.get("return_properties"),
                    )
                elif native.get("vector"):
                    response = handle.query.near_vector(
                        near_vector=native["vector"],
                        limit=int(native["limit"]),
                        filters=filter_object,
                        return_properties=native.get("return_properties"),
                        target_vector=native.get("target_vector"),
                        distance=native.get("distance"),
                    )
                else:
                    response = handle.query.bm25(
                        query=str(native.get("query_text", "")),
                        limit=int(native["limit"]),
                        filters=filter_object,
                        return_properties=native.get("return_properties"),
                    )
                return DocumentResult(
                    documents=[_object(item) for item in response.objects],
                    warnings=[],
                )
            if operation in ("find", "find_one", "scan"):
                limit = int(native["limit"])
                response = handle.query.fetch_objects(
                    limit=limit,
                    filters=filter_object,
                    return_properties=native.get("return_properties"),
                )
                return DocumentResult(
                    documents=[_object(item) for item in response.objects],
                    warnings=[],
                )
            if operation == "get":
                ids = [item for item in native.get("ids", []) if item is not None]
                if not ids:
                    raise AdapterError(AdapterErrorCode.SYNTAX, "Weaviate get requires an id equality filter.")
                documents = []
                for object_id in ids:
                    item = handle.query.fetch_object_by_id(str(object_id))
                    if item is not None:
                        documents.append(_object(item))
                return DocumentResult(documents=documents, affected_count=len(documents), warnings=[])
            if operation == "count":
                result = handle.aggregate.over_all(total_count=True, filters=filter_object)
                return DocumentResult(
                    documents=[],
                    affected_count=int(result.total_count or 0),
                    warnings=[],
                )
            if operation in ("insert_one", "insert_many"):
                responses = []
                for item in native.get("objects", []):
                    responses.append(
                        handle.data.insert(
                            properties=item.get("properties") or {},
                            uuid=item.get("id"),
                            vector=item.get("vector"),
                        )
                    )
                return DocumentResult(
                    documents=[{"_id": str(item)} for item in responses],
                    affected_count=len(responses),
                    warnings=[] if operation == "insert_one" else ["Weaviate batch writes are not atomic."],
                )
            if operation in ("upsert", "replace_one"):
                responses = []
                for item in native.get("objects", []):
                    if item.get("id"):
                        handle.data.replace(
                            uuid=str(item["id"]),
                            properties=item.get("properties") or {},
                            vector=item.get("vector"),
                        )
                        responses.append(str(item["id"]))
                    else:
                        responses.append(
                            str(
                                handle.data.insert(
                                    properties=item.get("properties") or {},
                                    vector=item.get("vector"),
                                )
                            )
                        )
                return DocumentResult(documents=[{"_id": item} for item in responses], warnings=[])
            if operation == "update_one":
                properties = native.get("properties", {})
                if not filter_object:
                    raise AdapterError(
                        AdapterErrorCode.UNSUPPORTED,
                        "Weaviate object updates require an id filter.",
                    )
                ids = [item for item in native.get("ids", []) if item is not None]
                if len(ids) != 1:
                    raise AdapterError(
                        AdapterErrorCode.SYNTAX,
                        "Weaviate update_one requires exactly one id equality filter.",
                    )
                handle.data.update(uuid=str(ids[0]), properties=properties)
                return DocumentResult(documents=[], affected_count=1, warnings=[])
            if operation in ("delete_one", "delete_many"):
                ids = [item for item in native.get("ids", []) if item is not None]
                if ids:
                    for object_id in ids:
                        handle.data.delete_by_id(str(object_id))
                    return DocumentResult(documents=[], affected_count=len(ids), warnings=[])
                if filter_object is not None:
                    result = handle.data.delete_many(where=filter_object)
                    return DocumentResult(
                        documents=[],
                        affected_count=int(getattr(result, "successful", 0) or 0),
                        warnings=[],
                    )
                raise AdapterError(
                    AdapterErrorCode.UNSUPPORTED,
                    "Weaviate delete requires ids or a filter; unfiltered deletes are refused.",
                )
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported Weaviate operation {operation!r}.")

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Weaviate has no multi-object transactions; batch writes are not atomic.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "Weaviate has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> WeaviateConnection:
        if not isinstance(connection, WeaviateConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Weaviate.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        lower = text.lower()
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "401" in text or "403" in text or "unauthorized" in lower or "forbidden" in lower:
            code = AdapterErrorCode.AUTH
        elif "404" in text or "not found" in lower or "does not exist" in lower:
            code = AdapterErrorCode.CAPABILITY
        elif "422" in text or "invalid" in lower or "validation" in lower:
            code = AdapterErrorCode.SYNTAX
        elif "429" in text or "timeout" in lower or "unavailable" in lower:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        return AdapterError(code, f"Weaviate {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _object(value: Any) -> Any:
    properties = dict(getattr(value, "properties", {}) or {})
    properties["_id"] = str(getattr(value, "uuid", ""))
    metadata = getattr(value, "metadata", None)
    if metadata is not None:
        distance = getattr(metadata, "distance", None)
        if distance is not None:
            properties["_distance"] = distance
        score = getattr(metadata, "score", None)
        if score is not None:
            properties["_score"] = score
    return normalize_document(properties)


def _equality_nodes(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return found
    if isinstance(node.get("and"), list):
        for item in node["and"]:
            found.extend(_equality_nodes(item))
    elif "eq" in node and (node.get("field") or node.get("name")):
        found.append(node)
    elif str(node.get("operator", "")).upper() in ("=", "EQ") and (node.get("field") or node.get("name")):
        found.append(node)
    return found


def _filter_spec(node: Any) -> dict[str, Any]:
    must: list[dict[str, Any]] = []
    should: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []

    def walk(value: Any, *, negate: bool = False) -> None:
        if not isinstance(value, dict):
            raise AdapterError(AdapterErrorCode.SYNTAX, "Weaviate filter must be an object.")
        if isinstance(value.get("and"), list):
            for item in value["and"]:
                walk(item, negate=negate)
            return
        if isinstance(value.get("or"), list):
            branch = [_condition(item) for item in value["or"]]
            (must_not if negate else should).extend(branch)
            return
        if "not" in value:
            walk(value["not"], negate=not negate)
            return
        (must_not if negate else must).append(_condition(value))

    walk(node)
    result: dict[str, Any] = {}
    if must:
        result["must"] = must
    if should:
        result["should"] = should
    if must_not:
        result["must_not"] = must_not
    return result


def _condition(node: Any) -> dict[str, Any]:
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "Weaviate filter has no property.")
    if "eq" in node:
        return {"field": str(field), "op": "equal", "value": node["eq"]}
    if "in" in node:
        return {"field": str(field), "op": "contains_any", "value": list(node["in"])}
    if "range" in node:
        parts = []
        for key, op in (("gt", "greater_than"), ("gte", "greater_or_equal"), ("lt", "less_than"), ("lte", "less_or_equal")):
            if key in node["range"]:
                parts.append({"field": str(field), "op": op, "value": node["range"][key]})
        if len(parts) == 1:
            return parts[0]
        return {"composite": "and", "children": parts}
    operator = str(node.get("operator") or "").upper()
    value = node.get("value", node.get("operand"))
    mapping = {
        "=": "equal",
        "EQ": "equal",
        "!=": "not_equal",
        "<>": "not_equal",
        "<": "less_than",
        "<=": "less_or_equal",
        ">": "greater_than",
        ">=": "greater_or_equal",
    }
    if operator not in mapping:
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Weaviate filter operator {operator!r} is unsupported.")
    return {"field": str(field), "op": mapping[operator], "value": value}


def _to_filter(spec: dict[str, Any] | None) -> Any:
    if not spec or Filter is None:
        return None
    clauses = [_to_clause(item) for item in spec.get("must", [])]
    excluded = [_to_clause(item) for item in spec.get("must_not", [])]
    alternatives = [_to_clause(item) for item in spec.get("should", [])]
    result: Any = None
    for clause in clauses:
        result = clause if result is None else result & clause
    if alternatives:
        branch: Any = None
        for clause in alternatives:
            branch = clause if branch is None else branch | clause
        result = branch if result is None else result & branch
    for clause in excluded:
        result = clause if result is None else result & clause.not_()
    return result


def _to_clause(spec: Any) -> Any:
    if not isinstance(spec, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "Weaviate filter clause is malformed.")
    if "composite" in spec:
        children = [_to_clause(item) for item in spec.get("children", [])]
        if not children:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Weaviate composite filter has no children.")
        result = children[0]
        for clause in children[1:]:
            result = result & clause if spec["composite"] == "and" else result | clause
        return result
    if "field" not in spec:
        raise AdapterError(AdapterErrorCode.SYNTAX, "Weaviate filter clause has no property.")
    prop = Filter.by_property(str(spec["field"]))
    op = spec.get("op")
    value = spec.get("value")
    if op == "equal":
        return prop.equal(value)
    if op == "not_equal":
        return prop.not_equal(value)
    if op == "less_than":
        return prop.less_than(value)
    if op == "less_or_equal":
        return prop.less_or_equal(value)
    if op == "greater_than":
        return prop.greater_than(value)
    if op == "greater_or_equal":
        return prop.greater_or_equal(value)
    if op == "contains_any":
        return prop.contains_any(value)
    raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Weaviate filter operation {op!r} is unsupported.")


def _universal(native: Any) -> str:
    name = str(getattr(native, "value", native)).lower()
    return {
        "text": "text",
        "int": "bigint",
        "number": "double",
        "boolean": "boolean",
        "date": "timestamp",
        "uuid": "uuid",
        "object": "json",
        "geoCoordinates": "geo",
        "phoneNumber": "text",
        "blob": "binary",
    }.get(name, "text")


WEAVIATE_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "int"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "int", notes="64-bit integer."),
    "float": TypeSupport(TypeSupportLevel.COMPATIBLE, "number"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "number"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "number", notes="Stored as a float."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "text"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "boolean"),
    "date": TypeSupport(TypeSupportLevel.EXACT, "date"),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "date"),
    "timestamp": TypeSupport(TypeSupportLevel.EXACT, "date"),
    "datetime": TypeSupport(TypeSupportLevel.COMPATIBLE, "date"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "int", notes="Seconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.EXACT, "uuid"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "text"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "text"),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "object"),
    "binary": TypeSupport(TypeSupportLevel.COMPATIBLE, "blob"),
    "geo": TypeSupport(TypeSupportLevel.EXACT, "geoCoordinates"),
    "list": TypeSupport(TypeSupportLevel.EXACT, "text[]", notes="Typed arrays depend on element type."),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "text[]"),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "object"),
    "vector": TypeSupport(TypeSupportLevel.EXACT, "vector", notes="Named and legacy vectors."),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="Cross-references exist natively but IR 0.1 cannot express them."),
}

WEAVIATE_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No multi-object transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(True, "Writes return object UUIDs."),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(True, "Per-property filterable indexes."),
    CAP_VECTOR_TYPE: Capability(True, "Named and legacy vectors."),
    CAP_JSON: Capability(True),
    CAP_ARRAY_TYPE: Capability(True),
    CAP_UUID_TYPE: Capability(True, "Native UUID object ids."),
    CAP_GEO_TYPE: Capability(True),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(True, "UUID uniqueness only."),
    CAP_INDEXES: Capability(True, "HNSW vector index and inverted property index."),
    CAP_FULL_TEXT_SEARCH: Capability(True, "BM25 and hybrid search."),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Multi-tenancy and sharding."),
    CAP_REPLICATION: Capability(True, "Replication factor per collection."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(True, "Aggregate API with grouped aggregates; not in IR."),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True),
    CAP_TTL: Capability(False),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(True, "HNSW ANN and hybrid search."),
    CAP_SEARCH_QUERY_DSL: Capability(False, "Structured Filter API, not a query DSL."),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False),
    CAP_BATCH_WRITES: Capability(True, "Batch object import."),
    CAP_STRONG_CONSISTENCY: Capability(True, "Writes are synchronous to the replication factor."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Properties can be added; collection schema is explicit."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(True, "Aggregate API; not an aggregation pipeline."),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(True, "Typed properties at the collection level."),
    CAP_NESTED_DOCUMENTS: Capability(True, "Object and object[] properties."),
    CAP_CHANGE_STREAMS: Capability(False),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
