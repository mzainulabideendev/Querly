# Project Status

**Project:** Querly — One Language. Many Databases.
**Implementation language:** Python 3.12+
**License:** MIT
**Current phase:** Phase 0 — complete, awaiting approval
**Date of this report:** 2026-11-22
**Development platform for verification:** Windows, Python 3.12.10

## Phase summary

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Specification and repository foundation | Complete, awaiting approval |
| 1 | Lexer and parser | Not started |
| 2 | Universal types, AST, IR | Not started |
| 3 | Semantic analyzer and compiler | Not started |
| 4 | PostgreSQL, MySQL, SQLite adapters | Not started |
| 5 | MongoDB and Redis adapters | Not started |
| 6 | Conversion and migration engine | Not started |
| 7 | Runtime security and CLI | Not started |
| 8 | Additional adapters | Not started |
| 9 | SDKs, optional assistant, release | Not started |

## What works today

Only the Phase 0 foundation. It is real, tested code, not a simulation:

1. **Workspace packaging.** Ten independently installable distributions share
   the `oq` namespace. Versions are kept in sync by a test. The CLI registers no
   console script because it does not work yet.
2. **Source model** (`oq.core.source`). Reads UTF-8 with optional BOM, rejects
   NUL content, maps offsets to line/column for `\n`, `\r\n`, `\r`, and mixed
   files, counts Unicode code points, and creates half-open spans. Verified by
   example tests and Hypothesis properties.
3. **Diagnostics** (`oq.core.diagnostics`). Stable `OQ-AREA-NNN` codes,
   severities, immutable values, an ordered bag with deterministic sorting, and
   compiler-style rendering with source snippets and notes.
4. **Specifications.** A normative language specification, formal EBNF grammar,
   universal type-system contract, IR format, adapter contract, CLI contract,
   security model, conversion model, migration model, and SDK designs.
5. **Architecture invariants.** Tests enforce the documented dependency
   direction, package versions, namespace layout, absence of driver imports and
   adapter code, presence of required documents, grammar well-formedness, and
   that no database is advertised as supported.

## What does not work

Everything else, explicitly:

* No lexer, parser, AST, semantic analyzer, compiler, or IR model.
* No adapter, connection, execution, transaction, inspection, or migration.
* No working `oq` command, Python SDK, JavaScript SDK, or AI assistant.
* No database is supported. Every row of `DATABASE_COMPATIBILITY.md` is
  `Not started`.
* English-like migrations and conversion requests are not in language 0.1.
* Language 0.1 omits subqueries, CTEs, unions, `distinct`, window functions,
  referential actions, and partial indexes.

See `KNOWN_LIMITATIONS.md` for the complete list.

## Phase 0 acceptance evidence

| Criterion | Evidence |
| --- | --- |
| Every package installs and imports | `tests/test_packaging.py` |
| Versions cannot drift | `test_package_version_matches_core_version` |
| Core has no driver dependency | `test_core_has_no_third_party_or_driver_dependencies` |
| Namespace sharing is intact | `test_namespace_package_has_no_top_level_init` |
| Grammar is structurally sound | `tests/test_grammar.py` |
| Dependency direction matches `ARCHITECTURE.md` | `tests/test_architecture.py` |
| No core package imports a driver or adapter | `test_no_core_package_imports_an_adapter_or_driver` |
| Type-system contract covers every canonical type | `test_type_system_document_covers_every_canonical_type` |
| Required docs exist | `tests/test_documentation.py` |
| No database support is claimed | `test_compatibility_matrix_claims_nothing_implemented_in_phase_zero` |
| Source mapping is correct | `packages/oq-core/tests/test_source.py` |
| Diagnostics render correctly | `packages/oq-core/tests/test_diagnostics.py` |
| Lint and types | `ruff check`, `ruff format --check`, `mypy` (strict on `oq-core`) |

Exact commands and results are recorded in `TEST_REPORT.md`.

## Security posture at this phase

No database connection exists, so no credential, injection, or execution risk is
present in the shipped code. The security design in
`docs/security/SECURITY.md` is a requirement for later phases, not an
implemented control. The only inputs processed today are local source files,
which are size-bounded by the caller and rejected if they contain NUL bytes.

Open security work for later phases: parameter binding tests, policy
enforcement, redaction, audit logging, secret providers, and supply-chain
pinning. None is claimed done.

## Known decisions awaiting resolution

* JavaScript SDK bridge (ADR-0012, Phase 9).
* IR details for enums, `set`/`map` literals, naive timestamps, and multi-artifact
  plans (Phase 2).

## Next action

Await approval of Phase 0. On approval, start Phase 1 (lexer and parser) and do
not begin Phase 2 until Phase 1 passes its gate.
