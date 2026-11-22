# Changelog

All notable changes to Querly are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) from 1.0. Before 1.0, minor versions
may contain breaking changes and will say so.

## [Unreleased]

### Added — Phase 0 (specification and repository foundation)

* Monorepo workspace with ten independently installable Python distributions
  sharing the `oq` namespace: `oq-core`, `oq-ir`, `oq-types`, `oq-compiler`,
  `oq-runtime`, `oq-converter`, `oq-migrations`, `oq-cli`, `oq-sdk-python`, and
  `oq-ai`.
* Normative language specification (version 0.1 draft) covering both
  English-like and structured syntax, the universal type system, queries,
  mutations, transactions, migrations, conversion, diagnostics, and conformance
  requirements.
* Formal EBNF grammar with a validated structure: no undefined or unreachable
  nonterminals, both syntax modes covered, and explicit profiles for conversion.
* Universal IR specification with versioned document envelope, canonical types
  and operators, schema/query/transaction/migration/plan/compatibility
  documents, and versioning rules.
* Adapter development guide with the capability report format, inspection,
  compilation, execution, error normalization, and integration-test obligations.
* Architecture document defining dependency direction, pipeline stages, AST/IR
  separation, runtime security boundary, and testing strategy.
* CLI reference with command contracts, exit codes, configuration format, and
  secret handling rules.
* Security model, conversion guide, migration guide, and Python and JavaScript
  SDK designs.
* Source file model: UTF-8 with BOM handling, NUL rejection, CRLF/LF/CR and
  mixed line-break support, Unicode code-point columns, half-open spans.
* Diagnostics: stable `OQ-AREA-NNN` codes, severities, immutable values,
  diagnostic bags with deterministic ordering, and compiler-style rendering with
  source snippets.
* Test suite covering source mapping (including Hypothesis properties),
  diagnostics, packaging invariants, documentation invariants, and grammar
  structure.
* CI configuration for Linux, Windows, and macOS on Python 3.12 and 3.13, plus
  distribution builds.
* Docker Compose manifest for disposable integration databases (not yet used).

### Notes

* No database adapter, compiler, runtime, CLI command, or SDK is implemented.
  See `KNOWN_LIMITATIONS.md`.
* All version numbers are `0.1.0.dev0` and are kept in sync by a test.
