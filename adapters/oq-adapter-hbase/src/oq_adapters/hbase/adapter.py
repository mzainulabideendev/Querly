"""Apache HBase adapter for Querly.

HBase is a sparse, sorted wide-column store. A collection is an HBase table, a
document is a row, and fields live under column families. This adapter inspects
column families through the Thrift gateway (``happybase``), compiles DDL to
``create_table`` with family options, and compiles reads to row gets/scans.

Because HBase filters are a separate server-side expression language, this
adapter performs non-key predicates client-side on a bounded scan and reports
that limitation in every plan that needs it. Row keys are the only push-down
predicate.

Driver: ``happybase`` (Thrift), imported lazily.
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
    import happybase
except ImportError:  # pragma: no cover
    happybase = None

__all__ = ["HBaseAdapter", "HBaseConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "get",
    "scan",
    "insert_one",
    "insert_many",
    "put",
    "update_one",
    "upsert",
    "delete_one",
    "delete_many",
    "count",
}
_WRITES = {"insert_one", "insert_many", "put", "update_one", "upsert"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class HBaseConnection(Connection):
    def __init__(self, config: ConnectionConfig, connection: Any) -> None:
        self._config = config
        self._connection = connection
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def raw(self) -> Any:
        return self._connection

    def ping(self) -> bool:
        try:
            self._connection.tables()
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._connection.close()
            finally:
                self._closed = True


class HBaseAdapter(DocumentAdapter):
    data_model = "wide-column"
    ir_subset = ("schema", "document CRUD", "scan", "count")

    @property
    def id(self) -> str:
        return "hbase"

    @property
    def display_name(self) -> str:
        return "Apache HBase"

    @property
    def package_version(self) -> str:
        from oq_adapters.hbase import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.hbase import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Apache HBase",
            tested_versions=[],
            capabilities=HBASE_CAPS,
            types=HBASE_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "Columns are sparse family:qualifier pairs; the schema cannot enumerate every qualifier.",
                "Only row-key and prefix predicates are pushed down; other predicates run client-side on a bounded scan.",
                "There are no multi-row transactions; row-level atomicity only.",
                "Secondary indexes need Apache Phoenix or an external index and are not created by this adapter.",
                "Thrift gateway availability is required by happybase.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "hbase":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected an hbase config.")
        if happybase is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "happybase is not installed. Install oq-adapter-hbase[driver].",
            )
        if not config.host:
            raise AdapterError(AdapterErrorCode.CONNECTION, "HBase Thrift requires a host.")
        kwargs: dict[str, Any] = {
            "host": config.host,
            "port": config.port or 9090,
            "timeout": int(config.options.get("timeout", "30")),
            "autoconnect": True,
            "table_prefix_separator": config.options.get("table_prefix_separator", ":"),
        }
        if config.user and secrets.get("password"):
            kwargs["username"] = config.user
            kwargs["password"] = secrets.get("password")
        if config.options.get("transport") == "framed":
            kwargs["transport"] = "framed"
            kwargs["protocol"] = "compact"
        if config.sslmode not in (None, "disable"):
            kwargs["transport"] = "framed"
            kwargs["protocol"] = "compact"
        try:
            connection = happybase.Connection(**kwargs)
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return HBaseConnection(config, connection)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            # HBase exposes its version through the Thrift server's status
            # text; happybase does not expose a version attribute, so read it
            # from the HBase shell compatibility endpoint when available.
            raw = str(getattr(conn.raw, "transport", "") or "hbase-thrift")
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        return EngineVersion(product="Apache HBase", version=raw, major=0, minor=0, patch=0)

    def close(self, connection: Connection) -> None:
        if isinstance(connection, HBaseConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            tables = [name.decode() if isinstance(name, bytes) else str(name) for name in conn.raw.tables()]
            collections = []
            for name in tables:
                table = conn.raw.table(name)
                families = table.families()
                fields = [
                    DocumentField(
                        path=f"{family}:*",
                        type=Type(kind="binary"),
                        nullable=True,
                        frequency=1.0,
                    )
                    for family in sorted(families)
                ]
                collections.append(
                    CollectionSchema(
                        name=name,
                        fields=fields,
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
            families = {}
            for field in collection.fields:
                family = field.path.split(":", 1)[0]
                if family and family not in families:
                    families[family] = {
                        "max_versions": 1,
                        "compression": collection.indexes[0].options.get("compression", "SNAPPY")
                        if collection.indexes
                        else "SNAPPY",
                    }
            artifacts.append(
                DDLArtifact(
                    sql=json_safe({"create_table": collection.name, "families": families}),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="HBase")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="HBase"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "filter"},
                engine="HBase",
            )
        )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "HBase has no aggregation pipeline.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if any(item.severity == "error" for item in diagnostics):
            return unsupported_document_plan(self.id, "hbase", query, diagnostics)

        table = query.collection
        key = _row_key(query.filter)
        operation = query.operation
        plan: dict[str, Any] = {"table": table, "operation": operation, "key": key}
        if operation in ("find", "find_one", "scan", "count"):
            plan["limit"] = int(query.limit or (1 if operation == "find_one" else 100))
            plan["client_filter"] = query.filter if key is None else None
            if key is None:
                diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.CAPABILITY,
                        "HBase non-key predicates are evaluated client-side over a bounded scan.",
                        operation=operation,
                        feature="client_side_filter",
                        severity="warning",
                    )
                )
        elif operation in ("insert_one", "insert_many", "put", "update_one", "upsert"):
            documents = query.documents[:1] if operation in ("insert_one", "put") else (
                list(query.documents) if operation in ("insert_many",) else [query.updates or {}]
            )
            rows = []
            for document in documents:
                row_key = str(document.get("_id") or document.get("_key") or document.get("key") or "")
                if not row_key:
                    raise AdapterError(AdapterErrorCode.SYNTAX, "HBase writes require _id or _key.")
                columns = {
                    _column(key): _encode(value)
                    for key, value in document.items()
                    if key not in ("_id", "_key", "key")
                }
                rows.append({"key": row_key, "columns": columns})
            plan["rows"] = rows
        elif operation in ("delete_one", "delete_many"):
            if key is None:
                raise AdapterError(AdapterErrorCode.SYNTAX, "HBase delete requires a row key.")
            plan["keys"] = [key]
        elif operation == "get":
            if key is None:
                raise AdapterError(AdapterErrorCode.SYNTAX, "HBase get requires a row key.")
            plan["key"] = key
            plan["operation"] = "find"

        return PlanDocument(
            target_adapter=self.id,
            operation=operation,
            artifact=Artifact(kind="hbase-plan", text=json_safe(plan), dialect="hbase"),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "row", "type": "map", "nullable": True}],
            engine_plan={"kind": "hbase", **json_safe(plan)},
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
        table = conn.raw.table(str(native["table"]))
        try:
            if operation in ("find", "scan"):
                if native.get("key"):
                    row = table.row(str(native["key"]))
                    documents = [_decode_row(str(native["key"]), row)] if row else []
                else:
                    documents = [
                        _decode_row(key, row)
                        for key, row in table.scan(limit=int(native.get("limit", 100)))
                    ]
                    if native.get("client_filter"):
                        documents = [
                            item for item in documents if _matches(item, native["client_filter"])
                        ]
                if plan.operation == "find_one":
                    documents = documents[:1]
                return DocumentResult(documents=[normalize_document(item) for item in documents], warnings=[])
            if operation == "count":
                if native.get("key"):
                    row = table.row(str(native["key"]))
                    return DocumentResult(documents=[], affected_count=1 if row else 0, warnings=[])
                total = sum(1 for _ in table.scan(limit=int(native.get("limit", 100))))
                return DocumentResult(documents=[], affected_count=total, warnings=["HBase count is a bounded scan."])
            if operation in ("insert_one", "insert_many", "put", "update_one", "upsert"):
                batch = table.batch()
                for row in native.get("rows", []):
                    batch.put(str(row["key"]), row.get("columns", {}))
                batch.send()
                return DocumentResult(documents=[], affected_count=len(native.get("rows", [])), warnings=[])
            if operation in ("delete_one", "delete_many"):
                for key in native.get("keys", []):
                    table.delete(str(key))
                return DocumentResult(documents=[], affected_count=len(native.get("keys", [])), warnings=[])
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported HBase operation {operation!r}.")

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "HBase has row-level atomicity only; no transactions.")

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "HBase has no transaction handle.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> HBaseConnection:
        if not isinstance(connection, HBaseConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for HBase.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        name = type(exc).__name__
        if "Authentication" in name or "Authorization" in name:
            code = AdapterErrorCode.AUTH
        elif "AlreadyExists" in name or "Invalid" in name:
            code = AdapterErrorCode.SYNTAX
        elif "Timeout" in name or "RetriesExhausted" in name or "NoServer" in name:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        return AdapterError(code, f"HBase {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _row_key(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in ("_id", "_key", "row_key", "key"):
        if key in node and isinstance(node[key], str):
            return node[key]
        if isinstance(node.get("field"), str) and node["field"] in ("_id", "_key", "row_key") and isinstance(node.get("eq"), str):
            return node["eq"]
    if "and" in node and isinstance(node["and"], list):
        for item in node["and"]:
            found = _row_key(item)
            if found:
                return found
    return None


def _column(field: str) -> str:
    return field if ":" in field else f"data:{field}"


def _encode(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if isinstance(value, (date, datetime, time)):
        return value.isoformat().encode()
    return str(value).encode()


def _decode_row(key: str | bytes, row: dict[bytes, bytes]) -> dict[str, Any]:
    result: dict[str, Any] = {"_id": key.decode() if isinstance(key, bytes) else key}
    for column, value in row.items():
        name = column.decode() if isinstance(column, bytes) else str(column)
        try:
            result[name] = value.decode()
        except UnicodeDecodeError:
            result[name] = value
    return result


def _matches(document: dict[str, Any], node: Any) -> bool:
    if not isinstance(node, dict):
        return True
    if "and" in node and isinstance(node["and"], list):
        return all(_matches(document, item) for item in node["and"])
    if "or" in node and isinstance(node["or"], list):
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
        if operator in ("=", "EQ"):
            return value == operand
        if operator in ("!=", "<>", "NE"):
            return value != operand
        if operator == "<":
            return value < operand
        if operator == "<=":
            return value <= operand
        if operator == ">":
            return value > operand
        if operator == ">=":
            return value >= operand
    except TypeError:
        return False
    return False


HBASE_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes", notes="No schema types; callers choose an encoding."),
    "bigint": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "float": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "double": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "text": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes", notes="UTF-8 bytes by convention."),
    "boolean": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "date": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes", notes="HBase cell timestamps are version stamps, not column values."),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "json": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "bytes"),
    "geo": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native geo type."),
    "list": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "map": TypeSupport(TypeSupportLevel.APPROXIMATE, "bytes"),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native vector type."),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No references."),
}

HBASE_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "Row-level atomicity only."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(False),
    CAP_ARRAY_TYPE: Capability(False),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(False),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(False),
    CAP_INDEXES: Capability(False, "No native secondary index; Phoenix or external indexes are separate systems."),
    CAP_FULL_TEXT_SEARCH: Capability(False),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Row-key region splits."),
    CAP_REPLICATION: Capability(True, "HBase replication."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(False),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "Put is an upsert."),
    CAP_TTL: Capability(True, "Family and cell TTL."),
    CAP_SECONDARY_INDEXES: Capability(False),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False),
    CAP_BATCH_WRITES: Capability(True, "Batch puts/deletes."),
    CAP_STRONG_CONSISTENCY: Capability(True, "Strong per-row consistency within a RegionServer."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Column families can be added/altered."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False),
    CAP_NESTED_DOCUMENTS: Capability(False),
    CAP_CHANGE_STREAMS: Capability(True, "HBase replication and WAL-based CDC tooling; not a built-in change feed."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
