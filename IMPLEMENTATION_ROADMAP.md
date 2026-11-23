# Implementation Roadmap

Each phase ends at a hard gate. A later phase does not start until the previous
gate is reported and approved.

## Phase 0 — Specification and repository foundation

**Status: complete, awaiting approval.**

Deliverables:

* workspace of independently installable packages sharing the `oq` namespace;
* normative language specification, formal EBNF grammar, and universal type-system contract;
* IR, adapter, CLI, security, conversion, migration, and SDK designs;
* source file model and diagnostics foundation with tests;
* packaging, versioning, documentation, and grammar invariants enforced by
  tests;
* CI for Linux, Windows, and macOS on Python 3.12 and 3.13.

**Acceptance gate**

- [x] `uv sync --all-packages` installs every package.
- [x] `ruff check`, `ruff format --check`, and `mypy` pass.
- [x] `pytest` passes on the development machine.
- [x] Every required tracking document exists.
- [x] Dependency direction is documented and test-enforced.
- [x] Type-system design is documented and covers every canonical type.
- [x] Adapter contract and capability metadata are specified.
- [x] Grammar has no undefined or unreachable nonterminals.
- [x] No database support is advertised.
- [x] No placeholder implementation is presented as working.
- [ ] Approval to start Phase 1.

## Phase 1 — Lexer and parser

Deliverables:

* token model with spans, trivia handling, and `newline` significance;
* deterministic lexer for all literal forms (including the date/timestamp
  maximal-munch rule);
* recursive-descent parser for both surface forms;
* AST spanning both forms, with local error recovery;
* parser and lexer test suites, including Hypothesis round-trips from tokens to
  spans.

**Acceptance gate**: every example in the specification parses into the expected
AST shape; both forms produce structurally equivalent ASTs; malformed input
produces positioned diagnostics without crashes; no semantic analysis yet.

## Phase 2 — Universal types, AST, and IR

Deliverables:

* universal type models and validation;
* semantic AST node definitions shared by both forms;
* Pydantic IR models for schema, query, transaction, migration, plan, and
  compatibility documents;
* deterministic, versioned JSON serialization;
* AST/semantic-model/IR separation enforced by types and tests.

**Acceptance gate**: IR documents validate and round-trip; invalid documents are
rejected; type mapping tables exist for the Phase 4 engines; no engine SQL in the
IR.

## Phase 3 — Semantic analyzer and first compiler

Deliverables:

* name, type, constraint, relationship, and parameter resolution;
* normalization to canonical IR for equivalent English-like and structured
  programs;
* capability analysis against a capability document;
* golden tests for diagnostics and normalized IR;
* `check` and `compile` library entry points (not the CLI yet).

**Acceptance gate**: specification examples check cleanly; equivalent programs
produce byte-identical IR; deliberate errors produce the specified codes;
capability gaps fail compilation.

## Phase 4 — PostgreSQL, MySQL, and SQLite adapters

Deliverables:

* adapter contract implementation in `oq-runtime`;
* three adapters with connection, inspection, DDL and DML compilation,
  parameter binding, transactions, error mapping, and capability reports;
* Docker Compose integration suite against real database versions;
* SQL injection and redaction tests;
* first end-to-end execution: source → IR → artifact → execute → normalized
  result.

**Acceptance gate**: every listed capability has a passing integration test;
inspection round-trips a nontrivial schema; generated artifacts are reviewed
golden files; credentials never appear in logs.

## Phase 5 — MongoDB and Redis adapters

Deliverables:

* document and key-value capability profiles;
* schema-on-read handling with explicit universal types;
* aggregation/command compilation and result normalization;
* integration tests against real services.

**Acceptance gate**: unsupported relational constructs fail with clear
diagnostics instead of being emulated silently; capability reports are honest
about transaction and constraint limits.

## Phase 6 — Conversion and migration engine

Deliverables:

* schema conversion between supported engines with compatibility reports;
* query conversion with target validation;
* profiles (`strict`, `compatible`, `best-effort`);
* migration planning, applied-state checksums, and verification;
* migration execution with transactional/non-transactional handling.

**Acceptance gate**: statuses are proven per construct; round-trip tests do not
claim losslessness; migrations are verified against inspected post-state; failed
migrations are never reported as applied.

## Phase 7 — Runtime security and CLI

Deliverables:

* read-only policy, destructive confirmation, row limits, timeouts,
  cancellation, audit logging, and secret providers;
* the full `oq` command set with documented exit codes and `--json` output;
* project configuration file;
* CLI tests on Windows, Linux, and macOS;
* security test suite for injection, redaction, and policy refusal.

**Acceptance gate**: policy refusal happens before execution; no secret appears
in output or logs; `--dry-run` never executes; CLI behavior matches the
reference.

## Phase 8 — Additional adapters

Deliverables, one engine at a time:

* SQL Server, Oracle, IBM Db2, CockroachDB, MariaDB;
* Cassandra, ScyllaDB, HBase;
* Neo4j, Amazon Neptune;
* Elasticsearch, OpenSearch, ClickHouse;
* pgvector, Qdrant, Milvus, Weaviate;
* DynamoDB, Aerospike, CouchDB, Amazon DocumentDB.

**Acceptance gate per adapter**: contract tests, real integration tests on the
listed versions, capability report, compatibility matrix entry, and an explicit
list of unsupported constructs. An adapter is never merged as "planned but
working".

## Phase 9 — SDKs, optional assistant, documentation, release

Deliverables:

* Python SDK and published JavaScript/TypeScript SDK (bridge decision resolved
  by ADR and benchmark);
* optional AI assistant that emits ordinary Querly source and cannot bypass the
  compiler;
* complete documentation set including tutorials and examples that run in CI;
* release engineering: versioning, package builds, provenance, changelog.

**Acceptance gate**: SDK examples are executable tests; assistant output is
validated by the deterministic pipeline; all docs match behavior; release
artifacts are reproducible.

## Gate reporting format

After every phase, report:

1. implementation summary;
2. created and modified files;
3. exact test command and results;
4. unimplemented functionality;
5. security concerns;
6. verification that earlier functionality still works;
7. updated tracking documents;
8. git commit hash when tests pass;
9. stop and await approval.