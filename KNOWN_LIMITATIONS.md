# Known Limitations

This file is the single place where Querly admits what it does not do. It is
updated at every release. A limitation listed here is not a bug report; it is a
scope boundary that must not be papered over in documentation or output.

## Current release

**Pre-release `0.1.0.dev0`.** The workspace installs, the CI suite passes, and a
standalone `oq` binary is published for Linux, macOS, and Windows. The language
pipeline is not complete, so the tool is not usable for real database work yet.

## Language and toolchain limitations

The lexer, token definitions, source model, and diagnostics are implemented and
tested. Everything after lexing is incomplete:

* **Grammar and lexer are not connected.** `terminals.lark` declares its terminals
  instead of defining regexes, because `oq.core.tokens.Lexer` is the only token
  implementation. `oq.core.parser` compiles the grammar with Lark's default lexer
  and never feeds it the hand-written tokens, so `oq check` and `oq compile`
  report `OQ-PARSE-001` (`unexpected character`) for ordinary source files.
* **Lark tree to AST conversion is a stub.** Schema declarations produce empty
  attribute, index, and unique-constraint tuples; query, migration, and
  conversion clauses are not extracted. Even a successful parse would not yield a
  usable AST.
* **Semantic analysis and SQL compilation are not validated end to end.** The
  modules exist, but no test starts from source text and checks generated SQL,
  because parsing never succeeds. `mypy` is disabled for
  `oq.core.semantic`, whose model does not yet match `oq.core.ast`.
* **No parser, compiler, runtime, converter, migration, or adapter behavior is
  covered by a source-to-output test.** The passing suite covers packaging,
  documentation invariants, grammar/token drift, diagnostics, and adapter
  capability reports.
* **Python packages other than `oq-core` are installable scaffolds.** Their
  `__init__` modules say so and export no stable API. This includes
  `oq-sdk-python`, `oq-ai`, `oq-compiler`, `oq-runtime`, `oq-converter`, and
  `oq-migrations`.
* **`oq` CLI.** `oq version` and `oq --help` work. `oq check` and `oq compile`
  exist but currently only produce parse diagnostics. Exit codes are stable
  (`0` success, `1` diagnostics, `2` usage/file error).

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

## Conversion limitations (designed, not implemented)

* No source-engine SQL ingestion in version 0.1; query conversion is specified to
  start from Querly source.
* Data migration is out of scope; schema conversion is specified to produce DDL
  and a plan.
* `exact` status requires a proof and therefore will often be unavailable for
  engine-specific type details.
* No conversion report is produced today: the conversion engine is a scaffold.

## Security limitations

* Native database permissions remain the primary control. Querly policy is
  defence in depth, not a sandbox around a malicious database server.
* An adapter with a driver vulnerability is a supply-chain risk; adapters and
  drivers are pinned and reported.
* Audit logging cannot prevent an authorized user from writing data; it records
  actions.
* Secret providers are limited to environment and documented providers; there is
  no built-in vault.
* No execution policy is enforced today, because execution is not wired end to
  end.

## Database and adapter limitations

* Every adapter directory is a real, installable distribution, and its capability
  report is checked for internal consistency. That is all the test suite proves.
* No adapter is exercised against a live engine in CI. Integration tests require
  Docker and the engine-specific driver and are skipped when those are absent.
* No row in `DATABASE_COMPATIBILITY.md` is marked supported or production ready.
* The MariaDB driver (`mariadb>=1.1`) builds native code and is an optional
  `driver` extra. Without it, MariaDB integration tests skip.
* Adapters do not execute queries end to end, because the parser and compiler do
  not produce executable plans yet.

## Platform and release limitations

* CI runs lint, formatting, type checking, the unit suite, and package builds on
  Linux, macOS, and Windows with Python 3.12 and 3.13.
* Standalone `oq` binaries are built by `.github/workflows/release.yml` for Linux
  x86_64, macOS Intel, macOS Apple Silicon, and Windows x86_64. There is no
  binary for other architectures, and no installer or package-manager entry.
* The binaries embed the `0.1.0.dev0` version string; versioning is tied to the
  package metadata, not derived from the release tag.
* macOS binaries are unsigned and not notarized, so Gatekeeper warns on first
  run.
* The Python packages are not published to PyPI. Installation is from wheels on
  the release page or from a source checkout.

## Claims we explicitly do not make

* Querly does not parse or compile any program yet.
* Querly does not convert or migrate anything yet.
* Querly does not execute queries against any database yet.
* Querly does not support any database yet.
* The CLI ships, but only `oq version` and `oq --help` are functional.
* No compatibility matrix row is marked supported or tested.
