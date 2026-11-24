"""Qdrant adapter for Querly.

Qdrant is a vector similarity search engine. Querly IR has no vector type in its
document query, so this adapter uses the portable document subset plus an
explicit ``options["vector"]`` payload: a collection is a Qdrant collection, a
document is a point payload, and ``_id`` is the point id.

Supported operations are vector search (``search``/``find``/``near_vector``),
scroll (``find`` without a vector), ``count``, upsert-style writes, payload
updates, and id/filter deletes. Filters are compiled into Qdrant's structured
filter objects, never into a query string.

Driver: ``qdrant-client`` (optional extra ``driver``), imported lazily.
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
    from qdrant_client import QdrantClient, models
except ImportError:  # pragma: no cover
    QdrantClient = None
    models = None

__all__ = ["QdrantAdapter", "QdrantConnection"]

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


class QdrantConnection(Connection):
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
            self._client.get_collections()
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True


class QdrantAdapter(DocumentAdapter):
    data_model = "vector"
    ir_subset = ("schema", "vector search", "point writes", "count", "scroll")

    @property
    def id(self) -> str:
        return "qdrant"

    @property
    def display_name(self) -> str:
        return "Qdrant"

    @property
    def package_version(self) -> str:
        from oq_adapters.qdrant import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.qdrant import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Qdrant",
            tested_versions=[],
            capabilities=QDRANT_CAPS,
            types=QDRANT_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "Vector search requires options['vector']; the portable IR filter alone performs a scroll.",
                "No joins, GROUP BY, HAVING, or aggregation pipeline.",
                "Points have at most one named vector configuration per advertised vector name.",
                "No multi-point transactions; upserts are atomic per request but not rollback-able.",
                "Full-text search is token-based payload matching, not BM25 ranking.",
                "Payload indexes are created explicitly and are not required for correct filtering.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "qdrant":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a qdrant config.")
        if QdrantClient is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "qdrant-client is not installed. Install oq-adapter-qdrant[driver].",
            )
        url = config.options.get("url") or (
            f"{'https' if config.sslmode not in (None, 'disable') else 'http'}"
            f"://{config.host or 'localhost'}:{config.port or 6333}"
        )
        kwargs: dict[str, Any] = {
            "url": url,
            "prefer_grpc": bool(config.options.get("prefer_grpc", False)),
            "timeout": int(config.options.get("timeout", "30")),
        }
        api_key = secrets.get("api_key") or config.options.get("api_key")
        if api_key:
            kwargs["api_key"] = api_key
        try:
            client = QdrantClient(**kwargs)
            client.get_collections()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return QdrantConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            info = conn.client.info()
            raw = str(getattr(info, "version", None) or (info.get("version") if isinstance(info, dict) else "unknown"))
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="Qdrant",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, QdrantConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            collections = []
            for item in conn.client.get_collections().collections:
                name = str(item.name)
                description = conn.client.get_collection(name)
                vectors = getattr(description.config.params, "vectors", None)
                fields: list[DocumentField] = []
                if hasattr(vectors, "size"):
                    fields.append(
                        DocumentField(
                            path="vector",
                            type=Type(kind="vector"),
                            nullable=False,
                            frequency=1.0,
                        )
                    )
                elif isinstance(vectors, dict):
                    for vector_name, params in vectors.items():
                        fields.append(
                            DocumentField(
                                path=str(vector_name),
                                type=Type(kind="vector"),
                                nullable=False,
                                frequency=1.0,
                                options={"size": getattr(params, "size", None)},
                            )
                        )
                try:
                    points, _ = conn.client.scroll(
                        name, limit=100, with_payload=True, with_vectors=False
                    )
                    seen: dict[str, str] = {}
                    for point in points:
                        for key, value in (point.payload or {}).items():
                            seen.setdefault(str(key), _qdrant_kind(value))
                    for key, kind in sorted(seen.items()):
                        fields.append(
                            DocumentField(
                                path=key,
                                type=Type(kind=kind),
                                nullable=True,
                                frequency=1.0,
                            )
                        )
                except Exception:
                    pass
                payload_schema = getattr(description, "payload_schema", None) or {}
                indexes = [
                    Index(
                        name=str(field),
                        columns=[str(field)],
                        unique=False,
                        kind="qdrant-payload-index",
                        options={"schema": str(getattr(spec, "data_type", spec))},
                    )
                    for field, spec in payload_schema.items()
                ]
                collections.append(
                    CollectionSchema(
                        name=name,
                        fields=fields,
                        document_count=int(getattr(description, "points_count", 0) or 0),
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
            vector_field = next(
                (field for field in collection.fields if field.type.kind == "vector"),
                None,
            )
            if vector_field is None:
                raise AdapterError(
                    AdapterErrorCode.CAPABILITY,
                    f"Qdrant collection {collection.name!r} needs a vector field.",
                )
            vector_options = vector_field.options or {}
            artifacts.append(
                DDLArtifact(
                    sql=json_safe(
                        {
                            "create_collection": collection.name,
                            "vectors": {
                                "size": int(vector_options.get("size", 1536)),
                                "distance": str(vector_options.get("distance", "Cosine")),
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
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            {
                                "create_payload_index": index.name,
                                "collection": collection.name,
                                "field": index.columns[0],
                                "schema": index.options.get("schema", "keyword"),
                            }
                        ),
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Qdrant")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="Qdrant"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "script"},
                engine="Qdrant",
            )
        )
        vector = (query.options or {}).get("vector")
        if query.operation in _SEARCH_OPS and not vector:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.SYNTAX,
                    "Qdrant search requires options['vector'] with a numeric list.",
                    operation=query.operation,
                    feature="vector",
                )
            )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Qdrant has no aggregation pipeline; use payload filters and facets.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if query.sort or query.skip:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Qdrant scroll does not support portable sort/skip; use a vector or payload index.",
                    operation=query.operation,
                    feature="query_shape",
                )
            )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "qdrant", query, diagnostics)

        plan: dict[str, Any] = {
            "collection": query.collection,
            "operation": query.operation,
            "filter": _filter_spec(query.filter) if query.filter else None,
            "limit": int(query.limit or (1 if query.operation == "find_one" else 10)),
            "with_payload": True,
        }
        if vector:
            plan["vector"] = [float(value) for value in vector]
            plan["score_threshold"] = (query.options or {}).get("score_threshold")
            plan["vector_name"] = (query.options or {}).get("vector_name")
        if query.operation in ("insert_one", "insert_many", "upsert"):
            plan["points"] = [
                {
                    "id": document.get("_id") or document.get("id"),
                    "payload": {
                        key: value
                        for key, value in document.items()
                        if key not in ("_id", "id", "vector")
                    },
                    "vector": document.get("vector") or vector,
                }
                for document in query.documents
            ]
        elif query.operation == "update_one":
            plan["payload"] = dict(query.updates or {})
        elif query.operation in ("delete_one", "delete_many"):
            plan["ids"] = [
                node.get("eq", node.get("value"))
                for node in _equality_nodes(query.filter)
                if str(node.get("field") or node.get("name")) in ("_id", "id")
            ]
        elif query.operation == "get":
            plan["ids"] = [
                node.get("eq", node.get("value"))
                for node in _equality_nodes(query.filter)
                if str(node.get("field") or node.get("name")) in ("_id", "id")
            ]

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="qdrant-request", text=json_safe(plan), dialect="qdrant"),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            required_capabilities=[CAP_VECTOR_SEARCH] if vector else [],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "point", "type": "map", "nullable": True}],
            engine_plan={"kind": "qdrant", **json_safe(plan)},
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
        filter_object = _to_filter(native.get("filter")) if native.get("filter") else None
        try:
            if operation in _SEARCH_OPS:
                results = self._search(client, native, filter_object)
                documents = [
                    normalize_document({"_id": str(point.id), "_score": point.score, **(point.payload or {})})
                    for point in results
                ]
                return DocumentResult(documents=documents[: int(native["limit"])], warnings=[])
            if operation in ("find", "find_one"):
                if native.get("vector") and operation == "find":
                    results = self._search(client, native, filter_object)
                    documents = [
                        normalize_document({"_id": str(point.id), "_score": point.score, **(point.payload or {})})
                        for point in results
                    ]
                    return DocumentResult(documents=documents, warnings=[])
                points, _ = client.scroll(
                    collection_name=collection,
                    scroll_filter=filter_object,
                    limit=int(native["limit"]),
                    with_payload=True,
                    with_vectors=False,
                )
                documents = [
                    normalize_document({"_id": str(point.id), **(point.payload or {})})
                    for point in points
                ]
                return DocumentResult(documents=documents, warnings=[])
            if operation == "get":
                ids = [item for item in native.get("ids", []) if item is not None]
                if not ids:
                    raise AdapterError(AdapterErrorCode.SYNTAX, "Qdrant get requires an id equality filter.")
                points = client.retrieve(collection_name=collection, ids=ids, with_payload=True)
                return DocumentResult(
                    documents=[normalize_document({"_id": str(point.id), **(point.payload or {})}) for point in points],
                    affected_count=len(points),
                    warnings=[],
                )
            if operation == "count":
                result = client.count(collection_name=collection, count_filter=filter_object, exact=True)
                return DocumentResult(documents=[], affected_count=int(result.count), warnings=[])
            if operation in ("insert_one", "insert_many", "upsert"):
                points = []
                for item in native.get("points", []):
                    if item.get("id") is None:
                        raise AdapterError(
                            AdapterErrorCode.SYNTAX,
                            "Qdrant writes require _id to identify the point.",
                        )
                    if item.get("vector") is None:
                        raise AdapterError(
                            AdapterErrorCode.SYNTAX,
                            "Qdrant writes require a vector in options or the document.",
                        )
                    points.append(
                        models.PointStruct(
                            id=item["id"],
                            vector=item["vector"],
                            payload=item.get("payload") or {},
                        )
                    )
                client.upsert(collection_name=collection, points=points, wait=True)
                return DocumentResult(documents=[], affected_count=len(points), warnings=[])
            if operation == "update_one":
                if not filter_object:
                    raise AdapterError(
                        AdapterErrorCode.UNSUPPORTED,
                        "Qdrant payload updates require a filter; use upsert to add points.",
                    )
                client.set_payload(
                    collection_name=collection,
                    payload=native.get("payload", {}),
                    points=filter_object,
                    wait=True,
                )
                return DocumentResult(documents=[], affected_count=None, warnings=[])
            if operation in ("delete_one", "delete_many"):
                ids = [item for item in native.get("ids", []) if item is not None]
                if ids:
                    selector: Any = models.PointIdsList(points=ids)
                elif filter_object is not None:
                    selector = models.FilterSelector(filter=filter_object)
                else:
                    raise AdapterError(
                        AdapterErrorCode.UNSUPPORTED,
                        "Qdrant delete requires ids or a filter; unfiltered deletes are refused.",
                    )
                client.delete(
                    collection_name=collection,
                    points_selector=selector,
                    wait=True,
                )
                return DocumentResult(documents=[], affected_count=len(ids) or None, warnings=[])
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported Qdrant operation {operation!r}.")

    @staticmethod
    def _search(client: Any, native: dict[str, Any], filter_object: Any) -> list[Any]:
        vector = native.get("vector")
        kwargs: dict[str, Any] = {
            "collection_name": native["collection"],
            "query": vector,
            "query_filter": filter_object,
            "limit": int(native["limit"]),
            "with_payload": True,
        }
        if native.get("score_threshold") is not None:
            kwargs["score_threshold"] = float(native["score_threshold"])
        name = native.get("vector_name")
        if name:
            kwargs["using"] = str(name)
        if hasattr(client, "query_points"):
            response = client.query_points(**kwargs)
            return list(getattr(response, "points", response))
        return list(client.search(**kwargs))

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Qdrant has no multi-point transactions; each upsert/delete request is atomic.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "Qdrant has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> QdrantConnection:
        if not isinstance(connection, QdrantConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Qdrant.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        lower = text.lower()
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "unauthorized" in lower or "api key" in lower or "forbidden" in lower:
            code = AdapterErrorCode.AUTH
        elif "not found" in lower or "doesn't exist" in lower:
            code = AdapterErrorCode.CAPABILITY
        elif "bad request" in lower or "validation" in lower or "dimension" in lower:
            code = AdapterErrorCode.SYNTAX
        elif "timeout" in lower or "unavailable" in lower or "too many" in lower:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        return AdapterError(code, f"Qdrant {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


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
    """Compile an IR filter into a JSON-safe Qdrant filter specification."""

    must: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []
    should: list[dict[str, Any]] = []

    def walk(value: Any, *, negate: bool = False) -> None:
        if not isinstance(value, dict):
            raise AdapterError(AdapterErrorCode.SYNTAX, "Qdrant filter must be an object.")
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
        condition = _condition(value)
        (must_not if negate else must).append(condition)

    walk(node)
    result: dict[str, Any] = {}
    if must:
        result["must"] = must
    if must_not:
        result["must_not"] = must_not
    if should:
        result["should"] = should
    return result


def _condition(node: Any) -> dict[str, Any]:
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "Qdrant filter has no field.")
    if "eq" in node:
        return {"key": str(field), "match": {"value": node["eq"]}}
    if "in" in node:
        return {"key": str(field), "match": {"any": list(node["in"])}}
    if "exists" in node:
        return {"is_empty": {"key": str(field)}} if not node["exists"] else {"key": str(field), "match": {"except": []}}
    if "range" in node:
        return {"key": str(field), "range": dict(node["range"])}
    operator = str(node.get("operator") or "").upper()
    value = node.get("value", node.get("operand"))
    if operator in ("=", "EQ"):
        return {"key": str(field), "match": {"value": value}}
    if operator in ("!=", "<>", "NE"):
        return {"key": str(field), "match": {"except": [value]}}
    mapping = {"<": "lt", "<=": "lte", ">": "gt", ">=": "gte", "LT": "lt", "LTE": "lte", "GT": "gt", "GTE": "gte"}
    if operator in mapping:
        return {"key": str(field), "range": {mapping[operator]: value}}
    raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Qdrant filter operator {operator!r} is unsupported.")


def _to_filter(spec: dict[str, Any] | None) -> Any:
    if not spec or models is None:
        return None
    kwargs: dict[str, Any] = {}
    if spec.get("must"):
        kwargs["must"] = [_to_condition(item) for item in spec["must"]]
    if spec.get("must_not"):
        kwargs["must_not"] = [_to_condition(item) for item in spec["must_not"]]
    if spec.get("should"):
        kwargs["should"] = [_to_condition(item) for item in spec["should"]]
    return models.Filter(**kwargs)


def _to_condition(spec: dict[str, Any]) -> Any:
    key = spec.get("key")
    if "match" in spec:
        match = spec["match"]
        if "value" in match:
            return models.FieldCondition(key=key, match=models.MatchValue(value=match["value"]))
        if "any" in match:
            return models.FieldCondition(key=key, match=models.MatchAny(any=match["any"]))
        if "except" in match:
            return models.FieldCondition(key=key, match=models.MatchExcept(**{"except": match["except"]}))
    if "range" in spec:
        return models.FieldCondition(key=key, range=models.Range(**spec["range"]))
    if "is_empty" in spec:
        return models.IsEmptyCondition(is_empty=models.PayloadField(key=spec["is_empty"]["key"]))
    raise AdapterError(AdapterErrorCode.SYNTAX, "Qdrant filter condition is malformed.")


def _qdrant_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "double"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "text"


QDRANT_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "integer", notes="Payload integer."),
    "bigint": TypeSupport(TypeSupportLevel.COMPATIBLE, "integer"),
    "float": TypeSupport(TypeSupportLevel.COMPATIBLE, "float/double"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "float"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "float", notes="No decimal payload type."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "keyword/text"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "bool"),
    "date": TypeSupport(TypeSupportLevel.APPROXIMATE, "datetime", notes="Stored as an ISO-8601 string or epoch."),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "datetime"),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "datetime"),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "datetime"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "integer"),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "object"),
    "binary": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="Payloads are JSON; base64 strings are application-defined."),
    "geo": TypeSupport(TypeSupportLevel.COMPATIBLE, "geo", notes="Geo radius/bbox filters are available."),
    "list": TypeSupport(TypeSupportLevel.EXACT, "array"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "array"),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "object"),
    "vector": TypeSupport(TypeSupportLevel.EXACT, "dense vector", notes="Named vectors supported."),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No foreign keys."),
}

QDRANT_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No multi-point transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(True, "Payload indexes over subsets are not partial; filter selectors are."),
    CAP_VECTOR_TYPE: Capability(True, "Dense, sparse, and named vectors."),
    CAP_JSON: Capability(True),
    CAP_ARRAY_TYPE: Capability(True),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(True),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(True, "Point ids are unique."),
    CAP_INDEXES: Capability(True, "Payload indexes and HNSW vector indexes."),
    CAP_FULL_TEXT_SEARCH: Capability(True, "Payload text index with token matching."),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Collections shard by point id; snapshots are per collection."),
    CAP_REPLICATION: Capability(True, "Distributed deployment replication."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(True, "Facet API only; not part of this adapter."),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True),
    CAP_TTL: Capability(False),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(True, "Native approximate nearest-neighbour search."),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False),
    CAP_BATCH_WRITES: Capability(True),
    CAP_STRONG_CONSISTENCY: Capability(False, "Wait=true confirms persistence, not isolation."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Payloads are schemaless."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(True, "Points can be upserted, retrieved, and deleted."),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(False),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
