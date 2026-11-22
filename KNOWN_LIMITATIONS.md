# Known Limitations

This file is the single place where Querly admits what it does not do. It is
updated at every phase gate. A limitation listed here is not a bug report; it is
a scope boundary that must not be papered over in documentation or output.

## Current phase

Phase 0 (specification and repository foundation) is complete and awaiting
approval. The repository contains specifications, packaging, and a tested source
and diagnostic foundation. It does not contain a working language toolchain.

## Not implemented at all

| Component | Package | Planned phase |
| --- | --- | --- |
| Lexer | `oq-core` | 1 |
| Parser and AST | `oq-core` | 1 |
| Semantic analyzer | `oq-core` | 3 |
| Universal type models | `oq-types` | 2 |
| IR models and serialization | `oq-ir` | 2 |
| Compiler, normalization, capability analysis | `oq-compiler` | 3 |
| Database Adapter (any engine) | `adapters/*` | 4+ |
| Runtime connections and execution | `oq-runtime` | 7 |
| Conversion engine | `oq-converter` | 6 |
| Migration engine | `oq-migrations` | 6/7 |
| CLI commands, including `oq` itself | `oq-cli` | 7 |
| Python SDK | `oq-sdk-python` | 9 |
| JavaScript/TypeScript SDK | `sdk/javascript` | 9 |
| Optional AI assistant | `oq-ai` | 9 |

The Python packages other than `oq-core` are installable scaffolds. Their
`__init__` modules state this explicitly and export no working API.

## Language limitations in version 0.1

These are deliberate and documented in the specification:

* No subqueries, common table expressions, unions, `distinct`, window functions,
  or grouping sets.
* No referential actions (`on delete cascade`, `on update ...`) and no deferred
  constraints.
* No partial indexes, expression indexes, or index methods.
* No `update ... from`, `insert ... select`, upserts, or bulk import syntax.
* No stored procedures, triggers, or user-defined functions.
* No database namespaces beyond import aliasing.
* English-like syntax exists for schemas, queries, and transactions, but not for
  migrations or conversion requests.
* English-like schema syntax cannot express `text(n)`, `binary(n)`, or
  `map<K, V>` field types; use the structured form for those.
* A database conversion request names adapters but does not move data.
* Isolation levels and retry policies are runtime configuration, not syntax.

## Conversion limitations (designed, not yet implemented)

* No source-engine SQL ingestion in version 0.1; query conversion starts from
  Querly source.
* Data migration is out of scope; schema conversion produces DDL and a plan.
* `exact` status requires a proof and therefore will often be unavailable for
  engine-specific type details.

## Security limitations

* Native database permissions remain the primary control. Querly policy is
  defence in depth, not a sandbox around a malicious database server.
* An adapter with a driver vulnerability is a supply-chain risk; adapters and
  drivers are pinned and reported.
* Audit logging cannot prevent an authorized user from writing data; it records
  actions.
* Secret providers are limited to environment and documented providers; there is
  no built-in vault.

## Open design decisions

* JavaScript SDK bridge: CLI process, local sidecar, or WASM. See
  `DECISIONS.md` (ADR-0012). Unresolved until Phase 9.
* Enum types and `set`/`map` literal representation in IR format 0.1
  (Phase 2).
* Multi-artifact plans for engines that need multi-statement DDL (Phase 2).

## Platform limitations

* Development and Phase 0 verification ran on Windows with Python 3.12.10. CI
  is configured for Linux, macOS, and Python 3.13, but those jobs have not yet
  run in a hosted environment in this repository.
* Integration tests require Docker; they are not part of the default suite.

## Claims we explicitly do not make

* Querly does not support any database yet.
* Querly does not parse or compile any program yet.
* Querly does not convert anything yet.
* Querly does not ship a CLI yet.
* No compatibility matrix row is marked supported or tested.
