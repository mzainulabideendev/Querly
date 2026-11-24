"""Aerospike adapter for Querly.

Aerospike is a distributed key-value store whose records live in namespaces and
sets. A collection is addressed as ``namespace.set``; a document is the record's
bin map. This adapter uses the native Aerospike client, preserves Aerospike's
typed bins, and creates secondary indexes with ``index_*`` calls.

Aerospike has no joins, aggregation pipeline, or multi-record transactions in
the community client. Non-key predicates are executed client-side over a
bounded scan and reported as such.

Driver: ``aerospike``, imported lazily.
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
    import aerospike
except ImportError:  # pragma: no cover
    aerospike = None

__all__ = ["AerospikeAdapter", "AerospikeConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "get",
    "scan",
    "insert_one",
    "insert_many",
    "put",
    "upsert",
    "update_one",
    "delete_one",
    "delete_many",
    "count",
}
_WRITES = {"insert_one", "insert_many", "put", "upsert", "update_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class AerospikeConnection(Connection):
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
            self._client.info("build")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True


class AerospikeAdapter(DocumentAdapter):
    data_model = "key-value"
    ir_subset = ("schema", "document CRUD", "scan", "count")

    @property
    def id(self) -> str:
        return "aerospike"

    @property
    def display_name(self) -> str:
        return "Aerospike"

    @property
    def package_version(self) -> str:
        from oq_adapters.aerospike import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.aerospike import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Aerospike",
            tested_versions=[],
            capabilities=AEROSPIKE_CAPS,
            types=AEROSPIKE_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "A collection is namespace.set; the namespace must be configured on the server.",
                "No joins, aggregation pipeline, or multi-record transactions in the community client.",
                "Non-key predicates are evaluated client-side over a bounded scan.",
                "Expressions and UDFs exist but are not generated from user filters.",
                "Secondary indexes are created per bin and are eventually populated.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "aerospike":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected an aerospike config.")
        if aerospike is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "aerospike is not installed. Install oq-adapter-aerospike[driver].",
            )
        hosts = config.options.get("hosts") or [
            (config.host or "127.0.0.1", config.port or 3000)
        ]
        client_config: dict[str, Any] = {
            "hosts": hosts,
            "policies": {
                "timeout": int(config.options.get("timeout_ms", "5000")),
                "total_timeout": int(config.options.get("total_timeout_ms", "10000")),
            },
        }
        if config.user:
            client_config["user"] = config.user
            client_config["password"] = secrets.get("password") or ""
        if config.options.get("tls"):
            client_config["tls"] = {
                "ca_file": config.sslrootcert,
                "cert_file": config.sslcert,
                "key_file": config.sslkey,
            }
        try:
            client = aerospike.client(client_config)
            client.connect()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return AerospikeConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            payload = conn.client.info("build")
            if isinstance(payload, dict):
                raw = str(next(iter(payload.values())))
            else:
                raw = str(payload)
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="Aerospike",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, AerospikeConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            namespaces = conn.client.info("namespaces")
            raw = namespaces if isinstance(namespaces, str) else str(namespaces)
            names = [item for item in raw.replace(";", ",").split(",") if item]
            collections = []
            for namespace in names:
                if namespace.startswith("namespace"):
                    continue
                try:
                    scan = conn.client.scan(namespace, None)
                    fields: dict[str, str] = {}
                    count = [0]

                    def collect(record: Any) -> None:
                        if count[0] >= 100:
                            return False
                        key, meta, bins = record
                        for name, value in bins.items():
                            fields.setdefault(str(name), _aerospike_kind(value))
                        count[0] += 1
                        return True

                    scan.foreach(collect)
                except Exception:
                    fields = {}
                collections.append(
                    CollectionSchema(
                        name=namespace,
                        fields=[
                            DocumentField(
                                path=name,
                                type=Type(kind=_universal(kind)),
                                nullable=False,
                                frequency=1.0,
                            )
                            for name, kind in sorted(fields.items())
                        ],
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
            namespace, _, set_name = collection.name.partition(".")
            artifacts.append(
                DDLArtifact(
                    sql=json_safe({"namespace": namespace, "set": set_name, "note": "Aerospike sets are implicit"}),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            {
                                "create_index": index.name,
                                "namespace": namespace,
                                "set": set_name,
                                "bin": index.columns[0] if index.columns else None,
                                "kind": index.kind,
                            }
                        ),
                        statement_type="create_index",
                        entity=collection.name,
                    )
                )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Aerospike")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="Aerospike"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "udf"},
                engine="Aerospike",
            )
        )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Aerospike has no aggregation pipeline.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "aerospike", query, diagnostics)

        namespace, _, set_name = query.collection.partition(".")
        key = _record_key(query.filter)
        operation = query.operation
        plan: dict[str, Any] = {
            "namespace": namespace,
            "set": set_name,
            "operation": operation,
            "key": key,
        }
        if operation in ("find", "find_one", "scan", "count"):
            plan["limit"] = int(query.limit or (1 if operation == "find_one" else 100))
            if key:
                plan["operation"] = "get"
            else:
                plan["client_filter"] = query.filter
                diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        "Aerospike non-key predicates are evaluated client-side over a bounded scan.",
                        operation=operation,
                        feature="client_side_filter",
                        severity="warning",
                    )
                )
        elif operation in ("insert_one", "insert_many", "put", "upsert", "update_one"):
            documents = (
                query.documents[:1]
                if operation in ("insert_one", "put")
                else (list(query.documents) if operation == "insert_many" else [query.updates or {}])
            )
            rows = []
            for document in documents:
                key_value = str(document.get("_id") or document.get("_key") or document.get("key") or "")
                if not key_value:
                    raise AdapterError(AdapterErrorCode.SYNTAX, "Aerospike writes require _id or _key.")
                bins = {
                    str(name): value
                    for name, value in document.items()
                    if name not in ("_id", "_key", "key")
                }
                rows.append({"key": key_value, "bins": bins})
            plan["rows"] = rows
        elif operation in ("delete_one", "delete_many"):
            if not key:
                raise AdapterError(AdapterErrorCode.SYNTAX, "Aerospike delete requires a record key.")
            plan["keys"] = [key]
        elif operation == "get":
            if not key:
                raise AdapterError(AdapterErrorCode.SYNTAX, "Aerospike get requires a record key.")
            plan["operation"] = "get"

        return PlanDocument(
            target_adapter=self.id,
            operation=operation,
            artifact=Artifact(kind="aerospike-plan", text=json_safe(plan), dialect="aerospike"),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "record", "type": "map", "nullable": True}],
            engine_plan={"kind": "aerospike", **json_safe(plan)},
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
        namespace = native["namespace"]
        set_name = native.get("set") or None
        operation = native["operation"]
        try:
            if operation == "get":
                key = (namespace, set_name, str(native["key"]))
                _, _, bins = conn.client.get(key)
                return DocumentResult(
                    documents=[normalize_document({"_id": native["key"], **bins})],
                    affected_count=1,
                    warnings=[],
                )
            if operation in ("find", "scan"):
                limit = int(native.get("limit", 100))
                documents: list[dict[str, Any]] = []

                def collect(record: Any) -> bool:
                    key, meta, bins = record
                    document = {"_id": key[2] if key and len(key) > 2 else None, **bins}
                    if native.get("client_filter") and not _matches(document, native["client_filter"]):
                        return True
                    documents.append(document)
                    return len(documents) < limit

                conn.client.scan(namespace, set_name).foreach(collect)
                return DocumentResult(documents=[normalize_document(item) for item in documents], warnings=[])
            if operation == "count":
                count = [0]

                def count_record(record: Any) -> bool:
                    if native.get("client_filter") and not _matches(
                        {"_id": record[0][2], **record[2]}, native["client_filter"]
                    ):
                        return True
                    count[0] += 1
                    return count[0] < int(native.get("limit", 100))

                conn.client.scan(namespace, set_name).foreach(count_record)
                return DocumentResult(documents=[], affected_count=count[0], warnings=["Aerospike count is a bounded scan."])
            if operation in ("insert_one", "insert_many", "put", "upsert", "update_one"):
                write_policy = {"ttl": int(native.get("ttl", 0))}
                for row in native.get("rows", []):
                    key = (namespace, set_name, str(row["key"]))
                    conn.client.put(key, row.get("bins", {}), meta=write_policy)
                return DocumentResult(documents=[], affected_count=len(native.get("rows", [])), warnings=[])
            if operation in ("delete_one", "delete_many"):
                for key_value in native.get("keys", []):
                    conn.client.remove((namespace, set_name, str(key_value)))
                return DocumentResult(documents=[], affected_count=len(native.get("keys", [])), warnings=[])
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported Aerospike operation {operation!r}.")

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Aerospike community client has no multi-record transactions; single-record operations are atomic.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "Aerospike has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> AerospikeConnection:
        if not isinstance(connection, AerospikeConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Aerospike.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        name = type(exc).__name__
        if "Auth" in name or "Security" in name:
            code = AdapterErrorCode.AUTH
        elif "Index" in name or "Admin" in name:
            code = AdapterErrorCode.SYNTAX
        elif "Timeout" in name or "Unavailable" in name or "DeviceOverload" in name:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        return AdapterError(code, f"Aerospike {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _record_key(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    for name in ("_id", "_key", "key"):
        if isinstance(node.get(name), str):
            return node[name]
        if node.get("field") == name and isinstance(node.get("eq"), str):
            return node["eq"]
    if isinstance(node.get("and"), list):
        for item in node["and"]:
            found = _record_key(item)
            if found:
                return found
    return None


def _aerospike_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "double"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "text"


def _universal(kind: str) -> str:
    return {"boolean": "boolean", "integer": "integer", "double": "double", "binary": "binary", "list": "list", "map": "map"}.get(kind, "text")


def _matches(document: dict[str, Any], node: Any) -> bool:
    if not isinstance(node, dict):
        return True
    if isinstance(node.get("and"), list):
        return all(_matches(document, item) for item in node["and"])
    if isinstance(node.get("or"), list):
        return any(_matches(document, item) for item in node["or"])
    field = node.get("field") or node.get("name")
    if not field:
        return True
    value = document.get(str(field))
    if "eq" in node:
        return value == node["eq"]
    operator = str(node.get("operator") or "").upper()
    operand = node.get("value", node.get("operand"))
    try:
        return {
            "=": value == operand,
            "EQ": value == operand,
            "!=": value != operand,
            "<>": value != operand,
            "<": value < operand,
            "<=": value <= operand,
            ">": value > operand,
            ">=": value >= operand,
        }.get(operator, False)
    except TypeError:
        return False


AEROSPIKE_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "integer", notes="64-bit signed."),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "integer"),
    "float": TypeSupport(TypeSupportLevel.COMPATIBLE, "double", notes="Aerospike has one floating-point type."),
    "double": TypeSupport(TypeSupportLevel.EXACT, "double"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "string/double", notes="No decimal bin type; store as string or double."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "string"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "boolean"),
    "date": TypeSupport(TypeSupportLevel.APPROXIMATE, "integer/string", notes="No date type; epoch or ISO string by convention."),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "integer/string"),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "integer", notes="Epoch seconds by convention."),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "integer"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "integer"),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "string"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "string"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "string"),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "map/list", notes="CDT map/list bins."),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "bytes"),
    "geo": TypeSupport(TypeSupportLevel.COMPATIBLE, "GeoJSON", notes="Aerospike geospatial index support."),
    "list": TypeSupport(TypeSupportLevel.EXACT, "list"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "list", notes="No set type."),
    "map": TypeSupport(TypeSupportLevel.EXACT, "map"),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native vector type; recent vector search is a separate feature not exposed here."),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No foreign keys."),
}

AEROSPIKE_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "Single-record atomicity; no multi-record transactions in the community client."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(True, "CDT map/list bins."),
    CAP_ARRAY_TYPE: Capability(True, "List bins."),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(True, "GeoJSON bins and geo2dsphere indexes."),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(False),
    CAP_INDEXES: Capability(True, "Secondary indexes on bins."),
    CAP_FULL_TEXT_SEARCH: Capability(False),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Namespaces partition data across nodes."),
    CAP_REPLICATION: Capability(True),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(False),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True),
    CAP_TTL: Capability(True, "Per-record TTL."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "Lua UDFs; never generated from filters."),
    CAP_BATCH_WRITES: Capability(True),
    CAP_STRONG_CONSISTENCY: Capability(True, "Per-record consistency, configurable."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Schemaless bins."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(True, "XDR/CDC connectors; not a built-in change feed."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
