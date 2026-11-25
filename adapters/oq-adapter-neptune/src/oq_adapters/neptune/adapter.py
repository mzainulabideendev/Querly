"""Amazon Neptune adapter for Querly.

Neptune is a managed property graph. Querly's portable IR has no graph or
relationship construct, so this adapter maps the document subset onto vertices:
a collection is a vertex label and a document is the vertex property map.
Traversal steps are compiled into a structured step list and executed through
``gremlinpython`` bindings, never by concatenating a Gremlin string.

Neptune also supports openCypher, but Gremlin is the portable traversal
protocol across Neptune engine versions and IAM-authenticated endpoints.

Driver: ``gremlinpython`` (and ``neptune-python-utils`` for IAM auth), imported
lazily.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
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
    from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
    from gremlin_python.process.anonymous_traversal import traversal
    from gremlin_python.process.traversal import P, T
except ImportError:  # pragma: no cover
    DriverRemoteConnection = None
    traversal = None
    P = None
    T = None

__all__ = ["NeptuneAdapter", "NeptuneConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "count",
    "insert_one",
    "insert_many",
    "update_one",
    "delete_one",
    "delete_many",
    "get",
}
_WRITES = {"insert_one", "insert_many", "update_one"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class NeptuneConnection(Connection):
    def __init__(self, config: ConnectionConfig, remote: Any, g: Any) -> None:
        self._config = config
        self._remote = remote
        self._g = g
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def g(self) -> Any:
        return self._g

    def ping(self) -> bool:
        try:
            self._g.V().limit(1).toList()
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._remote.close()
            finally:
                self._closed = True


class NeptuneAdapter(DocumentAdapter):
    data_model = "graph"
    ir_subset = ("schema", "document CRUD", "count")

    @property
    def id(self) -> str:
        return "neptune"

    @property
    def display_name(self) -> str:
        return "Amazon Neptune"

    @property
    def package_version(self) -> str:
        from oq_adapters.neptune import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.neptune import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Amazon Neptune",
            tested_versions=[],
            capabilities=NEPTUNE_CAPS,
            types=NEPTUNE_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "Querly IR 0.1 has no relationship/path construct; native Gremlin traversals are supported by the engine but not expressible.",
                "A collection is a vertex label; a document is a vertex property map.",
                "Vertex properties are flat; nested values must be serialized by the caller.",
                "Transactional isolation is per Gremlin request; no multi-statement interactive transaction is exposed.",
                "IAM authentication requires botocore SigV4 signing (neptune-python-utils) and is only used when configured.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "neptune":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a neptune config.")
        if DriverRemoteConnection is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "gremlinpython is not installed. Install oq-adapter-neptune[driver].",
            )
        host = config.host
        if not host:
            raise AdapterError(AdapterErrorCode.CONNECTION, "Neptune requires a cluster endpoint.")
        scheme = config.options.get("scheme", "wss")
        port = config.port or 8182
        url = config.options.get("url") or f"{scheme}://{host}:{port}/gremlin"
        headers: dict[str, str] = {}
        if secrets.get("auth_token") or config.options.get("iam_auth"):
            token = secrets.get("auth_token")
            if not token:
                raise AdapterError(
                    AdapterErrorCode.AUTH,
                    "IAM authentication needs a SigV4 token in secrets['auth_token'].",
                )
            headers["Authorization"] = token
        try:
            remote = DriverRemoteConnection(
                url,
                "g",
                headers=headers or None,
            )
            g = traversal().withRemote(remote)
            g.V().limit(1).toList()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return NeptuneConnection(config, remote, g)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            values = conn.g.V().limit(1).values("~version").toList()
            raw = str(values[0]) if values else "unknown"
        except Exception:
            # Neptune does not always expose engine version as a property;
            # report the protocol version truthfully rather than guessing.
            raw = "gremlin-python"
        return EngineVersion(product="Amazon Neptune", version=raw, major=0, minor=0, patch=0)

    def close(self, connection: Connection) -> None:
        if isinstance(connection, NeptuneConnection):
            with contextlib.suppress(Exception):
                connection.close()

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            labels = conn.g.V().label().groupCount().next()
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        collections = []
        for label, count in labels.items():
            fields: dict[str, str] = {}
            try:
                samples = conn.g.V().hasLabel(label).limit(100).valueMap().toList()
                for sample in samples:
                    for key, value in sample.items():
                        if key.startswith("~"):
                            continue
                        fields.setdefault(str(key), _neptune_kind(value))
            except Exception:  # noqa: S110 - best-effort label inspection
                pass
            collections.append(
                CollectionSchema(
                    name=str(label),
                    fields=[
                        DocumentField(
                            path=key,
                            type=Type(kind=_universal_kind(kind)),
                            nullable=True,
                            frequency=1.0,
                        )
                        for key, kind in sorted(fields.items())
                    ],
                    document_count=int(count),
                    indexes=[],
                )
            )
        return DocumentStoreSchema(collections=collections)

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        artifacts = []
        for collection in schema.collections:
            artifacts.append(
                DDLArtifact(
                    sql=json_safe(
                        {"vertex_label": collection.name, "note": "Neptune labels are implicit"}
                    ),
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
                                "label": collection.name,
                                "properties": index.columns,
                            }
                        ),
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Neptune")
        diagnostics.extend(
            check_collection(query, {c.name for c in schema.collections}, engine="Neptune")
        )
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "script", "lambda"},
                engine="Neptune",
            )
        )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Neptune aggregation pipelines are not part of the portable subset.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if query.projection or query.skip or query.sort:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Neptune adapter supports traversal-order find; projection/skip/sort need native Gremlin.",
                    operation=query.operation,
                    feature="query_shape",
                )
            )
        if diagnostics:
            return unsupported_document_plan(self.id, "gremlin", query, diagnostics)

        label = query.collection
        steps: list[dict[str, Any]] = []
        if query.operation in (
            "find",
            "find_one",
            "count",
            "delete_one",
            "delete_many",
            "update_one",
        ):
            steps.append({"step": "V"})
            steps.append({"step": "hasLabel", "label": label})
            steps.extend(_filter_steps(query.filter))
        if query.operation in ("find", "find_one", "get"):
            steps.append(
                {
                    "step": "limit",
                    "value": int(query.limit or (1 if query.operation != "find" else 100)),
                }
            )
            steps.append({"step": "valueMap", "include_tokens": True})
        elif query.operation == "count":
            steps.append({"step": "count"})
        elif query.operation in ("insert_one", "insert_many"):
            documents = query.documents[:1] if query.operation == "insert_one" else query.documents
            steps.append({"step": "addV", "label": label})
            for document in documents:
                for key, value in document.items():
                    steps.append({"step": "property", "key": str(key), "value": value})
        elif query.operation == "update_one":
            for key, value in (query.updates or {}).items():
                steps.append({"step": "property", "key": str(key), "value": value})
        elif query.operation in ("delete_one", "delete_many"):
            steps.append({"step": "drop"})
        if query.operation.startswith("delete"):
            steps.append({"step": "count"})

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="gremlin-steps",
                text=json_safe({"label": label, "steps": steps}),
                dialect="gremlin",
            ),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "vertex", "type": "map", "nullable": True}],
            engine_plan={"kind": "gremlin", "label": label, "steps": json_safe(steps)},
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
        try:
            traversal_obj = self._build(conn.g, plan.engine_plan["steps"])
            raw = traversal_obj.toList()
        except Exception as exc:
            raise self._error(exc, "execute") from exc
        operation = plan.operation
        if operation == "count":
            return DocumentResult(
                documents=[], affected_count=int(raw[0]) if raw else 0, warnings=[]
            )
        if operation in ("delete_one", "delete_many"):
            return DocumentResult(
                documents=[], affected_count=int(raw[0]) if raw else 0, warnings=[]
            )
        if operation in ("insert_one", "insert_many", "update_one"):
            return DocumentResult(documents=[], affected_count=len(raw), warnings=[])
        documents = []
        for item in raw:
            documents.append(normalize_document(_unpack_vertex(item)))
        return DocumentResult(documents=documents, affected_count=None, warnings=[])

    def _build(self, g: Any, steps: list[dict[str, Any]]) -> Any:
        current = None
        for step in steps:
            name = step["step"]
            if name == "V":
                current = g.V()
            elif name == "hasLabel":
                current = current.hasLabel(step["label"])
            elif name == "has":
                current = current.has(step["key"], _predicate(step["predicate"]))
            elif name == "limit":
                current = current.limit(int(step["value"]))
            elif name == "valueMap":
                current = current.valueMap(bool(step.get("include_tokens", True)))
            elif name == "count":
                current = current.count()
            elif name == "addV":
                current = current.addV(step["label"])
            elif name == "property":
                current = current.property(step["key"], step["value"])
            elif name == "drop":
                current = current.drop()
            else:  # pragma: no cover
                raise AdapterError(AdapterErrorCode.SYNTAX, f"Unsupported Gremlin step {name!r}.")
        if current is None:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Gremlin step list is empty.")
        return current

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Neptune has no interactive multi-statement transaction; each Gremlin request is atomic.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY, "Neptune has no interactive transaction handle."
        )

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> NeptuneConnection:
        if not isinstance(connection, NeptuneConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Neptune.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        lower = text.lower()
        if "403" in text or "unauthorized" in lower or "signature" in lower:
            code = AdapterErrorCode.AUTH
        elif "400" in text or "syntax" in lower or "invalid" in lower:
            code = AdapterErrorCode.SYNTAX
        elif "constraint" in lower or "already exists" in lower:
            code = AdapterErrorCode.CONSTRAINT
        elif "timeout" in lower or "throttl" in lower or "429" in text:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        return AdapterError(
            code,
            f"Neptune {operation} failed: {text}",
            retryable=retryable,
            cause=exc,
            redacted_cause=text,
        )


def _predicate(spec: dict[str, Any]) -> Any:
    """Convert a JSON-safe predicate spec into a real Gremlin ``P`` object."""

    operator = str(spec.get("op", "eq"))
    value = spec.get("value")
    if P is None:  # pragma: no cover - guarded by connect
        raise AdapterError(AdapterErrorCode.CONNECTION, "gremlinpython is not available.")
    mapping = {
        "eq": P.eq,
        "neq": P.neq,
        "lt": P.lt,
        "lte": P.lte,
        "gt": P.gt,
        "gte": P.gte,
    }
    if operator not in mapping:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"Gremlin predicate {operator!r} is unsupported."
        )
    return mapping[operator](value)


def _filter_steps(node: Any) -> list[dict[str, Any]]:
    if not node:
        return []
    if not isinstance(node, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "Neptune filter must be an object.")
    if "and" in node and isinstance(node["and"], list):
        return [step for item in node["and"] for step in _filter_steps(item)]
    if "or" in node:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, "Gremlin OR filters need a native traversal."
        )
    if "not" in node:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, "Gremlin NOT filters need a native traversal."
        )
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "Neptune filter has no field.")
    if "eq" in node:
        return [{"step": "has", "key": str(field), "predicate": {"op": "eq", "value": node["eq"]}}]
    operator = str(node.get("operator") or "").upper()
    value = node.get("value", node.get("operand"))
    mapping = {
        "=": "eq",
        "EQ": "eq",
        "!=": "neq",
        "<>": "neq",
        "<": "lt",
        "<=": "lte",
        ">": "gt",
        ">=": "gte",
    }
    if operator not in mapping:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, f"Neptune filter operator {operator!r} is unsupported."
        )
    return [
        {"step": "has", "key": str(field), "predicate": {"op": mapping[operator], "value": value}}
    ]


def _unpack_vertex(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"value": item}
    result = {}
    for key, value in item.items():
        if key == "id":
            result["_id"] = value
        elif key in ("label", "type"):
            continue
        elif isinstance(value, list) and len(value) == 1:
            result[str(key)] = value[0]
        else:
            result[str(key)] = value
    return result


def _neptune_kind(value: Any) -> str:
    if isinstance(value, list) and len(value) == 1:
        return _neptune_kind(value[0])
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "double"
    if isinstance(value, (date, datetime, time, timedelta)):
        return "timestamp"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, (list, tuple, set)):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "text"


def _universal_kind(kind: str) -> str:
    return {
        "boolean": "boolean",
        "integer": "integer",
        "double": "double",
        "timestamp": "timestamp",
        "binary": "binary",
        "list": "list",
        "map": "map",
    }.get(kind, "text")


def _value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (Decimal, date, datetime, time, bytes)):
        return value
    return value


NEPTUNE_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "Long"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "Long"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "Float"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "Double"),
    "decimal": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "String/BigDecimal",
        notes="Neptune has no decimal property type; store as string or double.",
    ),
    "text": TypeSupport(TypeSupportLevel.EXACT, "String"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "Boolean"),
    "date": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "String",
        notes="Neptune stores temporal values as strings (ISO-8601 by convention).",
    ),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "Long", notes="Seconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "json": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "String",
        notes="Property values are flat; JSON must be serialized.",
    ),
    "binary": TypeSupport(
        TypeSupportLevel.APPROXIMATE, "ByteBuffer", notes="Base64 in the Gremlin JSON protocol."
    ),
    "geo": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native geo property type."),
    "list": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "List",
        notes="Vertex properties may be multi-valued; this adapter writes one value per property key.",
    ),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "List", notes="No set uniqueness."),
    "map": TypeSupport(
        TypeSupportLevel.UNSUPPORTED,
        None,
        notes="Map values are not vertex property values in the Gremlin protocol.",
    ),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native vector type."),
    "reference": TypeSupport(
        TypeSupportLevel.UNSUPPORTED,
        None,
        notes="Relationships are edges, not properties, and IR 0.1 has no edges.",
    ),
}

NEPTUNE_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(
        False, "Each Gremlin request is atomic; no interactive transaction handle."
    ),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(True, "Traversal returns values."),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(False, "No map property values in Gremlin."),
    CAP_ARRAY_TYPE: Capability(True, "Multi-valued vertex properties."),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(False),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(
        True, "Neptune enforces uniqueness only for IDs, not arbitrary properties."
    ),
    CAP_INDEXES: Capability(
        True, "Neptune manages indexes automatically; no user-created secondary indexes."
    ),
    CAP_FULL_TEXT_SEARCH: Capability(
        True, "Elasticsearch integration is optional and not configured by this adapter."
    ),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(False),
    CAP_REPLICATION: Capability(True, "Multi-AZ cluster replication."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(True, "Gremlin group/groupCount; not expressible in IR."),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(True, "Gremlin traversals nest; not expressible in IR."),
    CAP_UNION: Capability(True, "Gremlin union; not expressible in IR."),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "fold/coalesce idiom; not expressible in IR."),
    CAP_TTL: Capability(True, "Neptune TTL."),
    CAP_SECONDARY_INDEXES: Capability(False),
    CAP_GRAPH_TRAVERSAL: Capability(True, "Native, but IR 0.1 cannot express edges or paths."),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False),
    CAP_BATCH_WRITES: Capability(
        True, "Batch edge/vertex loading; this adapter writes one traversal at a time."
    ),
    CAP_STRONG_CONSISTENCY: Capability(True),
    CAP_MULTI_KEY_ATOMICITY: Capability(True, "One traversal is atomic."),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Schema-optional."),
    CAP_UPSERT_BY_KEY: Capability(True, "fold/coalesce; not exposed."),
    CAP_AGGREGATION_FRAMEWORK: Capability(True, "Gremlin aggregation; not in IR."),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False),
    CAP_NESTED_DOCUMENTS: Capability(False),
    CAP_CHANGE_STREAMS: Capability(True, "Neptune streams."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
