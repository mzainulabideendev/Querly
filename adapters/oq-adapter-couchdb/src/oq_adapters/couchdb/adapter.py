"""Apache CouchDB adapter for Querly.

CouchDB is an HTTP document database with MVCC revisions, Mango indexes, and no
multi-document transactions. This adapter uses CouchDB's own HTTP API: ``_find``
for Mango queries, ``_bulk_docs`` for writes, ``_index`` for Mango indexes, and
``_all_docs`` for schema sampling. A collection is a CouchDB database; a
document is a JSON document with ``_id`` and ``_rev``.

Driver: ``httpx`` (optional extra ``driver``), imported lazily.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
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
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

__all__ = ["CouchDBAdapter", "CouchDBConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "count",
    "insert_one",
    "insert_many",
    "update_one",
    "replace_one",
    "delete_one",
    "delete_many",
}
_WRITES = {"insert_one", "insert_many", "update_one", "replace_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class CouchDBConnection(Connection):
    def __init__(self, config: ConnectionConfig, client: Any, base_url: str) -> None:
        self._config = config
        self._client = client
        self._base_url = base_url
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

    @property
    def base_url(self) -> str:
        return self._base_url

    def ping(self) -> bool:
        try:
            response = self._client.get(f"{self._base_url}/")
            return response.status_code == 200
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True


class CouchDBAdapter(DocumentAdapter):
    data_model = "document"
    ir_subset = ("schema", "document CRUD", "find", "count")

    @property
    def id(self) -> str:
        return "couchdb"

    @property
    def display_name(self) -> str:
        return "Apache CouchDB"

    @property
    def package_version(self) -> str:
        from oq_adapters.couchdb import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.couchdb import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Apache CouchDB",
            tested_versions=[],
            capabilities=COUCHDB_CAPS,
            types=COUCHDB_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "No multi-document ACID transactions; MVCC revisions only.",
                "Mango indexes are not unique indexes; uniqueness is not enforced.",
                "A collection is a CouchDB database, not a table inside one database.",
                "Map/reduce views are design documents and are not generated from IR aggregates.",
                "CouchDB validates documents with validate_doc_update functions; JSON Schema is not native.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "couchdb":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a couchdb config.")
        if httpx is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "httpx is not installed. Install oq-adapter-couchdb[driver].",
            )
        if not config.host:
            raise AdapterError(AdapterErrorCode.CONNECTION, "CouchDB requires a host.")
        scheme = config.options.get("scheme", "https" if config.sslmode not in (None, "disable") else "http")
        base_url = config.options.get("url") or f"{scheme}://{config.host}:{config.port or 5984}"
        auth = None
        if config.user:
            auth = (config.user, secrets.get("password") or "")
        try:
            client = httpx.Client(
                base_url=base_url,
                auth=auth,
                verify=config.sslmode not in (None, "disable"),
                timeout=float(config.options.get("timeout", "30")),
                headers={"Accept": "application/json"},
            )
            response = client.get("/")
            response.raise_for_status()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return CouchDBConnection(config, client, base_url)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            payload = conn.client.get("/").json()
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        raw = str(payload.get("version", "unknown"))
        parts = raw.split(".")
        return EngineVersion(
            product="Apache CouchDB",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, CouchDBConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            databases = conn.client.get("/_all_dbs").json()
            collections = []
            for name in databases:
                if str(name).startswith("_"):
                    continue
                sample = conn.client.get(
                    f"/{name}/_all_docs",
                    params={"include_docs": "true", "limit": 100},
                )
                sample.raise_for_status()
                rows = sample.json().get("rows", [])
                fields: dict[str, str] = {}
                for row in rows:
                    document = row.get("doc") or {}
                    for key, value in document.items():
                        if str(key).startswith("_"):
                            continue
                        fields.setdefault(str(key), _couch_kind(value))
                indexes = []
                try:
                    index_payload = conn.client.get(f"/{name}/_index").json()
                    for item in index_payload.get("indexes", []):
                        definition = item.get("def") or {}
                        indexes.append(
                            Index(
                                name=str(item.get("name", "")),
                                columns=[
                                    str(field.get("name"))
                                    if isinstance(field, dict)
                                    else str(field)
                                    for field in definition.get("fields", [])
                                ],
                                unique=False,
                                kind="mango",
                                options={"type": item.get("type", "json")},
                            )
                        )
                except Exception:
                    pass
                collections.append(
                    CollectionSchema(
                        name=str(name),
                        fields=[
                            DocumentField(
                                path=key,
                                type=Type(kind=_universal_kind(kind)),
                                nullable=True,
                                frequency=1.0,
                            )
                            for key, kind in sorted(fields.items())
                        ],
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
            artifacts.append(
                DDLArtifact(
                    sql=json_safe({"create_database": collection.name}),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                if index.kind.lower() not in ("mango", "json"):
                    continue
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            {
                                "create_index": index.name,
                                "database": collection.name,
                                "body": {
                                    "index": {
                                        "fields": [
                                            {"name": column, "type": "asc"}
                                            for column in index.columns
                                        ]
                                    },
                                    "name": index.name,
                                    "type": "json",
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
        diagnostics = check_document_operation(query, _SUPPORTED, engine="CouchDB")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="CouchDB"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "$accumulator"},
                engine="CouchDB",
            )
        )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "CouchDB has no aggregation pipeline; use a map/reduce design document.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if query.operation in ("delete_one", "delete_many", "update_one", "replace_one") and not query.filter:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "CouchDB write-by-selector requires a selector; unfiltered writes are refused.",
                    operation=query.operation,
                    feature="unfiltered_write",
                )
            )
        if query.operation in ("find_one", "count") and not query.filter:
            # A whole-database count is allowed, but report the scan cost.
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.CAPABILITY,
                    "CouchDB count without a selector uses the database's doc_count.",
                    operation=query.operation,
                    feature="unfiltered",
                    severity="warning",
                )
            )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "couchdb", query, diagnostics)

        database = query.collection
        body: dict[str, Any] = {}
        if query.operation in ("find", "find_one"):
            body = {
                "selector": _to_mango(query.filter),
                "limit": int(query.limit or (1 if query.operation == "find_one" else 100)),
            }
            if query.projection:
                body["fields"] = [str(key) for key in query.projection if not str(key).startswith("_")]
            if query.sort:
                body["sort"] = [
                    {
                        str(item.get("field")): "desc"
                        if str(item.get("direction", 1)) in ("-1", "desc", "DESC")
                        else "asc"
                    }
                    for item in query.sort
                    if item.get("field")
                ]
        elif query.operation in ("insert_one", "insert_many"):
            documents = query.documents[:1] if query.operation == "insert_one" else list(query.documents)
            body = {"docs": documents}
        elif query.operation in ("update_one", "replace_one"):
            if not query.updates:
                raise AdapterError(AdapterErrorCode.SYNTAX, "CouchDB update requires a document body.")
            body = {
                "selector": _to_mango(query.filter),
                "update": query.updates,
                "upsert": bool(query.upsert),
            }
        elif query.operation in ("delete_one", "delete_many"):
            body = {"selector": _to_mango(query.filter)}

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="couchdb-request",
                text=json_safe({"database": database, "operation": query.operation, "body": body}),
                dialect="couchdb",
            ),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "doc", "type": "map", "nullable": True}],
            engine_plan={"kind": "couchdb", "database": database, "operation": query.operation, "body": json_safe(body)},
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
        database = native["database"]
        operation = native["operation"]
        body = native.get("body", {})
        try:
            if operation in ("find", "find_one"):
                response = conn.client.post(f"/{database}/_find", json=body)
                response.raise_for_status()
                documents = [normalize_document(item) for item in response.json().get("docs", [])]
                return DocumentResult(documents=documents, affected_count=None, warnings=[])
            if operation == "count":
                response = conn.client.get(f"/{database}")
                response.raise_for_status()
                return DocumentResult(documents=[], affected_count=int(response.json().get("doc_count", 0)), warnings=[])
            if operation in ("insert_one", "insert_many"):
                response = conn.client.post(f"/{database}/_bulk_docs", json=body)
                response.raise_for_status()
                results = response.json()
                errors = [item for item in results if isinstance(item, dict) and item.get("error")]
                if errors:
                    raise AdapterError(AdapterErrorCode.CONSTRAINT, "CouchDB bulk write rejected documents.")
                return DocumentResult(documents=[], affected_count=len(results), warnings=[])
            if operation in ("update_one", "replace_one"):
                response = conn.client.post(f"/{database}/_update", json=body)
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    raise AdapterError(AdapterErrorCode.CONSTRAINT, str(payload.get("error")))
                return DocumentResult(documents=[], affected_count=int(payload.get("updated", 0)), warnings=[])
            if operation in ("delete_one", "delete_many"):
                # CouchDB has no selector delete in the core API. Find matching
                # ids, then delete each with its current _rev.
                response = conn.client.post(f"/{database}/_find", json={"selector": body.get("selector", {})})
                response.raise_for_status()
                docs = response.json().get("docs", [])
                for item in docs:
                    conn.client.delete(
                        f"/{database}/{item['_id']}",
                        params={"rev": item["_rev"]},
                    )
                return DocumentResult(documents=[], affected_count=len(docs), warnings=[])
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported CouchDB operation {operation!r}.")

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "CouchDB has no multi-document transactions; use per-document _rev compare-and-set.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "CouchDB has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> CouchDBConnection:
        if not isinstance(connection, CouchDBConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for CouchDB.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        status = getattr(exc, "response", None)
        status_code = getattr(status, "status_code", None) if status is not None else None
        if status_code in (401, 403):
            code = AdapterErrorCode.AUTH
        elif status_code == 409:
            code = AdapterErrorCode.CONSTRAINT
        elif status_code == 404:
            code = AdapterErrorCode.CAPABILITY
        elif status_code in (400, 412):
            code = AdapterErrorCode.SYNTAX
        elif status_code in (429, 500, 502, 503):
            retryable = True
            if status_code == 429:
                code = AdapterErrorCode.TIMEOUT
        return AdapterError(code, f"CouchDB {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _to_mango(node: Any) -> dict[str, Any]:
    if not node:
        return {}
    if not isinstance(node, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "CouchDB selector must be an object.")
    if "and" in node and isinstance(node["and"], list):
        return {"$and": [_to_mango(item) for item in node["and"]]}
    if "or" in node and isinstance(node["or"], list):
        return {"$or": [_to_mango(item) for item in node["or"]]}
    if "not" in node:
        return {"$not": _to_mango(node["not"])}
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "CouchDB filter has no field.")
    if "eq" in node:
        return {str(field): node["eq"]}
    if "in" in node:
        return {str(field): {"$in": list(node["in"])}}
    if "exists" in node:
        return {str(field): {"$exists": bool(node["exists"])}}
    if "range" in node:
        return {str(field): dict(node["range"])}
    operator = str(node.get("operator") or "").upper()
    value = node.get("value", node.get("operand"))
    mapping = {"=": "$eq", "EQ": "$eq", "!=": "$ne", "<>": "$ne", "<": "$lt", "<=": "$lte", ">": "$gt", ">=": "$gte"}
    if operator not in mapping:
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"CouchDB selector operator {operator!r} is unsupported.")
    return {str(field): {mapping[operator]: value}}


def _couch_kind(value: Any) -> str:
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


def _universal_kind(kind: str) -> str:
    return {"integer": "integer", "double": "double", "boolean": "boolean", "list": "list", "map": "json"}.get(kind, "text")


def _value(value: Any) -> Any:
    if isinstance(value, (Decimal, date, datetime, time, bytes)):
        return value
    return value


COUCHDB_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "JSON number", notes="JSON has one number type; integers are exact up to 2^53 in JavaScript consumers."),
    "bigint": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON number", notes="Precision limited by JSON/JavaScript number semantics."),
    "float": TypeSupport(TypeSupportLevel.EXACT, "JSON number"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "JSON number"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON number or string", notes="No arbitrary-precision decimal."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "JSON string"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "JSON boolean"),
    "date": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string", notes="ISO-8601 string by convention."),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string"),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string"),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON number", notes="Seconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string", notes="_id is a string."),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string"),
    "json": TypeSupport(TypeSupportLevel.EXACT, "JSON object"),
    "binary": TypeSupport(TypeSupportLevel.APPROXIMATE, "Base64 string or attachment", notes="Attachments are separate blobs."),
    "geo": TypeSupport(TypeSupportLevel.APPROXIMATE, "GeoJSON object", notes="No native spatial index without a plugin."),
    "list": TypeSupport(TypeSupportLevel.EXACT, "JSON array"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON array", notes="No set uniqueness."),
    "map": TypeSupport(TypeSupportLevel.EXACT, "JSON object"),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native vector type."),
    "reference": TypeSupport(TypeSupportLevel.APPROXIMATE, "JSON string", notes="Application-level reference."),
}

COUCHDB_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No multi-document transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False, "Writes return per-document results, not a RETURNING clause."),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(True, "Mango partial_filter_selector."),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(True, "Documents are JSON."),
    CAP_ARRAY_TYPE: Capability(True, "JSON arrays."),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(False),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(True, "validate_doc_update functions, not declarative CHECK."),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(False),
    CAP_INDEXES: Capability(True, "Mango JSON indexes and search indexes when enabled."),
    CAP_FULL_TEXT_SEARCH: Capability(False, "Requires a Lucene/Clouseau search plugin."),
    CAP_MATERIALIZED_VIEWS: Capability(True, "Map/reduce views are incrementally maintained indexes."),
    CAP_PARTITIONING: Capability(True, "Partitioned databases."),
    CAP_REPLICATION: Capability(True, "Native multi-master replication."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(True, "Map/reduce views, not ad-hoc GROUP BY."),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "Bulk docs create or replace by _id/_rev."),
    CAP_TTL: Capability(False),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(True, "Mango selector DSL, not a full-text DSL."),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "JavaScript design documents; never generated from filters."),
    CAP_BATCH_WRITES: Capability(True, "_bulk_docs."),
    CAP_STRONG_CONSISTENCY: Capability(False, "Eventual consistency across replicas; quorum options are per-request."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Schema-on-read."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(True, "validate_doc_update, not JSON Schema."),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(True, "_changes feed."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
