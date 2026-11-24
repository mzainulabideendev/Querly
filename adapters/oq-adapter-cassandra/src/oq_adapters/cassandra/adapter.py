"""Apache Cassandra adapter for Querly.

Cassandra is a partitioned wide-column store with CQL, not a relational
database. This adapter inspects ``system_schema``, compiles CQL with the
partition/clustering-key rules, and refuses to invent joins, transactions, or
schema constraints the engine does not enforce. Consistency is per statement
and is taken from ``ConnectionConfig.options['consistency']``.

Driver: ``cassandra-driver`` (DataStax Python driver), imported lazily.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Mapping

from oq.ir import (
    Artifact,
    Attribute,
    Entity,
    Index,
    PlanDocument,
    QueryDocument,
    SchemaDocument,
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
    Adapter,
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    EngineVersion,
    ResultColumn,
    ResultRow,
    ResultSet,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)
from oq_adapters.kit import build_report, compile_cql_schema, compile_cql_select, diagnostic

try:
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster, ConsistencyLevel, ExecutionProfile
    from cassandra.policies import DCAwareRoundRobinPolicy
except ImportError:  # pragma: no cover
    Cluster = None
    ExecutionProfile = None
    PlainTextAuthProvider = None
    ConsistencyLevel = None
    DCAwareRoundRobinPolicy = None

__all__ = ["CassandraAdapter", "CassandraConnection"]

_CQL_TO_UNIVERSAL = {
    "int": "integer",
    "bigint": "bigint",
    "smallint": "integer",
    "tinyint": "integer",
    "varint": "bigint",
    "float": "float",
    "double": "double",
    "decimal": "decimal",
    "text": "text",
    "varchar": "text",
    "ascii": "text",
    "boolean": "boolean",
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "duration": "duration",
    "uuid": "uuid",
    "timeuuid": "uuid",
    "blob": "binary",
    "list": "list",
    "set": "set",
    "map": "map",
}


class CassandraConnection(Connection):
    def __init__(self, config: ConnectionConfig, cluster: Any, session: Any, keyspace: str | None) -> None:
        self._config = config
        self._cluster = cluster
        self._session = session
        self._keyspace = keyspace
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def session(self) -> Any:
        return self._session

    @property
    def keyspace(self) -> str | None:
        return self._keyspace

    def ping(self) -> bool:
        try:
            self._session.execute("SELECT release_version FROM system.local")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            try:
                self._cluster.shutdown()
            finally:
                self._closed = True


class CassandraAdapter(Adapter):
    data_model = "wide-column"
    ir_subset = ("schema", "select")

    @property
    def id(self) -> str:
        return "cassandra"

    @property
    def display_name(self) -> str:
        return "Apache Cassandra"

    @property
    def package_version(self) -> str:
        from oq_adapters.cassandra import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.cassandra import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Apache Cassandra",
            tested_versions=[],
            capabilities=CASSANDRA_CAPS,
            types=CASSANDRA_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "CQL has no joins, GROUP BY, HAVING, subqueries, UNION, window functions, or OFFSET.",
                "There are no multi-partition ACID transactions; only single-partition batches and lightweight transactions.",
                "Filters on non-key columns require ALLOW FILTERING and are reported as a warning.",
                "UNIQUE is not enforceable except through lightweight transactions.",
                "Secondary indexes are local and not equivalent to relational indexes.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "cassandra":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a cassandra config.")
        if Cluster is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "cassandra-driver is not installed. Install oq-adapter-cassandra[driver].",
            )
        hosts = [config.host] if config.host else list(
            filter(None, str(config.options.get("contact_points", "")).split(","))
        )
        if not hosts:
            raise AdapterError(AdapterErrorCode.CONNECTION, "Cassandra requires a contact point.")
        auth = (
            PlainTextAuthProvider(username=config.user, password=secrets.get("password") or "")
            if config.user
            else None
        )
        consistency_name = str(config.options.get("consistency", "LOCAL_QUORUM")).upper()
        consistency = getattr(ConsistencyLevel, consistency_name, ConsistencyLevel.LOCAL_QUORUM)
        profile = ExecutionProfile(
            consistency_level=consistency,
            load_balancing_policy=DCAwareRoundRobinPolicy(
                local_dc=str(config.options.get("local_dc", "datacenter1"))
            ),
            request_timeout=float(config.options.get("request_timeout", "10")),
        )
        ssl_options = None
        if config.sslmode not in (None, "disable"):
            import ssl

            context = ssl.create_default_context(cafile=config.sslrootcert)
            if config.sslcert and config.sslkey:
                context.load_cert_chain(config.sslcert, config.sslkey)
            ssl_options = {"ssl_context": context}
        try:
            cluster = Cluster(
                contact_points=hosts,
                port=config.port or 9042,
                auth_provider=auth,
                execution_profiles={0: profile, 1: profile},
                ssl_options=ssl_options,
                connect_timeout=float(config.options.get("connect_timeout", "10")),
            )
            session = cluster.connect(config.database) if config.database else cluster.connect()
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return CassandraConnection(config, cluster, session, config.database)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            rows = list(conn.session.execute("SELECT release_version FROM system.local"))
            raw = str(rows[0].release_version) if rows else "unknown"
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        parts = raw.split(".")
        return EngineVersion(
            product="Apache Cassandra",
            version=raw,
            major=int(parts[0]) if parts[0].isdigit() else 0,
            minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, CassandraConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        conn = self._require(connection)
        keyspace = conn.keyspace
        if not keyspace:
            raise AdapterError(AdapterErrorCode.CONNECTION, "Cassandra inspection requires a keyspace.")
        try:
            tables = list(
                conn.session.execute(
                    "SELECT table_name FROM system_schema.tables WHERE keyspace_name = %s",
                    (keyspace,),
                )
            )
            columns = list(
                conn.session.execute(
                    "SELECT table_name, column_name, kind, position, type FROM system_schema.columns "
                    "WHERE keyspace_name = %s",
                    (keyspace,),
                )
            )
            indexes = list(
                conn.session.execute(
                    "SELECT table_name, index_name, kind, options FROM system_schema.indexes "
                    "WHERE keyspace_name = %s",
                    (keyspace,),
                )
            )
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        by_table: dict[str, list[Any]] = {}
        for row in columns:
            by_table.setdefault(str(row.table_name), []).append(row)
        index_rows: dict[str, list[Any]] = {}
        for row in indexes:
            index_rows.setdefault(str(row.table_name), []).append(row)
        entities: list[Entity] = []
        for table in tables:
            name = str(table.table_name)
            key_rows = sorted(
                [row for row in by_table.get(name, []) if str(row.kind) in ("partition_key", "clustering")],
                key=lambda row: int(row.position or 0),
            )
            partition = [str(row.column_name) for row in key_rows if str(row.kind) == "partition_key"]
            clustering = [str(row.column_name) for row in key_rows if str(row.kind) == "clustering"]
            attributes = []
            for row in by_table.get(name, []):
                kind, type_obj = _cassandra_type(str(row.type))
                attributes.append(
                    Attribute(
                        name=str(row.column_name),
                        type=type_obj,
                        nullable=str(row.kind) not in ("partition_key", "clustering"),
                        primary=str(row.kind) in ("partition_key", "clustering"),
                        indexed=any(str(item.table_name) == name for item in index_rows.get(name, [])),
                    )
                )
            entities.append(
                Entity(
                    name=name,
                    attributes=attributes,
                    primary_key=partition + clustering,
                    indexes=[
                        Index(
                            name=str(row.index_name),
                            columns=[str((row.options or {}).get("target", ""))],
                            unique=False,
                            kind=str(row.kind),
                            options=dict(row.options or {}),
                        )
                        for row in index_rows.get(name, [])
                    ],
                    partition_key=partition,
                )
            )
        return SchemaDocument(entities=entities, enums=[], extensions=[])

    def compile_schema(self, schema: SchemaDocument, options: CompileOptions | None) -> list[DDLArtifact]:
        result = compile_cql_schema(
            schema,
            engine="Cassandra",
            keyspace=None,
            supports_vector=False,
            supports_materialized_views=True,
        )
        if not result.supported:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Cassandra cannot compile part of the schema.",
                diagnostics=[item.model_dump(mode="json") for item in result.diagnostics],
            )
        return result.artifacts

    def compile_query(
        self, query: QueryDocument, schema: SchemaDocument, options: CompileOptions | None = None
    ) -> PlanDocument:
        result = compile_cql_select(
            query, schema, engine="Cassandra", keyspace=None, supports_offset=False
        )
        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="cql",
                text=result.cql,
                dialect="cassandra-cql",
                placeholders=result.parameters,
            ),
            parameters=result.parameters,
            safety=query.safety,
            result_columns=result.result_columns,
            engine_plan={"kind": "cql", "cql": result.cql, "parameter_order": [p["name"] for p in result.parameters]},
            diagnostics=result.diagnostics,
            supported=result.supported,
        )

    def execute(self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]) -> ResultSet:
        conn = self._require(connection)
        if not plan.supported or not plan.artifact.text:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Plan is unsupported and was not executed.",
                diagnostics=[item.model_dump(mode="json") for item in plan.diagnostics],
            )
        values = tuple(params[str(entry["name"])] for entry in plan.parameters)
        try:
            rows = conn.session.execute(plan.artifact.text, values)
            all_rows = list(rows)
        except Exception as exc:
            raise self._error(exc, "execute") from exc
        columns = []
        if getattr(rows, "column_names", None):
            columns = [
                ResultColumn(name=str(name), type=None, nullable=True)
                for name in rows.column_names
            ]
        return ResultSet(
            columns=columns,
            rows=[ResultRow(values=tuple(_value(item) for item in row)) for row in all_rows],
            affected_rows=None,
            warnings=[],
        )

    def begin(self, connection: Connection, options: TransactionOptions | None = None) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "Cassandra has no multi-statement transactions; use a single-partition BATCH or LWT.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "Cassandra has no transactions.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> CassandraConnection:
        if not isinstance(connection, CassandraConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for Cassandra.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        name = type(exc).__name__
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if "Authentication" in name or "Authorization" in name:
            code = AdapterErrorCode.AUTH
        elif "AlreadyExists" in name or "InvalidRequest" in name:
            code = AdapterErrorCode.SYNTAX
        elif "Unavailable" in name or "Timeout" in name or "Overloaded" in name:
            code = AdapterErrorCode.TIMEOUT
            retryable = True
        elif "NoHostAvailable" in name:
            retryable = True
        return AdapterError(code, f"Cassandra {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _cassandra_type(native: str) -> tuple[str, Type]:
    base = native.strip().lower()
    if base.startswith("frozen<"):
        base = base[7:-1]
    if base.startswith("vector<"):
        return "vector", Type(kind="vector")
    if base.startswith("list<"):
        return "list", Type(kind="list")
    if base.startswith("set<"):
        return "set", Type(kind="set")
    if base.startswith("map<"):
        return "map", Type(kind="map")
    if base.startswith("tuple<"):
        return "json", Type(kind="json")
    kind = _CQL_TO_UNIVERSAL.get(base, "text")
    return kind, Type(kind=kind)


def _value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (Decimal, date, datetime, time, bytes)):
        return value
    return value


CASSANDRA_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "int", range="-2147483648..2147483647"),
    "bigint": TypeSupport(TypeSupportLevel.EXACT, "bigint"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "float"),
    "double": TypeSupport(TypeSupportLevel.EXACT, "double"),
    "decimal": TypeSupport(TypeSupportLevel.EXACT, "decimal", notes="Arbitrary precision decimal."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "text"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "boolean"),
    "date": TypeSupport(TypeSupportLevel.EXACT, "date"),
    "time": TypeSupport(TypeSupportLevel.EXACT, "time"),
    "timestamp": TypeSupport(TypeSupportLevel.EXACT, "timestamp", notes="Millisecond precision, UTC."),
    "datetime": TypeSupport(TypeSupportLevel.COMPATIBLE, "timestamp"),
    "duration": TypeSupport(TypeSupportLevel.EXACT, "duration"),
    "uuid": TypeSupport(TypeSupportLevel.EXACT, "uuid"),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "text", notes="No validation."),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "text", notes="No validation."),
    "json": TypeSupport(TypeSupportLevel.APPROXIMATE, "text", notes="JSON is stored as text; no native JSON type."),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "blob"),
    "geo": TypeSupport(TypeSupportLevel.APPROXIMATE, "text", notes="No native geo type; WKT text by convention."),
    "list": TypeSupport(TypeSupportLevel.COMPATIBLE, "list<text>", notes="Element type narrowed by this adapter."),
    "set": TypeSupport(TypeSupportLevel.COMPATIBLE, "set<text>"),
    "map": TypeSupport(TypeSupportLevel.COMPATIBLE, "map<text, text>"),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="Vector columns require Cassandra 5.0 and are version-probed, not advertised."),
    "reference": TypeSupport(TypeSupportLevel.APPROXIMATE, "uuid", notes="Application-level reference; no foreign keys."),
}

CASSANDRA_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No multi-partition ACID transactions; LWT and batches only."),
    CAP_TRANSACTIONAL_DDL: Capability(False, "Schema changes are not transactional."),
    CAP_RETURNING: Capability(False, "CQL has no RETURNING."),
    CAP_ORDER_BY_NULLS: Capability(False, "No NULLS FIRST/LAST; nulls sort first in CQL."),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(False, "Cassandra 5 vector type is version-dependent and not advertised."),
    CAP_JSON: Capability(False, "No native JSON column type."),
    CAP_ARRAY_TYPE: Capability(True, "list<T> collections."),
    CAP_UUID_TYPE: Capability(True),
    CAP_GEO_TYPE: Capability(False),
    CAP_DURATION_TYPE: Capability(True),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(True, "decimal is arbitrary precision."),
    CAP_CHECK_CONSTRAINTS: Capability(False, "No CHECK constraints."),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(False, "Only lightweight transactions can enforce uniqueness."),
    CAP_INDEXES: Capability(True, "Secondary indexes and SAI/SAI-like indexes vary by version."),
    CAP_FULL_TEXT_SEARCH: Capability(False, "Requires an external search index (DSE Search/Solr); not in Cassandra."),
    CAP_MATERIALIZED_VIEWS: Capability(True),
    CAP_PARTITIONING: Capability(True, "Partition keys and clustering columns."),
    CAP_REPLICATION: Capability(True, "Configurable replication factor per keyspace."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(False),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "INSERT is an upsert."),
    CAP_TTL: Capability(True, "Per-write and table default TTL."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(True, "User-defined functions, with sandbox restrictions."),
    CAP_BATCH_WRITES: Capability(True, "Logged and unlogged batches."),
    CAP_STRONG_CONSISTENCY: Capability(False, "Tunable consistency; not strongly consistent by default."),
    CAP_MULTI_KEY_ATOMICITY: Capability(False, "Atomic only within one partition."),
    CAP_SCHEMA_EVOLUTION: Capability(True),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(False),
    CAP_CHANGE_STREAMS: Capability(False, "No CDC stream in Apache Cassandra before 4.0 sidecars; not part of this adapter."),
}
