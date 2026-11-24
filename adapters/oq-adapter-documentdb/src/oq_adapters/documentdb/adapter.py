"""Amazon DocumentDB adapter for Querly.

Amazon DocumentDB speaks the MongoDB wire protocol but is not MongoDB: it
requires TLS, has no multi-document transactions, does not execute JavaScript
(``$where``, ``$function``, ``$accumulator``), and supports a version-dependent
aggregation subset. This adapter therefore implements its own connection policy
and validation instead of reusing the MongoDB adapter, and it reports
DocumentDB-specific capabilities.

Driver: ``pymongo`` (optional extra ``driver``), imported lazily.
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
    from bson import ObjectId
    from pymongo import MongoClient
    from pymongo.errors import (
        BulkWriteError,
        ConnectionFailure,
        DuplicateKeyError,
        OperationFailure,
        PyMongoError,
    )
except ImportError:  # pragma: no cover
    ObjectId = None
    MongoClient = None
    PyMongoError = Exception
    ConnectionFailure = OperationFailure = DuplicateKeyError = BulkWriteError = Exception

__all__ = ["DocumentDBAdapter", "DocumentDBConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "count",
    "distinct",
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "delete_one",
    "delete_many",
    "aggregate",
}
_WRITES = {"insert_one", "insert_many", "update_one", "update_many", "replace_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}

# DocumentDB does not execute JavaScript and does not expose mapReduce.
_FORBIDDEN_OPERATORS = {"$where", "$function", "$accumulator", "$expr"}
_ALLOWED_STAGES = {
    "$match",
    "$group",
    "$sort",
    "$limit",
    "$skip",
    "$project",
    "$unwind",
    "$count",
    "$addFields",
    "$set",
    "$unset",
    "$replaceRoot",
    "$replaceWith",
    "$facet",
    "$sample",
    "$lookup",
}


class DocumentDBConnection(Connection):
    def __init__(self, config: ConnectionConfig, client: Any, database: Any) -> None:
        self._config = config
        self._client = client
        self._database = database
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
    def database(self) -> Any:
        return self._database

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True


class DocumentDBAdapter(DocumentAdapter):
    data_model = "document"
    ir_subset = ("schema", "document CRUD", "aggregate", "count", "distinct")

    @property
    def id(self) -> str:
        return "documentdb"

    @property
    def display_name(self) -> str:
        return "Amazon DocumentDB"

    @property
    def package_version(self) -> str:
        from oq_adapters.documentdb import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.documentdb import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Amazon DocumentDB",
            tested_versions=[],
            capabilities=DOCUMENTDB_CAPS,
            types=DOCUMENTDB_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "No multi-document transactions; each write is atomic at the document level.",
                "No server-side JavaScript: $where, $function, $accumulator, and $expr are rejected.",
                "Aggregation support is a version-dependent subset of MongoDB's; $out/$merge are not supported.",
                "Change streams require a DocumentDB version that exposes them; not assumed here.",
                "TLS with the Amazon RDS global bundle is required by DocumentDB endpoints.",
                "The wire protocol is MongoDB-compatible but version numbers are not MongoDB versions.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "documentdb":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a documentdb config.")
        if MongoClient is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "pymongo is not installed. Install oq-adapter-documentdb[driver].",
            )
        if not config.host:
            raise AdapterError(AdapterErrorCode.CONNECTION, "DocumentDB requires a cluster endpoint.")
        if not config.database:
            raise AdapterError(AdapterErrorCode.CONNECTION, "DocumentDB requires a database name.")
        options: dict[str, Any] = {
            "host": config.host,
            "port": config.port or 27017,
            "tls": True,
            "retryWrites": bool(config.options.get("retry_writes", True)),
            "retryReads": bool(config.options.get("retry_reads", True)),
            "appname": "querly-documentdb",
            "serverSelectionTimeoutMS": int(config.options.get("timeout_ms", "10000")),
        }
        ca_file = config.sslrootcert or config.options.get("tls_ca_file")
        if ca_file:
            options["tlsCAFile"] = ca_file
        if config.options.get("tls_allow_invalid_certificates") is True:
            options["tlsAllowInvalidCertificates"] = True
        if config.user:
            options["username"] = config.user
            options["password"] = secrets.get("password") or ""
            options["authSource"] = config.options.get("auth_source", "admin")
            options["authMechanism"] = config.options.get("auth_mechanism", "SCRAM-SHA-1")
        try:
            client = MongoClient(**options)
            client.admin.command("ping")
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return DocumentDBConnection(config, client, client[str(config.database)])

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            info = conn.client.server_info()
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        raw = str(info.get("version", "unknown"))
        parts = raw.split(".")
        return EngineVersion(
            product="Amazon DocumentDB",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, DocumentDBConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            names = conn.database.list_collection_names()
            collections = []
            for name in sorted(names):
                if str(name).startswith("system."):
                    continue
                sample = list(conn.database[name].find({}, limit=100))
                stats = _collect_paths(sample)
                indexes = []
                for info in conn.database[name].list_indexes():
                    keys = list(info.get("key", {}).items())
                    indexes.append(
                        Index(
                            name=str(info.get("name", "")),
                            columns=[str(key) for key, _ in keys],
                            unique=bool(info.get("unique", False)),
                            kind="documentdb-index",
                            keys=[
                                {
                                    "field": str(key),
                                    "direction": int(value)
                                    if isinstance(value, (int, float))
                                    else str(value),
                                }
                                for key, value in keys
                            ],
                        )
                    )
                collections.append(
                    CollectionSchema(
                        name=str(name),
                        fields=[
                            DocumentField(
                                path=path,
                                type=Type(kind=_universal(kinds)),
                                nullable=True,
                                frequency=round(min(1.0, len(kinds) / max(1, len(sample))), 3)
                                if sample
                                else 0.0,
                            )
                            for path, kinds in sorted(stats.items())
                        ],
                        document_count=conn.database[name].estimated_document_count(),
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
                    sql=json_safe({"create_collection": collection.name}),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                keys = {
                    str(item["field"]): int(item.get("direction", 1))
                    for item in (index.keys or [{"field": column} for column in index.columns])
                }
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            {
                                "create_index": index.name,
                                "collection": collection.name,
                                "keys": keys,
                                "unique": index.unique,
                            }
                        ),
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="DocumentDB")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="DocumentDB"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter, forbidden=_FORBIDDEN_OPERATORS, engine="DocumentDB"
            )
        )
        if query.operation == "aggregate":
            if not query.pipeline:
                diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.SYNTAX,
                        "DocumentDB aggregate requires at least one pipeline stage.",
                        operation=query.operation,
                        feature="pipeline",
                    )
                )
            for stage in query.pipeline:
                if stage.operator in ("$out", "$merge"):
                    diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.UNSUPPORTED,
                            "DocumentDB does not support $out/$merge as an aggregation sink.",
                            operation=query.operation,
                            feature=stage.operator,
                        )
                    )
                elif stage.operator not in _ALLOWED_STAGES:
                    diagnostics.append(
                        diagnostic(
                            AdapterErrorCode.UNSUPPORTED,
                            f"DocumentDB aggregation stage {stage.operator!r} is not in the supported subset.",
                            operation=query.operation,
                            feature=stage.operator,
                        )
                    )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "documentdb", query, diagnostics)

        if query.operation == "aggregate":
            body: dict[str, Any] = {"pipeline": [sanitize_stage(stage) for stage in query.pipeline]}
        elif query.operation in ("insert_one", "insert_many"):
            body = {"documents": [sanitize_document(item) for item in query.documents]}
        elif query.operation in ("update_one", "update_many", "replace_one"):
            body = {
                "filter": sanitize_document(query.filter or {}),
                "update": sanitize_document(query.updates or {}),
                "upsert": bool(query.upsert),
            }
        elif query.operation in ("delete_one", "delete_many"):
            body = {"filter": sanitize_document(query.filter or {})}
        else:
            body = {"filter": sanitize_document(query.filter or {})}
            if query.projection:
                body["projection"] = dict(query.projection)
            if query.sort:
                body["sort"] = [
                    {
                        str(item.get("field")): -1
                        if str(item.get("direction", 1)) in ("-1", "desc", "DESC")
                        else 1
                    }
                    for item in query.sort
                    if item.get("field")
                ]
            if query.skip:
                body["skip"] = int(query.skip)
            if query.limit:
                body["limit"] = int(query.limit)
            if query.operation == "find_one":
                body["limit"] = 1
            if query.distinct_field:
                body["distinct"] = query.distinct_field

        required = [CAP_AGGREGATION_FRAMEWORK] if query.operation == "aggregate" else []
        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="documentdb-command",
                text=json_safe({"collection": query.collection, "operation": query.operation, "body": body}),
                dialect="documentdb",
            ),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            required_capabilities=required,
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "document", "type": "map", "nullable": True}],
            engine_plan={
                "kind": "documentdb",
                "collection": query.collection,
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
        collection = conn.database[native["collection"]]
        operation = native["operation"]
        body = native.get("body", {})
        try:
            if operation in ("find", "find_one"):
                cursor = collection.find(
                    body.get("filter", {}),
                    projection=body.get("projection"),
                    sort=body.get("sort"),
                    skip=body.get("skip"),
                    limit=body.get("limit"),
                )
                return DocumentResult(
                    documents=[normalize_document(_decode(item)) for item in cursor],
                    affected_count=None,
                    warnings=[],
                )
            if operation == "count":
                return DocumentResult(
                    documents=[],
                    affected_count=collection.count_documents(body.get("filter", {})),
                    warnings=["DocumentDB count_documents is an exact collection scan unless indexed."],
                )
            if operation == "distinct":
                values = collection.distinct(str(body.get("distinct")), body.get("filter", {}))
                return DocumentResult(
                    documents=[{"value": normalize_document(_decode(item))} for item in values],
                    affected_count=len(values),
                    warnings=[],
                )
            if operation == "aggregate":
                pipeline = [_revive_stage(stage) for stage in body.get("pipeline", [])]
                cursor = collection.aggregate(pipeline, allowDiskUse=True)
                return DocumentResult(
                    documents=[normalize_document(_decode(item)) for item in cursor],
                    affected_count=None,
                    warnings=[],
                )
            if operation == "insert_one":
                document = body.get("documents", [{}])[0]
                result = collection.insert_one(_revive(document))
                return DocumentResult(
                    documents=[{"_id": str(result.inserted_id)}],
                    affected_count=1,
                    warnings=[],
                )
            if operation == "insert_many":
                result = collection.insert_many([_revive(item) for item in body.get("documents", [])])
                return DocumentResult(
                    documents=[{"_id": str(item)} for item in result.inserted_ids],
                    affected_count=len(result.inserted_ids),
                    warnings=[],
                )
            if operation in ("update_one", "update_many"):
                method = collection.update_one if operation == "update_one" else collection.update_many
                result = method(
                    body.get("filter", {}),
                    _revive(body.get("update", {})),
                    upsert=bool(body.get("upsert", False)),
                )
                return DocumentResult(
                    documents=[],
                    affected_count=int(result.modified_count),
                    warnings=[],
                )
            if operation == "replace_one":
                result = collection.replace_one(
                    body.get("filter", {}),
                    _revive(body.get("update", {})),
                    upsert=bool(body.get("upsert", False)),
                )
                return DocumentResult(documents=[], affected_count=int(result.modified_count), warnings=[])
            if operation in ("delete_one", "delete_many"):
                method = collection.delete_one if operation == "delete_one" else collection.delete_many
                result = method(body.get("filter", {}))
                return DocumentResult(documents=[], affected_count=int(result.deleted_count), warnings=[])
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported DocumentDB operation {operation!r}.")

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Amazon DocumentDB has no multi-document transactions; document writes are atomic individually.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "DocumentDB has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> DocumentDBConnection:
        if not isinstance(connection, DocumentDBConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for DocumentDB.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if isinstance(exc, DuplicateKeyError):
            code = AdapterErrorCode.CONSTRAINT
        elif isinstance(exc, OperationFailure):
            code = AdapterErrorCode.SYNTAX
            if "Authentication" in text or "auth" in text.lower():
                code = AdapterErrorCode.AUTH
        elif isinstance(exc, ConnectionFailure):
            retryable = True
        elif isinstance(exc, PyMongoError):
            retryable = True
        return AdapterError(code, f"DocumentDB {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _is_object_id(value: Any) -> bool:
    return ObjectId is not None and isinstance(value, ObjectId)


def sanitize_document(value: Any) -> Any:
    """Convert driver-only values into deterministic JSON-safe literals."""

    if _is_object_id(value):
        return {"$oid": str(value)}
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, dict):
        return {str(key): sanitize_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_document(item) for item in value]
    return value


def sanitize_stage(stage: Any) -> dict[str, Any]:
    operator = str(getattr(stage, "operator", ""))
    value = getattr(stage, "value", {})
    return {operator: sanitize_document(value)}


def _revive(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$oid"}:
            if ObjectId is None:
                return str(value["$oid"])
            return ObjectId(str(value["$oid"]))
        if set(value) == {"$decimal"}:
            return Decimal(str(value["$decimal"]))
        if set(value) == {"$bytes"}:
            return bytes.fromhex(str(value["$bytes"]))
        return {key: _revive(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_revive(item) for item in value]
    return value


def _revive_stage(stage: Any) -> dict[str, Any]:
    if not isinstance(stage, dict):
        return stage
    return {key: _revive(item) for key, item in stage.items()}


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if _is_object_id(value):
        return str(value)
    return value


def _collect_paths(documents: list[dict[str, Any]]) -> dict[str, set[str]]:
    stats: dict[str, set[str]] = {}
    for document in documents:
        _walk(document, "", stats)
    return stats


def _walk(value: Any, prefix: str, stats: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            stats.setdefault(path, set()).add(_kind(item))
            _walk(item, path, stats)
    elif isinstance(value, list) and value:
        _walk(value[0], prefix, stats)


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (datetime, date, time)):
        return "timestamp"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "text"


def _universal(kinds: set[str]) -> str:
    if not kinds:
        return "text"
    if len(kinds) == 1:
        return _kind_to_universal(next(iter(kinds)))
    for candidate in ("map", "list", "text", "double", "binary", "timestamp"):
        if candidate in kinds:
            return _kind_to_universal(candidate)
    return "text"


def _kind_to_universal(kind: str) -> str:
    return {
        "boolean": "boolean",
        "integer": "integer",
        "double": "double",
        "text": "text",
        "timestamp": "timestamp",
        "binary": "binary",
        "list": "list",
        "map": "json",
        "null": "text",
    }.get(kind, "text")


DOCUMENTDB_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "int/long", notes="32-bit and 64-bit integers."),
    "bigint": TypeSupport(TypeSupportLevel.COMPATIBLE, "long"),
    "float": TypeSupport(TypeSupportLevel.COMPATIBLE, "double"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "double"),
    "decimal": TypeSupport(TypeSupportLevel.COMPATIBLE, "decimal128"),
    "text": TypeSupport(TypeSupportLevel.EXACT, "string"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "bool"),
    "date": TypeSupport(TypeSupportLevel.COMPATIBLE, "date"),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "date"),
    "timestamp": TypeSupport(TypeSupportLevel.COMPATIBLE, "date"),
    "datetime": TypeSupport(TypeSupportLevel.COMPATIBLE, "date"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "long", notes="Milliseconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "string/binary"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "string"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "string"),
    "json": TypeSupport(TypeSupportLevel.EXACT, "object"),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "binData"),
    "geo": TypeSupport(TypeSupportLevel.COMPATIBLE, "GeoJSON", notes="2dsphere indexes are supported."),
    "list": TypeSupport(TypeSupportLevel.EXACT, "array"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "array", notes="No set uniqueness."),
    "map": TypeSupport(TypeSupportLevel.EXACT, "object"),
    "vector": TypeSupport(
        TypeSupportLevel.UNSUPPORTED,
        None,
        notes="No native vector index; DocumentDB has no Atlas Vector Search equivalent.",
    ),
    "reference": TypeSupport(TypeSupportLevel.APPROXIMATE, "DBRef/objectId", notes="No foreign keys."),
}

DOCUMENTDB_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No multi-document transactions."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(True, "Writes return ids; reads return documents."),
    CAP_ORDER_BY_NULLS: Capability(True, "Sort semantics follow BSON ordering."),
    CAP_PARTIAL_INDEXES: Capability(True, "partialFilterExpression."),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(True),
    CAP_ARRAY_TYPE: Capability(True),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(True),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False, "decimal128 has 34 significant digits."),
    CAP_CHECK_CONSTRAINTS: Capability(True, "JSON Schema validators, not SQL CHECK."),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(True, "Unique indexes, including TTL-compatible ones."),
    CAP_INDEXES: Capability(True, "B-tree, compound, geospatial, partial, TTL."),
    CAP_FULL_TEXT_SEARCH: Capability(False, "No text index or Atlas Search."),
    CAP_MATERIALIZED_VIEWS: Capability(True, "On-demand materialized views via $out to a collection."),
    CAP_PARTITIONING: Capability(True, "Sharding depends on cluster configuration."),
    CAP_REPLICATION: Capability(True, "Managed six-way replication across three AZs."),
    CAP_JOINS: Capability(True, "$lookup is supported with limitations; no $graphLookup."),
    CAP_GROUP_BY: Capability(True, "$group."),
    CAP_HAVING: Capability(True, "$match after $group."),
    CAP_SUBQUERIES: Capability(False, "No $lookup subqueries in expressions."),
    CAP_UNION: Capability(True, "$unionWith is version-dependent; not assumed."),
    CAP_WINDOW_FUNCTIONS: Capability(True, "$setWindowFields is version-dependent."),
    CAP_UPSERT: Capability(True),
    CAP_TTL: Capability(True, "TTL indexes."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False, "JavaScript is not executed by DocumentDB."),
    CAP_BATCH_WRITES: Capability(True, "insertMany/bulkWrite."),
    CAP_STRONG_CONSISTENCY: Capability(False, "Read concern is not configurable beyond defaults."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Schema-on-read with optional validators."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(True, "Version-dependent aggregation subset."),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(True, "JSON Schema validators."),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(True, "Available on supported DocumentDB versions."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
