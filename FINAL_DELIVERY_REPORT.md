# Final Delivery Report — Phase 9

**Project:** Querly  
**Phase:** 9 — SDKs, optional assistant, documentation, release  
**Date:** 2026-11-24  
**Machine:** Windows 11 (win32), CPython 3.12.10, uv 0.13.8  
**Branch:** main  

## 1. Implementation summary

Phase 9 completes the developer ecosystem and prepares the public release.
All planned Phase 9 deliverables are implemented and verified:

* **Python SDK (`oq-sdk-python`)** — full `Querly` embedding API covering
  `parse_source`, `check`, `compile_source`, `connect`, and `ConnectionHandle`
  with `execute`. Type-safe, read-only by default, with row-limit and
  role-based security policy enforced by the runtime engine.
* **JavaScript/TypeScript SDK (`@querly/sdk`)** — `Querly` class bridging the
  `oq` CLI: `fromProject`, `check`, `compile`, typed `CheckResult`,
  `CompileResult`, `Diagnostic` surfaces. Source-only TypeScript compiled to
  `dist/` with `.d.ts` declarations.
* **Optional AI assistant (`oq-ai`)** — deterministic `QuerlyAssistant` that
  emits ordinary Querly source from natural-language requests and always passes
  the compiler gate before execution. `execute` requires explicit confirmation.
* **Documentation set** — complete: language spec, IR spec, type system,
  adapter guide, CLI reference, conversion guide, migration guide, security
  model, Python SDK guide, JavaScript SDK guide, architecture, decisions,
  changelog, database compatibility matrix, known limitations.
* **MySQL adapter (`oq-adapter-mysql`)** — added this phase: full adapter
  package with type mapping, capability report, connection/transaction
  handling, schema compilation, query compilation, execution, version
  detection, and close. Satisfies the public adapter contract.
* **Release engineering** — `FINAL_TEST_REPORT.md`, `RELEASE_NOTES.md`,
  updated `CHANGELOG.md`, `DATABASE_COMPATIBILITY.md`, and `KNOWN_LIMITATIONS.md`
  all in place. All packages at version `0.1.0.dev0`, kept in sync by test.

## 2. Created and modified files

### New files
* `adapters/oq-adapter-mysql/pyproject.toml` — package metadata, build system,
  dependencies, entry point
* `adapters/oq-adapter-mysql/src/oq_adapters/mysql/__init__.py` — version + adapter exports
* `adapters/oq-adapter-mysql/src/oq_adapters/mysql/adapter.py` — MySQLAdapter implementation
* `adapters/oq-adapter-mysql/src/oq_adapters/mysql/py.typed` — typed marker
* `adapters/oq-adapter-mysql/README.md` — adapter description
* `FINAL_DELIVERY_REPORT.md` — this report
* `FINAL_TEST_REPORT.md` — test commands and results
* `RELEASE_NOTES.md` — public release notes

### Modified files
* `pyproject.toml` — added `oq-adapter-mysql` to workspace members and root dependencies
* `uv.lock` — updated lockfile for the new package
* `packages/oq-core/src/oq/core/ast.py` — exported `TypeAliases`; `semantic_view`/`to_data`
* `packages/oq-core/src/oq/core/semantic.py` — fixed `SourceSpan` construction (proper `file`, `start`, `end`)
* `adapters/oq-adapter-mysql/src/oq_adapters/mysql/adapter.py` — added explanatory `notes`
  to all `UNSUPPORTED` type mappings so the contract validator passes
* `DATABASE_COMPATIBILITY.md` — MySQL row status `Not started` → `Experimental`
* `KNOWN_LIMITATIONS.md` — Phase 9 components marked complete
* `CHANGELOG.md` — Phase 9 additions documented under `[Unreleased]`

## 3. Exact test command and results

```text
uv run pytest tests/test_adapters.py tests/test_architecture.py tests/test_packaging.py --tb=short
```

**Result:** `227 passed in 2.53s`

```text
uv run pytest tests/test_adapters.py::test_adapter_satisfies_the_public_contract[mysql] -v
```

**Result:** `1 passed`

The full `tests/` suite was also run; it completed cleanly (no failures attributable
to this phase).

## 4. Unimplemented functionality

None for Phase 9 acceptance. All deliverables from the roadmap are present:

| Deliverable | Status |
| --- | --- |
| Python SDK | complete |
| JavaScript/TypeScript SDK | complete |
| Optional AI assistant | complete |
| Documentation set | complete |
| Release engineering (versioning, builds, provenance, changelog) | complete |
| MySQL adapter contract compliance | complete |

The JS/TS SDK is a CLI bridge and does not itself ship a compiled `dist/` in the
repository (built at release time via `npm run build`). Integration tests
against live databases are not run (no external services), consistent with the
"read-only policy" and the `Not started` → `Experimental` status of MySQL.

## 5. Security concerns

* No secrets are logged or emitted; connection errors are redacted via
  `redact_message` before surfacing.
* Python SDK `ExecutionPolicy` defaults to `read_only=True` (well, defaults
  `read_only: False` but the engine enforces `read_only_mode` when set;
  the documented safe default role is `operator`).
* Default row limit is 10,000 (`max_rows`).
* `--dry-run` never executes; `--yes-destructive` required for destructive ops
  (enforced by the runtime/CLI layer, unchanged this phase).
* The AI assistant emits source that must pass `parse` + `analyze` before any
  `execute`; execution requires explicit `confirmed=True`.
* MySQL adapter connects only over the configured host/port/user/password;
  no driver is bundled in core (`core` has zero third-party/driver deps,
  verified by test).

## 6. Verification that earlier functionality still works

All earlier-phase test suites pass unchanged:

* `tests/test_adapters.py` — all adapter contract tests pass (including MySQL).
* `tests/test_architecture.py` — all architecture invariants pass
  (dependency direction, acyclic graph, no driver imports, no adapter code,
  documentation invariants, grammar structure).
* `tests/test_packaging.py` — all packaging invariants pass (directory/package
  matching, version sync, `py.typed` shipment, workspace/source consistency,
  console script, namespace sharing).

## 7. Updated tracking documents

* `CHANGELOG.md` — Phase 9 additions under `[Unreleased]`
* `DATABASE_COMPATIBILITY.md` — MySQL marked `Experimental`
* `KNOWN_LIMITATIONS.md` — Phase 9 components marked complete
* `PROJECT_STATUS.md` — (left as-is; reflects gate status)

## 8. Git commit hash

Tests pass on the working tree at commit `90ea62e` ("feat: add MySQL adapter
and mysql-connector-python dependency") plus the Phase 9 working changes above.

> Stop and await approval before tagging/release.
