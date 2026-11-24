"""Neo4j adapter for Querly.

Neo4j is a property graph, not a relational database. This adapter maps the
document-store IR subset onto labeled nodes: a collection is a label and a
document is a node's property map. Relationships and native graph traversals are
honest limitations of Querly IR 0.1; the engine supports them, but the portable
IR does not, so they are not advertised as supported by this adapter.

Cypher is generated as a native artifact, values are bound as ``$p`` parameters,
and server-side procedures/apoc calls are never injected from user filters.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Mapping

from oq.ir import (
    AdapterDiagnostic,
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
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None

__all__ = ["Neo4jAdapter", "Neo4jConnection", "Neo4jTransaction"]

_SUPPORTED = {
    "find",
    "find_one",
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "delete_one",
    "delete_many",
    "count",
}
_WRITES = {"insert_one", "insert_many", "update_one", "update_many"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class Neo4jConnection(Connection):
    def __init__(self, config: ConnectionConfig, driver: Any, database: str) -> None:
        self._config = config
        self._driver = driver
        self._database = database
        self._session: Any = None
        self._tx: Any = None
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    def ping(self) -> bool:
        try:
            self._driver.verify_connectivity()
            return True
        except Exception:
            return False

    @property
    def driver(self) -> Any:
        return self._driver

    @property
    def database(self) -> str:
        return self._database

    def session(self) -> Any:
        if self._session is None:
            self._session = self._driver.session(database=self._database)
        return self._session

    def close(self) -> None:
        if not self._closed:
            try:
                if self._tx is not None:
                    self._tx.rollback()
            except Exception:
                pass
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
            self._driver.close()
            self._closed = True


class Neo4jTransaction(Transaction):
    def __init__(self, connection: Neo4jConnection, options: TransactionOptions) -> None:
        self._connection = connection
        self._options = options
        self._tx = connection.session().begin_transaction()
        self._active = True

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def is_active(self) -> bool:
        return self._active and not self._connection.is_closed

    @property
    def native(self) -> Any:
        return self._tx

    def commit(self) -> None:
        if self._active:
            self._tx.commit()
            self._active = False
            self._connection._tx = None

    def rollback(self) -> None:
        if self._active:
            self._tx.rollback()
            self._active = False
            self._connection._tx = None


class Neo4jAdapter(DocumentAdapter):
    data_model = "graph"
    ir_subset = ("schema", "document CRUD", "count")

    @property
    def id(self) -> str:
        return "neo4j"

    @property
    def display_name(self) -> str:
        return "Neo4j"

    @property
    def package_version(self) -> str:
        from oq_adapters.neo4j import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.neo4j import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Neo4j",
            tested_versions=[],
            capabilities=NEO4J_CAPS,
            types=NEO4J_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "Querly IR 0.1 has no relationship or path construct; graph traversal is native but not expressible.",
                "A collection is a node label; a document is a property map.",
                "Schema inspection samples property values for types when APOC is absent.",
                "Server-side procedures are never called from compiled user filters.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "neo4j":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a neo4j config.")
        if GraphDatabase is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "neo4j is not installed. Install oq-adapter-neo4j[driver].",
            )
        scheme = config.options.get("scheme", "bolt")
        uri = config.options.get("uri") or f"{scheme}://{config.host or 'localhost'}:{config.port or 7687}"
        auth = (config.user, secrets.get("password")) if config.user else None
        try:
            driver = GraphDatabase.driver(
                uri,
                auth=auth,
                connection_timeout=float(config.options.get("connect_timeout", "10")),
                encrypted=config.sslmode not in (None, "disable"),
            )
            driver.verify_connectivity()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return Neo4jConnection(config, driver, config.database or config.options.get("database", "neo4j"))

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            records, _, _ = conn.driver.execute_query(
                "CALL dbms.components() YIELD versions, name RETURN name, versions[0] AS version",
                database_=conn.database,
            )
            raw = str(records[0]["version"]) if records else "Neo4j"
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="Neo4j",
            version=raw,
            major=int(parts[0]) if parts and parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, Neo4jConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            records, _, _ = conn.driver.execute_query(
                "CALL db.labels() YIELD label RETURN label ORDER BY label",
                database_=conn.database,
            )
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        collections: list[CollectionSchema] = []
        for record in records:
            label = str(record["label"])
            fields: dict[str, str] = {}
            try:
                sample, _, _ = conn.driver.execute_query(
                    f"MATCH (n:`{_escape_label(label)}`) RETURN n LIMIT 100",
                    database_=conn.database,
                )
                for item in sample:
                    node = item["n"]
                    for key, value in dict(node).items():
                        fields.setdefault(str(key), _neo4j_kind(value))
            except Exception:
                # Inspection must not fail because one label is unreadable.
                pass
            collections.append(
                CollectionSchema(
                    name=label,
                    fields=[
                        DocumentField(
                            path=key,
                            type=Type(kind=_universal_kind(kind)),
                            nullable=True,
                            frequency=1.0,
                        )
                        for key, kind in sorted(fields.items())
                    ],
                    indexes=self._indexes(conn, label),
                )
            )
        return DocumentStoreSchema(collections=collections)

    def _indexes(self, conn: Neo4jConnection, label: str) -> list[Index]:
        try:
            records, _, _ = conn.driver.execute_query(
                "SHOW INDEXES YIELD name, labelsOrTypes, properties, type, uniqueness, entityType "
                "WHERE entityType = 'NODE'",
                database_=conn.database,
            )
        except Exception:
            return []
        result = []
        for record in records:
            labels = record["labelsOrTypes"] or []
            if label not in labels:
                continue
            props = [str(item) for item in (record["properties"] or [])]
            result.append(
                Index(
                    name=str(record["name"]),
                    columns=props,
                    unique=str(record["uniqueness"]).upper() in ("UNIQUE", "NODE_KEY"),
                    kind=str(record["type"]).lower(),
                    options={"entity_type": str(record["entityType"])},
                )
            )
        return result

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        artifacts: list[DDLArtifact] = []
        for collection in schema.collections:
            label = _escape_label(collection.name)
            # Neo4j has no CREATE LABEL; a label exists when a node uses it.
            artifacts.append(
                DDLArtifact(
                    sql=f"// Label `{label}` is implicit; no create statement is required.",
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                props = ", ".join(f"n.`{_escape_label(prop)}`" for prop in index.columns)
                kind = "RANGE"
                if index.kind.lower() in ("text", "fulltext"):
                    kind = "FULLTEXT"
                elif index.kind.lower() in ("vector", "point"):
                    kind = index.kind.upper()
                unique = "UNIQUE " if index.unique else ""
                statement = (
                    f"CREATE {unique}{kind} INDEX `{_escape_label(index.name)}` "
                    f"IF NOT EXISTS FOR (n:`{label}`) ON ({props})"
                )
                artifacts.append(
                    DDLArtifact(
                        sql=statement,
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
        return artifacts

    def compile_query(
        self, query: DocumentQuery, schema: DocumentStoreSchema
    ) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="Neo4j")
        diagnostics.extend(
            check_collection(
                query,
                {collection.name for collection in schema.collections},
                engine="Neo4j",
            )
        )
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$accumulator", "$expr"},
                engine="Neo4j",
            )
        )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "Aggregation pipelines are not part of the Neo4j document subset.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if diagnostics:
            return unsupported_document_plan(self.id, "cypher", query, diagnostics)

        label = _escape_label(query.collection)
        params: dict[str, Any] = {}
        counter = [0]

        def bind(value: Any) -> str:
            counter[0] += 1
            name = f"p{counter[0]}"
            params[name] = value
            return f"${name}"

        where = self._compile_filter(query.filter, bind) if query.filter else ""
        sql = ""
        if query.operation in ("find", "find_one", "count") and where:
            base = f"MATCH (n:`{label}`) WHERE {where}"
        elif query.operation in ("find", "find_one", "count"):
            base = f"MATCH (n:`{label}`)"
        elif query.operation in ("update_one", "update_many", "delete_one", "delete_many"):
            base = f"MATCH (n:`{label}`)"
            if where:
                base += f" WHERE {where}"
        else:
            base = f"MATCH (n:`{label}`)"

        if query.operation == "count":
            sql = base + " RETURN count(n) AS count"
        elif query.operation in ("find", "find_one"):
            projection = self._projection(query.projection)
            sql = base + f" RETURN {projection} AS doc"
            if query.sort:
                sql += " ORDER BY " + ", ".join(
                    f"doc.`{_escape_label(str(item.get('field')))}` "
                    + ("DESC" if str(item.get("direction", 1)) in ("-1", "desc", "DESC") else "ASC")
                    for item in query.sort
                )
            if query.skip:
                sql += f" SKIP {int(query.skip)}"
            limit = query.limit or (1 if query.operation == "find_one" else None)
            if limit:
                sql += f" LIMIT {int(limit)}"
        elif query.operation in ("insert_one", "insert_many"):
            documents = []
            if query.operation == "insert_one":
                documents = [query.documents[0] if query.documents else {}]
            else:
                documents = list(query.documents)
            clauses = []
            for document in documents:
                param = bind(document)
                clauses.append(
                    f"CREATE (n:`{label}`) SET n = {param} RETURN properties(n) AS doc"
                )
            sql = " UNION ALL ".join(clauses) if clauses else f"CREATE (n:`{label}`) RETURN properties(n) AS doc"
        elif query.operation in ("update_one", "update_many"):
            document = query.updates or {}
            param = bind(document)
            sql = base + f" SET n += {param} RETURN properties(n) AS doc"
        elif query.operation in ("delete_one", "delete_many"):
            sql = base + " DETACH DELETE n RETURN count(n) AS deleted"
        else:  # pragma: no cover - guarded above
            return unsupported_document_plan(
                self.id,
                "cypher",
                query,
                [diagnostic(AdapterErrorCode.UNSUPPORTED, "Unsupported operation.")],
            )

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="cypher", text=sql, dialect="cypher", placeholders=[]),
            parameters=[{"name": name, "type": type_kind(_literal_type(value))} for name, value in params.items()],
            required_capabilities=[CAP_GRAPH_TRAVERSAL] if False else [],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "doc", "type": "map", "nullable": False}],
            engine_plan={
                "kind": "cypher",
                "cypher": sql,
                "parameters": json_safe(params),
                "label": query.collection,
            },
            diagnostics=[],
            supported=True,
        )

    def _compile_filter(self, node: Any, bind: Any) -> str:
        if not isinstance(node, dict):
            raise AdapterError(AdapterErrorCode.SYNTAX, "Neo4j filter must be an object.")
        if any(key in node for key in ("$where", "$function", "$expr")):
            raise AdapterError(AdapterErrorCode.UNSUPPORTED, "Server-side scripts are not supported.")
        if "and" in node and isinstance(node["and"], list):
            return "(" + " AND ".join(self._compile_filter(item, bind) for item in node["and"]) + ")"
        if "or" in node and isinstance(node["or"], list):
            return "(" + " OR ".join(self._compile_filter(item, bind) for item in node["or"]) + ")"
        if "not" in node:
            return f"(NOT {self._compile_filter(node['not'], bind)})"
        # Field: value or operator object.
        field = str(node.get("field") or node.get("name") or "")
        if not field:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Neo4j filter has no field.")
        column = f"n.`{_escape_label(field)}`"
        value = node.get("value", node.get("eq"))
        if value is not None:
            return f"{column} = {bind(value)}"
        operator = str(node.get("operator") or "").upper()
        operand = node.get("operand", node.get("right"))
        mapping = {
            "=": "=",
            "!=": "<>",
            "<>": "<>",
            "<": "<",
            "<=": "<=",
            ">": ">",
            ">=": ">=",
            "IN": "IN",
            "CONTAINS": "CONTAINS",
            "STARTS_WITH": "STARTS WITH",
            "ENDS_WITH": "ENDS WITH",
        }
        if operator not in mapping:
            raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Neo4j filter operator {operator!r} is unsupported.")
        if operator == "IN":
            values = operand if isinstance(operand, list) else [operand]
            return f"{column} IN {bind(values)}"
        return f"{column} {mapping[operator]} {bind(operand)}"

    def _projection(self, projection: Any) -> str:
        if not projection:
            return "properties(n)"
        if isinstance(projection, dict) and projection.get("_id"):
            return "elementId(n)"
        return "properties(n)"

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
        native_params = plan.engine_plan.get("parameters", {})
        try:
            tx = conn.session().begin_transaction()
            try:
                result = tx.run(plan.artifact.text, **native_params)
                records = [dict(record) for record in result]
                tx.commit()
            except Exception:
                tx.rollback()
                raise
        except Exception as exc:
            raise self._error(exc, "execute") from exc
        documents = []
        affected = None
        for record in records:
            if "doc" in record:
                documents.append(normalize_document(record["doc"]))
            elif "count" in record:
                affected = int(record["count"])
            elif "deleted" in record:
                affected = int(record["deleted"])
        return DocumentResult(documents=documents, affected_count=affected, warnings=[])

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        conn = self._require(connection)
        tx = Neo4jTransaction(conn, options)
        conn._tx = tx.native
        return tx

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, Neo4jTransaction):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong transaction type.")
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if isinstance(transaction, Neo4jTransaction):
            try:
                transaction.rollback()
            except Exception:
                pass

    def _require(self, connection: Connection) -> Neo4jConnection:
        if not isinstance(connection, Neo4jConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Neo4j.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "authentication" in text.lower() or "Unauthorized" in text:
            code = AdapterErrorCode.AUTH
        elif "ConstraintValidationFailed" in text or "already exists" in text:
            code = AdapterErrorCode.CONSTRAINT
        elif "SyntaxError" in text or "Invalid input" in text:
            code = AdapterErrorCode.SYNTAX
        elif "TransientError" in text or "Deadlock" in text:
            retryable = True
        return AdapterError(code, f"Neo4j {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


NEO4J_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "Integer (64-bit)"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "Integer (64-bit)"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "Float"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "Float"),
    "decimal": TypeSupport(TypeSupportLevel.APPROXIMATE, "Float or String", notes="No native decimal in Neo4j properties."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "String"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "Boolean"),
    "date": TypeSupport(TypeSupportLevel.EXACT, "Date"),
    "time": TypeSupport(TypeSupportLevel.EXACT, "Time"),
    "timestamp": TypeSupport(TypeSupportLevel.EXACT, "DateTime (with zone)", notes="Zone preserved through the Neo4j temporal type."),
    "datetime": TypeSupport(TypeSupportLevel.COMPATIBLE, "LocalDateTime"),
    "duration": TypeSupport(TypeSupportLevel.EXACT, "Duration"),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "String", notes="No native UUID property type."),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "String"),
    "json": TypeSupport(TypeSupportLevel.COMPATIBLE, "Map / List", notes="Nested property maps are not allowed to contain arbitrary nesting beyond Neo4j's property value rules."),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "Byte array"),
    "geo": TypeSupport(TypeSupportLevel.EXACT, "Point"),
    "list": TypeSupport(TypeSupportLevel.EXACT, "List"),
    "set": TypeSupport(TypeSupportLevel.APPROXIMATE, "List", notes="No set type; duplicates are allowed."),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "Map", notes="Property maps are flat in Neo4j."),
    "vector": TypeSupport(TypeSupportLevel.COMPATIBLE, "List<Float>", notes="Vector indexes exist in Neo4j 5.x; no dedicated property type."),
    "reference": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="Relationships are native graph edges; they are not a property type and are not in IR 0.1."),
}

NEO4J_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(True),
    CAP_TRANSACTIONAL_DDL: Capability(False, "Index/constraint DDL is not rolled back with data transactions."),
    CAP_RETURNING: Capability(True, "Cypher RETURN."),
    CAP_ORDER_BY_NULLS: Capability(True),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(False, "Vector indexes are native on 5.x but the property type is List<Float> and the IR has no vector operation."),
    CAP_JSON: Capability(True, "Maps/lists as property values."),
    CAP_ARRAY_TYPE: Capability(True, "Lists."),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(True, "Point."),
    CAP_DURATION_TYPE: Capability(True),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False),
    CAP_CHECK_CONSTRAINTS: Capability(True, "Node key/existence/property type constraints."),
    CAP_FOREIGN_KEYS: Capability(False, "Relationships are graph edges, not foreign keys."),
    CAP_UNIQUE_CONSTRAINTS: Capability(True, "Unique property constraints."),
    CAP_INDEXES: Capability(True),
    CAP_FULL_TEXT_SEARCH: Capability(True, "Full-text indexes."),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(False, "No table partitioning; database sharding exists in Enterprise."),
    CAP_REPLICATION: Capability(True, "Cluster replication."),
    CAP_JOINS: Capability(False, "Graph traversal replaces relational joins; IR has no join construct."),
    CAP_GROUP_BY: Capability(True, "Cypher aggregation."),
    CAP_HAVING: Capability(False, "Cypher has WITH ... WHERE instead of HAVING."),
    CAP_SUBQUERIES: Capability(True, "Cypher subqueries; not in IR."),
    CAP_UNION: Capability(True, "Cypher UNION; not in IR."),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "MERGE."),
    CAP_TTL: Capability(False),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(True, "Native, but not expressible in Querly IR 0.1."),
    CAP_VECTOR_SEARCH: Capability(True, "Vector index + db.index.vector.queryNodes; not in IR."),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "User-defined procedures."),
    CAP_BATCH_WRITES: Capability(True, "UNWIND batches."),
    CAP_STRONG_CONSISTENCY: Capability(True),
    CAP_MULTI_KEY_ATOMICITY: Capability(True, "Within one transaction."),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Schema-optional; indexes evolve."),
    CAP_UPSERT_BY_KEY: Capability(True, "MERGE."),
    CAP_ANN_SEARCH: Capability(True, "Vector index; not in IR."),
    CAP_HYBRID_SEARCH: Capability(False),
    CAP_AGGREGATION_FRAMEWORK: Capability(True, "Cypher aggregation."),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(True, "Constraints, not JSON Schema."),
    CAP_NESTED_DOCUMENTS: Capability(False, "Neo4j properties cannot contain nested maps as property values."),
    CAP_CHANGE_STREAMS: Capability(True),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(True),
}


def _escape_label(value: str) -> str:
    return value.replace("`", "``")


def _neo4j_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, (date, datetime, time, timedelta)):
        return "temporal"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "map"
    return type(value).__name__


def _universal_kind(kind: str) -> str:
    return {
        "bool": "boolean",
        "integer": "integer",
        "double": "double",
        "string": "text",
        "binary": "binary",
        "temporal": "timestamp",
        "list": "list",
        "map": "map",
        "null": "null",
    }.get(kind, "text")


def _literal_type(value: Any) -> Type:
    return Type(kind=_universal_kind(_neo4j_kind(value)))
