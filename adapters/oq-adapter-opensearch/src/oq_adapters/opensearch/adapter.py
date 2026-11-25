"""OpenSearch adapter for Querly.

OpenSearch is an Apache-2.0 fork of Elasticsearch 7.10 with a diverging API:
the k-NN plugin uses a ``query.knn`` clause, SQL/PPL plugins are optional, and
AWS-managed domains use SigV4 request signing. This adapter targets
``opensearch-py`` and reports OpenSearch capabilities rather than Elasticsearch
ones. Query bodies are structured JSON, never string-concatenated.

Driver: ``opensearch-py`` (optional extra ``driver``), imported lazily.
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
from oq_adapters.kit import build_report, json_safe, type_kind
from oq_adapters.kit.document import (
    check_collection,
    check_document_operation,
    check_unsafe_filter_operators,
    document_safety,
    normalize_document,
    unsupported_document_plan,
)

try:
    from opensearchpy import OpenSearch
    from opensearchpy.exceptions import (
        AuthenticationException,
        NotFoundError,
        RequestError,
    )
    from opensearchpy.exceptions import (
        ConnectionError as OpenSearchConnectionError,
    )
except ImportError:  # pragma: no cover
    OpenSearch = None
    AuthenticationException = OpenSearchConnectionError = NotFoundError = RequestError = Exception

__all__ = ["OpenSearchAdapter", "OpenSearchConnection"]

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


class OpenSearchConnection(Connection):
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
            return bool(self._client.ping())
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            with contextlib.suppress(Exception):
                self._client.close()
            self._closed = True


class OpenSearchAdapter(DocumentAdapter):
    data_model = "search"
    ir_subset = ("schema", "document CRUD", "search", "count")

    @property
    def id(self) -> str:
        return "opensearch"

    @property
    def display_name(self) -> str:
        return "OpenSearch"

    @property
    def package_version(self) -> str:
        from oq_adapters.opensearch import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.opensearch import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="OpenSearch",
            tested_versions=[],
            capabilities=OPENSEARCH_CAPS,
            types=OPENSEARCH_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "No multi-document ACID transactions.",
                "k-NN is provided by the OpenSearch k-NN plugin and requires index settings; version-dependent.",
                "AWS domains require SigV4 signing, configured with service/region options.",
                "SQL/PPL plugins are optional and are not used for compiled queries.",
                "Mappings are dynamic; inspection reports observed mappings.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "opensearch":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected an opensearch config.")
        if OpenSearch is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "opensearch-py is not installed. Install oq-adapter-opensearch[driver].",
            )
        host = config.host or "localhost"
        scheme = config.options.get(
            "scheme", "https" if config.sslmode not in (None, "disable") else "http"
        )
        url = config.options.get("url") or f"{scheme}://{host}:{config.port or 9200}"
        auth: Any = None
        if config.options.get("aws_sigv4"):
            try:
                import boto3
                from botocore.auth import SigV4Auth
                from botocore.awsrequest import AWSRequest
            except ImportError as exc:  # pragma: no cover
                raise AdapterError(
                    AdapterErrorCode.CONNECTION, "boto3 is required for SigV4."
                ) from exc
            session = boto3.Session(
                aws_access_key_id=secrets.get("access_key_id"),
                aws_secret_access_key=secrets.get("secret_access_key"),
                aws_session_token=secrets.get("session_token"),
                region_name=config.options.get("region"),
            )

            class _SigV4Auth:
                def __call__(self, request: Any) -> Any:  # pragma: no cover - network path
                    aws_request = AWSRequest(
                        method=request.method, url=request.url, data=request.body
                    )
                    SigV4Auth(
                        session.get_credentials(), "es", config.options.get("region", "us-east-1")
                    ).add_auth(aws_request)
                    request.headers.update(dict(aws_request.headers))
                    return request

            auth = _SigV4Auth()
        elif config.user:
            auth = (config.user, secrets.get("password") or "")
        try:
            client = OpenSearch(
                hosts=[url],
                http_auth=auth,
                use_ssl=scheme == "https",
                verify_certs=config.sslmode in ("verify-ca", "verify-full"),
                ca_certs=config.sslrootcert,
                ssl_assert_hostname=config.sslmode == "verify-full",
                timeout=float(config.options.get("timeout", "30")),
                max_retries=int(config.options.get("max_retries", "3")),
            )
            if not client.ping():
                raise AdapterError(AdapterErrorCode.CONNECTION, "OpenSearch did not answer ping.")
        except AdapterError:
            raise
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return OpenSearchConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            info = conn.client.info()
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        distribution = info.get("version", {}).get("distribution", "opensearch")
        raw = str(info.get("version", {}).get("number", "unknown"))
        parts = raw.split(".")
        return EngineVersion(
            product=f"OpenSearch ({distribution})",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, OpenSearchConnection):
            with contextlib.suppress(Exception):
                connection.close()

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            mappings = conn.client.indices.get_mapping()
            collections = []
            for index, payload in mappings.items():
                if str(index).startswith("."):
                    continue
                properties = payload.get("mappings", {}).get("properties", {})
                fields = _flatten(properties)
                count = conn.client.count(index=index).get("count", 0)
                collections.append(
                    CollectionSchema(
                        name=str(index),
                        fields=[
                            DocumentField(
                                path=path,
                                type=Type(kind=_universal(kind)),
                                nullable=True,
                                frequency=1.0,
                            )
                            for path, kind in sorted(fields.items())
                        ],
                        document_count=int(count),
                        indexes=[],
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
            properties = {field.path: _mapping(field.type.kind) for field in collection.fields}
            artifacts.append(
                DDLArtifact(
                    sql=json_safe(
                        {
                            "create_index": collection.name,
                            "body": {
                                "settings": {"index": {"knn": True}},
                                "mappings": {"properties": properties},
                            },
                        }
                    ),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="OpenSearch")
        diagnostics.extend(
            check_collection(query, {c.name for c in schema.collections}, engine="OpenSearch")
        )
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "script", "script_score", "runtime_mappings"},
                engine="OpenSearch",
            )
        )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "opensearch", query, diagnostics)

        body: dict[str, Any] = {}
        if query.operation in ("find", "find_one", "search", "count", "aggregate"):
            body["query"] = _query(query.filter)
            if query.projection:
                body["_source"] = [str(key) for key in query.projection if key != "_id"]
            if query.sort:
                body["sort"] = [
                    {
                        str(item["field"]): {
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
                body["aggs"] = _aggs(query.pipeline)
            vector = (
                getattr(query, "options", {}).get("vector")
                if getattr(query, "options", None)
                else None
            )
            if vector and query.operation in ("search", "find"):
                field = str(getattr(query, "options", {}).get("vector_field", "embedding"))
                body["query"] = {
                    "knn": {
                        field: {
                            "vector": list(vector),
                            "k": int(body.get("size", 10)),
                        }
                    }
                }
        elif query.operation in ("insert_one", "insert_many"):
            body["documents"] = list(query.documents)
            body["refresh"] = getattr(query, "options", {}).get("refresh", False)
        elif query.operation == "update_one":
            body["doc"] = query.updates or {}
            body["upsert"] = bool(query.upsert)
        elif query.operation in ("delete_one", "delete_many"):
            body["query"] = _query(query.filter)

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="opensearch-dsl",
                text=json_safe(
                    {"index": query.collection, "operation": query.operation, "body": body}
                ),
                dialect="opensearch",
            ),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            required_capabilities=[CAP_SEARCH_QUERY_DSL],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "_source", "type": "map", "nullable": True}],
            engine_plan={
                "kind": "opensearch",
                "index": query.collection,
                "operation": query.operation,
                "body": json_safe(body),
            },
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
        index = native["index"]
        operation = native["operation"]
        body = native.get("body", {})
        client = conn.client
        try:
            if operation in ("find", "find_one", "search", "count"):
                result = client.search(index=index, body=body)
                hits = result.get("hits", {})
                documents = [
                    normalize_document({"_id": hit.get("_id"), **hit.get("_source", {})})
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
                client.index(index=index, body=document, refresh=body.get("refresh", False))
                return DocumentResult(documents=[], affected_count=1, warnings=[])
            if operation == "insert_many":
                operations = []
                for document in body.get("documents", []):
                    operations.append({"index": {"_index": index}})
                    operations.append(document)
                result = client.bulk(body=operations, refresh=body.get("refresh", False))
                if result.get("errors"):
                    raise AdapterError(
                        AdapterErrorCode.CONSTRAINT, "OpenSearch bulk request reported errors."
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
                )
                return DocumentResult(
                    documents=[], affected_count=result.get("deleted", 0), warnings=[]
                )
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"Unsupported OpenSearch operation {operation!r}."
        )

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY, "OpenSearch has no multi-document transactions."
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "OpenSearch has no transactions.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> OpenSearchConnection:
        if not isinstance(connection, OpenSearchConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for OpenSearch.")
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
        elif isinstance(exc, OpenSearchConnectionError):
            retryable = True
        return AdapterError(
            code,
            f"OpenSearch {operation} failed: {text}",
            retryable=retryable,
            cause=exc,
            redacted_cause=text,
        )


def _flatten(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    for name, spec in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        result[path] = str(spec.get("type", "object"))
        if "properties" in spec:
            result.update(_flatten(spec["properties"], path))
    return result


def _universal(kind: str) -> str:
    return {
        "long": "bigint",
        "integer": "integer",
        "short": "integer",
        "byte": "integer",
        "float": "float",
        "double": "double",
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
        "knn_vector": "vector",
    }.get(kind, "json")


def _mapping(kind: str) -> dict[str, Any]:
    return {
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
        "vector": {"type": "knn_vector", "dimension": 1536},
    }.get(kind, {"type": "keyword"})


def _query(node: Any) -> dict[str, Any]:
    if not node:
        return {"match_all": {}}
    if not isinstance(node, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "OpenSearch filter must be an object.")
    if "and" in node and isinstance(node["and"], list):
        return {"bool": {"must": [_query(item) for item in node["and"]]}}
    if "or" in node and isinstance(node["or"], list):
        return {
            "bool": {"should": [_query(item) for item in node["or"]], "minimum_should_match": 1}
        }
    if "not" in node:
        return {"bool": {"must_not": [_query(node["not"])]}}
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "OpenSearch filter has no field.")
    if "eq" in node:
        return {"term": {str(field): node["eq"]}}
    if "in" in node:
        return {"terms": {str(field): list(node["in"])}}
    if "range" in node:
        return {"range": {str(field): dict(node["range"])}}
    if "exists" in node:
        return (
            {"exists": {"field": str(field)}}
            if node["exists"]
            else {"bool": {"must_not": {"exists": {"field": str(field)}}}}
        )
    if "match" in node:
        return {"match": {str(field): node["match"]}}
    operator = str(node.get("operator") or "").upper()
    value = node.get("value", node.get("operand"))
    mapping = {
        "=": ("term", None),
        "EQ": ("term", None),
        "!=": ("not", None),
        "<>": ("not", None),
        "<": (">compare", "lt"),
        "<=": (">compare", "lte"),
        ">": (">compare", "gt"),
        ">=": (">compare", "gte"),
    }
    if operator not in mapping:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"OpenSearch filter operator {operator!r} is unsupported."
        )
    kind, key = mapping[operator]
    if kind == "term":
        return {"term": {str(field): value}}
    if kind == "not":
        return {"bool": {"must_not": [{"term": {str(field): value}}]}}
    return {"range": {str(field): {key: value}}}


def _aggs(pipeline: list[Any]) -> dict[str, Any]:
    aggs: dict[str, Any] = {}
    for stage in pipeline:
        if stage.operator == "$group" and isinstance(stage.value, dict):
            identifier = stage.value.get("_id")
            if isinstance(identifier, str):
                aggs[identifier] = {"terms": {"field": identifier, "size": 1000}}
            elif isinstance(identifier, dict):
                for field in identifier.values():
                    aggs[str(field)] = {"terms": {"field": str(field), "size": 1000}}
        elif stage.operator in ("$match", "$sort", "$limit", "$skip"):
            continue
        else:
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                f"OpenSearch aggregation stage {stage.operator!r} is unsupported.",
            )
    return aggs or {"all": {"terms": {"field": "_index", "size": 1}}}


OPENSEARCH_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "long"),
    "bigint": TypeSupport(TypeSupportLevel.COMPATIBLE, "long"),
    "float": TypeSupport(TypeSupportLevel.APPROXIMATE, "float"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "double"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "scaled_float"),
    "text": TypeSupport(TypeSupportLevel.COMPATIBLE, "text/keyword"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "boolean"),
    "date": TypeSupport(TypeSupportLevel.COMPATIBLE, "date"),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "date"),
    "timestamp": TypeSupport(TypeSupportLevel.COMPATIBLE, "date"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "long"),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "keyword"),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "object/nested"),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "binary"),
    "geo": TypeSupport(TypeSupportLevel.EXACT, "geo_point/geo_shape"),
    "list": TypeSupport(TypeSupportLevel.EXACT, "array"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "array"),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "object"),
    "vector": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "knn_vector",
        notes="Requires the k-NN plugin and index settings.",
    ),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No foreign keys."),
}

OPENSEARCH_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(True, "knn_vector via the k-NN plugin."),
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
    CAP_UNIQUE_CONSTRAINTS: Capability(False, "_id only."),
    CAP_INDEXES: Capability(True),
    CAP_FULL_TEXT_SEARCH: Capability(True),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Shards and index state management."),
    CAP_REPLICATION: Capability(True),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(True, "Aggregations."),
    CAP_HAVING: Capability(True, "bucket_selector."),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True),
    CAP_TTL: Capability(True, "Index State Management retention."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(True, "k-NN plugin."),
    CAP_SEARCH_QUERY_DSL: Capability(True),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "Painless; never emitted from user filters."),
    CAP_BATCH_WRITES: Capability(True, "Bulk API."),
    CAP_STRONG_CONSISTENCY: Capability(False),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(True),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(
        True, "Index translog and cross-cluster replication; no change feed equivalent."
    ),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
