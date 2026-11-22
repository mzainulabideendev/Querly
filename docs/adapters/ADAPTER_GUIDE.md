# Querly Adapter Development Guide

**Status:** Phase 0 contract design. No adapter is implemented yet. The
interface below is the contract Phase 4 adapters must satisfy; it is expected to
be refined in Phase 4, but the obligations are not optional.

## 1. What an adapter is

An adapter connects one database engine to the Querly IR. It is responsible for:

1. reporting what the engine can and cannot do, truthfully;
2. inspecting an existing schema into universal schema IR;
3. compiling universal schema IR into engine DDL;
4. compiling universal query IR into an engine artifact plus a binding plan;
5. executing a binding plan with parameters, inside a policy-controlled runtime;
6. normalizing results and errors;
7. managing connections and, where supported, transactions.

An adapter is not allowed to:

* parse Querly source (that is `oq-core`),
* invent IR constructs the language does not define,
* silently drop unsupported constraints,
* claim a feature is supported because the engine has a similarly named one,
* log credentials, bound values, or full connection strings,
* execute anything that the runtime policy has rejected,
* classify a generated artifact as executed.

## 2. Distribution and discovery

Adapters are independent distributions named `oq-adapter-<engine>`, for example
`oq-adapter-postgres`. They install into the `oq_adapters` namespace.

They publish an entry point in group `oq.adapters`:

```toml
[project.entry-points."oq.adapters"]
postgres = "oq_adapters.postgres:ADAPTER"
```

Discovery rules:

* The CLI lists adapters by reading installed entry points.
* Loading an adapter must not connect to anything.
* Importing an adapter must not require the database server to be running.
* Duplicate ids are an error, not a last-one-wins resolution.
* The adapter id is lower-case ASCII and stable. It is the id used in
  `convert schema from postgres ...` and in connection configuration.

The driver dependency belongs to the adapter package, never to a core package.

## 3. Required interface

The concrete Python types are defined in `oq-runtime` in Phase 4. The required
behaviour is:

```python
class Adapter(Protocol):
    id: str
    display_name: str
    package_version: str

    def capabilities(self) -> CapabilityReport: ...
    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection: ...
    def inspect_schema(self, connection: Connection) -> SchemaDocument: ...
    def compile_schema(
        self, schema: SchemaDocument, options: CompileOptions
    ) -> list[DDLArtifact]: ...
    def compile_query(self, query: QueryDocument, schema: SchemaDocument) -> PlanDocument: ...
    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Value]
    ) -> ResultSet: ...
    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction: ...
    def commit(self, transaction: Transaction) -> None: ...
    def rollback(self, transaction: Transaction) -> None: ...
    def detect_version(self, connection: Connection) -> EngineVersion: ...
    def close(self, connection: Connection) -> None: ...
```

Contract notes:

* `connect` receives configuration and a secret provider. It never receives
  secrets as plain strings embedded in a URL that might be logged.
* `compile_*` is pure with respect to the database: it may not execute DDL or
  queries. Compilation may use a live connection for catalog introspection only
  when explicitly passed an inspection handle, and that must be documented.
* `execute` receives a plan that already passed capability analysis and policy
  checks. It must not re-plan. It must bind values, never interpolate them.
* `execute` returns a `ResultSet` with ordered column metadata, rows, affected
  row count, and engine-specific warnings normalized into a list.
* Errors are raised as `AdapterError` subclasses with a stable code, a
  retryability flag, and a redacted message. Drivers' exception text may be
  attached as a separately redacted `cause` field.

## 4. Capability report

Capability reporting is the most important part of the contract. A capability
report is versioned and machine-readable:

```json
{
  "adapter": "postgres",
  "adapter_version": "0.1.0.dev0",
  "engine": "PostgreSQL",
  "tested_versions": ["14.11", "15.6", "16.2"],
  "capabilities": {
    "transactions": {"supported": true, "notes": null},
    "transactional_ddl": {"supported": true, "notes": null},
    "returning": {"supported": true, "notes": null},
    "order_by_nulls": {"supported": true, "notes": null},
    "partial_indexes": {"supported": true, "notes": "not yet expressible in language 0.1"},
    "vector_type": {"supported": false, "notes": "requires pgvector extension"}
  },
  "types": {
    "integer": {"support": "exact", "native": "bigint", "range": "-9223372036854775808..9223372036854775807"},
    "decimal": {"support": "exact", "native": "numeric", "max_precision": 1000},
    "json": {"support": "compatible", "native": "jsonb", "notes": "duplicate keys and key order are not preserved"},
    "geo": {"support": "unsupported", "native": null, "notes": "no native geography type"}
  },
  "limitations": ["..."],
  "generated_at": "2026-01-01T00:00:00Z"
}
```

Rules:

* `tested_versions` lists only versions the adapter's integration suite actually
  ran against in CI. Marketing versions are not listed.
* Type support is one of `exact`, `compatible`, `approximate`, `unsupported`.
  `compatible` means values and constraints survive, with documented differences
  that do not change results. `approximate` means some values or guarantees are
  lost. Anything with `approximate` participates in conversion reporting.
* Capabilities are never inferred from engine version alone. The adapter probes
  the connection when a capability depends on server configuration or
  extensions, and records the probe result.
* A capability marked `supported` without a corresponding integration test is a
  defect and must fail the adapter's CI.

## 5. Schema inspection

`inspect_schema` returns universal schema IR, not engine DDL and not a raw
catalog dump. Requirements:

* Preserve declared names exactly, including case, and record the engine's
  folding behaviour in the capability report.
* Map every type; if a type cannot be mapped exactly, return the closest
  universal type and record an inspection finding. Inspection findings are
  surfaced to the user, not swallowed.
* Preserve nullability, primary keys, unique constraints, foreign keys, checks
  where available, defaults, and indexes.
* Preserve composite keys and composite uniqueness.
* Record unsupported objects (triggers, procedures, partitions, custom types) in
  an `unsupported_objects` list with their kind and name. Do not represent them
  as something else.
* Inspection must be read-only and must not acquire write locks. On engines
  where catalog reads can block, the adapter documents that fact and uses
  read-only transactions.

## 6. Query compilation

`compile_query` returns a `PlanDocument` whose artifact is engine-specific.

Requirements:

* Validate that every required capability is reported as supported. If not,
  return a capability finding; do not approximate.
* Use placeholders for every parameter. Placeholder syntax is adapter-specific;
  values never appear in the artifact.
* Quote identifiers according to the engine's rules, including the declared
  case. Never trust names from an inspector without validating them against the
  schema document.
* Record expected result columns and types.
* Set a safety classification (`read`, `write`, `destructive_write`, `ddl`)
  based on the IR operation, never based on string inspection of the artifact.
* Reject plans whose `limit` or timeout exceeds runtime policy.
* Deterministic output: identical IR and capabilities produce identical
  artifacts. This is required for golden tests.

## 7. Execution and transactions

Execution is always mediated by `oq-runtime`:

* The runtime applies read-only policy, destructive confirmation, row limits,
  statement timeout, and audit logging.
* The adapter executes a plan with bound parameters and returns normalized rows.
* The adapter must not retry a write implicitly. Retries are a runtime policy
  and must be idempotency-aware.
* Transaction support is declared. If an engine cannot roll back DDL, the
  adapter reports `transactional_ddl: false`, and the migration planner splits
  or refuses accordingly.
* Cancellation must use the driver's supported cancellation mechanism. If none
  exists, the adapter reports the limitation rather than pretending the
  statement stopped.

## 8. Result and error normalization

Result values are normalized to universal types:

* decimals as exact decimal strings or `decimal.Decimal`, never binary floats;
* timestamps as timezone-aware datetimes when the source has a zone, naive
  otherwise, with the zone presence recorded;
* binary as `bytes`;
* JSON as parsed values with a documented duplicate-key policy;
* documents and key-value stores as ordered maps or tuples with metadata about
  which native shape produced them.

Errors map to stable adapter codes:

| Code | Meaning |
| --- | --- |
| `OQ-ADAPTER-CONNECTION` | connection failed or lost |
| `OQ-ADAPTER-AUTH` | authentication/authorization rejected |
| `OQ-ADAPTER-CAPABILITY` | plan requires an unsupported capability |
| `OQ-ADAPTER-CONSTRAINT` | unique/foreign-key/check violation |
| `OQ-ADAPTER-TIMEOUT` | statement timeout |
| `OQ-ADAPTER-CANCELLED` | cancelled |
| `OQ-ADAPTER-SYNTAX` | generated artifact rejected by the engine (adapter bug) |
| `OQ-ADAPTER-DATA` | value conversion failure |
| `OQ-ADAPTER-UNSUPPORTED` | engine/version does not support the operation |

Messages must be redacted: no bound values, no credentials, no full connection
strings. The original driver exception is attached as a cause and is itself
redacted before logging.

## 9. Integration testing requirements

An adapter is not considered implemented until it has:

* a Docker Compose service (or documented equivalent) for every tested version;
* a skipped-by-default test module marked `integration`;
* tests for connect, version detection, inspect, compile, execute, parameter
  binding, error mapping, transaction commit/rollback, and capability report
  consistency;
* at least one end-to-end round trip: Querly source → IR → artifact → execute →
  normalized result;
* a golden artifact test reviewed by a human;
* an injection test proving parameter values cannot alter the artifact;
* a documented compatibility matrix row with real tested versions.

The capability report must be generated from the same source of truth used by
the tests, so it cannot drift from what was tested.

## 10. Adapter review checklist

Before an adapter is listed as supported:

- [ ] No driver import in any core package.
- [ ] Entry point discovery works from a clean install.
- [ ] Capability report covers every universal type and capability.
- [ ] Unsupported features fail loudly and early.
- [ ] No string interpolation of values into artifacts.
- [ ] Errors and logs redact values and credentials.
- [ ] Integration suite passes on every listed version.
- [ ] Compatibility matrix row updated with tested versions.
- [ ] Known limitations documented.

## 11. Reference implementation order

| Phase | Adapters |
| --- | --- |
| 4 | SQLite (fastest local integration), PostgreSQL, MySQL |
| 5 | MongoDB, Redis |
| 6 | Converter uses the Phase 4/5 adapters as both source and target |
| 8 | SQL Server, Oracle, Db2, CockroachDB, MariaDB, Cassandra, ScyllaDB, HBase, Neo4j, Neptune, Elasticsearch, OpenSearch, ClickHouse, Qdrant, Milvus, Weaviate, DynamoDB, Aerospike, CouchDB, DocumentDB, pgvector |

Adapters in Phase 8 are added one at a time. A category being planned is not a
support claim. See `DATABASE_COMPATIBILITY.md`.
