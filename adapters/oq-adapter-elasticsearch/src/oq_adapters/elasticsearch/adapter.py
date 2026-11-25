"""Elasticsearch adapter for Querly.

Elasticsearch is a distributed search engine, not a document database with
transactions. This adapter maps document IR to index/mapping schemas and query
DSL search requests. Mappings are dynamic; inspection records inferred field
types and does not pretend that Elasticsearch enforces a relational schema.

The native artifact is structured JSON DSL. Values live inside the JSON document
and cannot change its structure, so they cannot inject query clauses; this
adapter never builds query DSL by concatenating strings.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

from oq.ir import (
    Artifact,
    CollectionSchema,
    DocumentField,
    DocumentQuery,
    DocumentStoreSchema,
    PlanDocument,
    Type,
)
from oq.runtime.contract import (
    CAP_AGGREGATION_FRAMEWORK,
    CAP_ANN_SEARCH,
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
    CAP_HYBRID_SEARCH,
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
    from elasticsearch import Elasticsearch
    from elasticsearch.exceptions import (
        AuthenticationException,
        NotFoundError,
        RequestError,
        TransportError,
    )
    from elasticsearch.exceptions import (
        ConnectionError as ESConnectionError,
    )
except ImportError:  # pragma: no cover
    Elasticsearch = None
    AuthenticationException = ESConnectionError = NotFoundError = RequestError = TransportError = (
        Exception
    )

__all__ = ["ElasticsearchAdapter", "ElasticsearchConnection", "ElasticsearchTransaction"]

_SUPPORTED = {
    "find",
    "find_one",
    "search",
    "count",
    "insert_one",
    "insert_many",
    "update_one",
    "delete_one",
    "delete_many",
    "aggregate",
}
_WRITES = {"insert_one", "insert_many", "update_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class ElasticsearchConnection(Connection):
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

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    @property
    def client(self) -> Any:
        return self._client

    def close(self) -> None:
        if not self._closed:
            with contextlib.suppress(Exception):
                self._client.close()
            self._closed = True


class ElasticsearchTransaction(Transaction):
    """Placeholder transaction: Elasticsearch has no multi-document transaction."""

    def __init__(self, connection: ElasticsearchConnection, options: TransactionOptions) -> None:
        self._connection = connection
        self._options = options
        self._active = False

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def is_active(self) -> bool:
        return self._active

    def commit(self) -> None:
        self._active = False

    def rollback(self) -> None:
        self._active = False


class ElasticsearchAdapter(DocumentAdapter):
    data_model = "search"
    ir_subset = ("schema", "document CRUD", "search", "count")

    @property
    def id(self) -> str:
        return "elasticsearch"

    @property
    def display_name(self) -> str:
        return "Elasticsearch"

    @property
    def package_version(self) -> str:
        from oq_adapters.elasticsearch import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.elasticsearch import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Elasticsearch",
            tested_versions=[],
            capabilities=ELASTICSEARCH_CAPS,
            types=ELASTICSEARCH_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "No multi-document ACID transactions.",
                "Mappings are dynamic; inspection reports observed field types.",
                "Refresh is near-real-time; a write is not immediately searchable unless refresh=wait_for.",
                "Query DSL is the native artifact; it is structured JSON, never string-concatenated.",
                "Relational joins, constraints, and unique keys are not emulated.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "elasticsearch":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected an elasticsearch config.")
        if Elasticsearch is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "elasticsearch is not installed. Install oq-adapter-elasticsearch[driver].",
            )
        host = config.host or "localhost"
        scheme = config.options.get(
            "scheme", "https" if config.sslmode not in (None, "disable") else "http"
        )
        url = config.options.get("url") or f"{scheme}://{host}:{config.port or 9200}"
        auth = None
        if config.options.get("api_key"):
            auth = ("api_key", config.options["api_key"])
        elif config.user:
            auth = (config.user, secrets.get("password") or "")
        try:
            client = Elasticsearch(
                url,
                basic_auth=auth if isinstance(auth, tuple) and auth[0] != "api_key" else None,
                api_key=auth[1] if isinstance(auth, tuple) and auth[0] == "api_key" else None,
                verify_certs=config.sslmode in ("verify-ca", "verify-full"),
                ca_certs=config.sslrootcert,
                request_timeout=float(config.options.get("timeout", "30")),
            )
            if not client.ping():
                raise AdapterError(
                    AdapterErrorCode.CONNECTION, "Elasticsearch did not answer ping."
                )
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return ElasticsearchConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            info = conn.client.info()
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        raw = str(info.get("version", {}).get("number", "unknown"))
        parts = raw.split(".")
        return EngineVersion(
            product="Elasticsearch",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, ElasticsearchConnection):
            with contextlib.suppress(Exception):
                connection.close()

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            mappings = conn.client.indices.get_mapping()
            counts = {name: conn.client.count(index=name).get("count", 0) for name in mappings}
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        collections = []
        for index_name, payload in mappings.items():
            properties = payload.get("mappings", {}).get("properties", {})
            fields = _flatten_mapping(properties)
            collections.append(
                CollectionSchema(
                    name=str(index_name),
                    fields=[
                        DocumentField(
                            path=path,
                            type=Type(kind=_universal_es_type(native)),
                            nullable=True,
                            frequency=1.0,
                        )
                        for path, native in sorted(fields.items())
                    ],
                    document_count=counts.get(index_name),
                    indexes=[],
                )
            )
        return DocumentStoreSchema(collections=collections)

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        artifacts = []
        for collection in schema.collections:
            properties = _build_mapping(collection.fields)
            body = {"mappings": {"properties": properties}}
            artifacts.append(
                DDLArtifact(
                    sql=json_safe({"create_index": collection.name, "body": body}),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                if index.kind.lower() not in ("text", "fulltext"):
                    continue
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            {
                                "create_index": collection.name,
                                "body": {
                                    "mappings": {
                                        "properties": {
                                            column: {
                                                "type": "text",
                                                "fields": {"keyword": {"type": "keyword"}},
                                            }
                                            for column in index.columns
                                        }
                                    }
                                },
                            }
                        ),
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Elasticsearch")
        diagnostics.extend(
            check_collection(query, {c.name for c in schema.collections}, engine="Elasticsearch")
        )
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "script", "script_score", "runtime_mappings"},
                engine="Elasticsearch",
            )
        )
        if query.operation == "aggregate" and not query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.SYNTAX,
                    "Aggregate requires a pipeline of supported stages.",
                    operation="aggregate",
                    feature="pipeline",
                )
            )
        if diagnostics:
            return unsupported_document_plan(self.id, "elasticsearch", query, diagnostics)

        body: dict[str, Any] = {}
        if query.operation in ("find", "find_one", "search", "count", "aggregate"):
            body["query"] = _filter_to_query(query.filter)
            if query.projection:
                body["_source"] = [str(key) for key in query.projection if key not in ("_id",)]
            if query.sort:
                body["sort"] = [
                    {
                        str(item.get("field")): {
                            "order": "desc"
                            if str(item.get("direction", 1)) in ("-1", "desc", "DESC")
                            else "asc"
                        }
                    }
                    for item in query.sort
                    if item.get("field")
                ]
            if query.skip:
                body["from"] = int(query.skip)
            limit = query.limit or (1 if query.operation == "find_one" else None)
            if limit:
                body["size"] = int(limit)
            if query.operation == "aggregate":
                body["aggs"] = _pipeline_to_aggs(query.pipeline)
            if query.operation == "search" and getattr(query, "options", {}).get("knn"):
                body["knn"] = query.options["knn"]
        elif query.operation in ("insert_one", "insert_many"):
            body["documents"] = list(query.documents)
            body["refresh"] = getattr(query, "options", {}).get("refresh", False)
        elif query.operation == "update_one":
            body["doc"] = query.updates or {}
            body["upsert"] = bool(query.upsert)
            body["refresh"] = getattr(query, "options", {}).get("refresh", False)
        elif query.operation in ("delete_one", "delete_many"):
            body["query"] = _filter_to_query(query.filter)
            body["refresh"] = getattr(query, "options", {}).get("refresh", False)

        parameters = [
            {"name": parameter.name, "type": type_kind(parameter.type)}
            for parameter in query.parameters
        ]
        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="search-dsl",
                text=json_safe(
                    {"index": query.collection, "operation": query.operation, "body": body}
                ),
                dialect="elasticsearch",
            ),
            parameters=parameters,
            required_capabilities=[CAP_SEARCH_QUERY_DSL],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "_source", "type": "map", "nullable": True}],
            engine_plan={
                "kind": "search-dsl",
                "index": query.collection,
                "operation": query.operation,
                "body": json_safe(body),
            },
            diagnostics=[],
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
        index = native["index"]
        operation = native["operation"]
        body = native.get("body", {})
        client = conn.client
        try:
            if operation in ("find", "find_one", "search", "count"):
                result = client.search(index=index, body=body)
                hits = result.get("hits", {})
                documents = [
                    normalize_document({" _id": hit.get("_id"), **hit.get("_source", {})})
                    for hit in hits.get("hits", [])
                ]
                return DocumentResult(documents=documents, affected_count=None, warnings=[])
            if operation == "aggregate":
                result = client.search(index=index, body=body)
                return DocumentResult(
                    documents=[normalize_document(result.get("aggregations", {}))], warnings=[]
                )
            if operation == "insert_one":
                document = (body.get("documents") or [{}])[0]
                result = client.index(
                    index=index, document=document, refresh=body.get("refresh", False)
                )
                return DocumentResult(documents=[], affected_count=1, warnings=[])
            if operation == "insert_many":
                operations = []
                for document in body.get("documents", []):
                    operations.append({"index": {"_index": index}})
                    operations.append(document)
                result = client.bulk(operations=operations, refresh=body.get("refresh", False))
                if result.get("errors"):
                    raise AdapterError(
                        AdapterErrorCode.CONSTRAINT, "Elasticsearch bulk request reported errors."
                    )
                return DocumentResult(
                    documents=[], affected_count=len(body.get("documents", [])), warnings=[]
                )
            if operation == "update_one":
                result = client.update_by_query(
                    index=index,
                    body={
                        "query": body.get("query", {"match_all": {}}),
                        "script": {
                            "source": "ctx._source.putAll(params.doc)",
                            "params": {"doc": body.get("doc", {})},
                        },
                    },
                    refresh=body.get("refresh", False),
                )
                return DocumentResult(
                    documents=[], affected_count=result.get("updated", 0), warnings=[]
                )
            if operation in ("delete_one", "delete_many"):
                result = client.delete_by_query(
                    index=index,
                    body={"query": body.get("query", {"match_all": {}})},
                    refresh=body.get("refresh", False),
                )
                return DocumentResult(
                    documents=[], affected_count=result.get("deleted", 0), warnings=[]
                )
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"Unsupported Elasticsearch operation {operation!r}."
        )

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Elasticsearch has no multi-document transactions.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "Elasticsearch has no transactions.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> ElasticsearchConnection:
        if not isinstance(connection, ElasticsearchConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION, "Wrong connection type for Elasticsearch."
            )
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if isinstance(exc, AuthenticationException):
            code = AdapterErrorCode.AUTH
        elif isinstance(exc, NotFoundError):
            code = AdapterErrorCode.CAPABILITY
        elif isinstance(exc, RequestError):
            code = AdapterErrorCode.SYNTAX
        elif isinstance(exc, ESConnectionError):
            retryable = True
        return AdapterError(
            code,
            f"Elasticsearch {operation} failed: {text}",
            retryable=retryable,
            cause=exc,
            redacted_cause=text,
        )


ELASTICSEARCH_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "integer/long",
        notes="JSON numbers are mapped by the index mapping.",
    ),
    "bigint": TypeSupport(TypeSupportLevel.COMPATIBLE, "long"),
    "float": TypeSupport(TypeSupportLevel.APPROXIMATE, "float", notes="32-bit float."),
    "double": TypeSupport(TypeSupportLevel.EXACT, "double"),
    "decimal": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "scaled_float/double",
        notes="No arbitrary-precision decimal; scaled_float has a fixed scale.",
    ),
    "text": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "text/keyword",
        notes="text is analyzed; keyword is exact. The adapter chooses keyword for exact fields.",
    ),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "boolean"),
    "date": TypeSupport(
        TypeSupportLevel.COMPATIBLE, "date", notes="Date and datetime share one type."
    ),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "date"),
    "timestamp": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "date",
        notes="Millisecond precision by default; date_nanos is separate.",
    ),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "long", notes="No duration type."),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword", notes="No native UUID type."),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "json": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "object/nested",
        notes="Nested documents must use the nested type to query independently.",
    ),
    "binary": TypeSupport(
        TypeSupportLevel.EXACT, "binary", notes="Base64-encoded in JSON; not indexed by default."
    ),
    "geo": TypeSupport(TypeSupportLevel.EXACT, "geo_point/geo_shape"),
    "list": TypeSupport(
        TypeSupportLevel.EXACT,
        "array",
        notes="Arrays are multi-valued fields, not a distinct type.",
    ),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "array", notes="No set uniqueness."),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "object"),
    "vector": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "dense_vector",
        notes="kNN search requires index options and a recent version.",
    ),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No foreign keys."),
}

ELASTICSEARCH_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No multi-document ACID transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(
        False, "Sort missing values with 'missing', not NULLS FIRST/LAST."
    ),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(True, "dense_vector."),
    CAP_JSON: Capability(True, "Documents are JSON."),
    CAP_ARRAY_TYPE: Capability(True, "Multi-valued fields."),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(True),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(
        False, "_id is unique; arbitrary unique constraints are not enforced."
    ),
    CAP_INDEXES: Capability(True),
    CAP_FULL_TEXT_SEARCH: Capability(True),
    CAP_MATERIALIZED_VIEWS: Capability(
        False, "Transforms/rollups exist but are not materialized views."
    ),
    CAP_PARTITIONING: Capability(True, "Index sharding and time-series data streams."),
    CAP_REPLICATION: Capability(True),
    CAP_JOINS: Capability(
        False, "No relational joins; parent/child and nested documents are different."
    ),
    CAP_GROUP_BY: Capability(True, "Aggregations."),
    CAP_HAVING: Capability(True, "bucket_selector pipeline aggregation."),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "Update API upsert."),
    CAP_TTL: Capability(True, "ILM and data stream retention, not per-document TTL."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False, "Graph explore is not a general graph engine."),
    CAP_VECTOR_SEARCH: Capability(True, "kNN/dense_vector; version-dependent."),
    CAP_SEARCH_QUERY_DSL: Capability(True),
    CAP_SERVER_SIDE_SCRIPTING: Capability(
        True, "Painless; never emitted from user filters by this adapter."
    ),
    CAP_BATCH_WRITES: Capability(True, "Bulk API."),
    CAP_STRONG_CONSISTENCY: Capability(False, "Near-real-time refresh and replica lag."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Dynamic mappings and reindex."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_ANN_SEARCH: Capability(True),
    CAP_HYBRID_SEARCH: Capability(True, "Combine kNN and text queries; not expressible in IR."),
    CAP_AGGREGATION_FRAMEWORK: Capability(True),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False, "Mappings are not a JSON Schema validator."),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(
        True, "Change data streams via _change_stream or CCR depending on version."
    ),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}


def _flatten_mapping(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    for name, spec in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        native = str(spec.get("type", "object"))
        result[path] = native
        if "properties" in spec:
            result.update(_flatten_mapping(spec["properties"], path))
    return result


def _build_mapping(fields: list[DocumentField]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in fields:
        properties[field.path] = _es_mapping_for(field.type.kind)
    return properties


def _es_mapping_for(kind: str) -> dict[str, Any]:
    mapping = {
        "integer": {"type": "long"},
        "bigint": {"type": "long"},
        "float": {"type": "float"},
        "double": {"type": "double"},
        "decimal": {"type": "scaled_float", "scaling_factor": 1000000},
        "text": {"type": "keyword"},
        "boolean": {"type": "boolean"},
        "date": {"type": "date"},
        "time": {"type": "date"},
        "timestamp": {"type": "date"},
        "duration": {"type": "long"},
        "uuid": {"type": "keyword"},
        "email": {"type": "keyword"},
        "url": {"type": "keyword"},
        "json": {"type": "object"},
        "binary": {"type": "binary"},
        "geo": {"type": "geo_point"},
        "list": {"type": "keyword"},
        "set": {"type": "keyword"},
        "map": {"type": "object"},
        "vector": {"type": "dense_vector"},
    }
    return dict(mapping.get(kind, {"type": "keyword"}))


def _universal_es_type(native: str) -> str:
    return {
        "long": "bigint",
        "integer": "integer",
        "short": "integer",
        "byte": "integer",
        "float": "float",
        "double": "double",
        "half_float": "float",
        "scaled_float": "decimal",
        "keyword": "text",
        "text": "text",
        "boolean": "boolean",
        "date": "timestamp",
        "date_nanos": "timestamp",
        "binary": "binary",
        "geo_point": "geo",
        "geo_shape": "geo",
        "object": "map",
        "nested": "json",
        "dense_vector": "vector",
        "sparse_vector": "vector",
    }.get(native, "json")


def _filter_to_query(node: Any) -> dict[str, Any]:
    if not node:
        return {"match_all": {}}
    if not isinstance(node, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "Elasticsearch filter must be an object.")
    if "and" in node and isinstance(node["and"], list):
        return {"bool": {"must": [_filter_to_query(item) for item in node["and"]]}}
    if "or" in node and isinstance(node["or"], list):
        return {
            "bool": {
                "should": [_filter_to_query(item) for item in node["or"]],
                "minimum_should_match": 1,
            }
        }
    if "not" in node:
        return {"bool": {"must_not": [_filter_to_query(node["not"])]}}
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "Elasticsearch filter has no field.")
    if "eq" in node:
        return {"term": {str(field): node["eq"]}}
    if "in" in node:
        return {"terms": {str(field): list(node["in"])}}
    if "range" in node and isinstance(node["range"], dict):
        return {"range": {str(field): node["range"]}}
    if "exists" in node:
        return (
            {"exists": {"field": str(field)}}
            if node["exists"]
            else {"bool": {"must_not": {"exists": {"field": str(field)}}}}
        )
    if "match" in node:
        return {"match": {str(field): node["match"]}}
    if "wildcard" in node:
        return {"wildcard": {str(field): node["wildcard"]}}
    operator = str(node.get("operator") or "")
    value = node.get("value")
    if operator in ("=", "eq"):
        return {"term": {str(field): value}}
    if operator in ("!=", "<>", "ne"):
        return {"bool": {"must_not": [{"term": {str(field): value}}]}}
    if operator in ("<", "<=", ">", ">="):
        key = {"<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}[operator]
        return {"range": {str(field): {key: value}}}
    raise AdapterError(
        AdapterErrorCode.UNSUPPORTED, f"Elasticsearch filter operator {operator!r} is unsupported."
    )


def _pipeline_to_aggs(pipeline: list[Any]) -> dict[str, Any]:
    aggs: dict[str, Any] = {}
    group_fields: list[str] = []
    for stage in pipeline:
        operator = stage.operator
        value = stage.value
        if operator == "$group" and isinstance(value, dict):
            identifier = value.get("_id")
            if isinstance(identifier, str):
                group_fields.append(identifier)
            elif isinstance(identifier, dict):
                group_fields.extend(str(field) for field in identifier.values())
        elif operator in ("$match", "$sort", "$limit", "$skip"):
            continue
        else:
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                f"Elasticsearch aggregation stage {operator!r} is unsupported.",
            )
    for field in group_fields:
        aggs[field] = {"terms": {"field": field, "size": 1000}}
    return aggs or {"all": {"terms": {"field": "_index", "size": 1}}}
