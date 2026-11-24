# Release Notes — Querly 0.1.0.dev0 (Phase 9)

**Status:** pre-release / development snapshot. Not yet tagged. Awaiting approval.

Querly is a database-independent programming language and toolchain. This
snapshot completes the Phase 9 developer ecosystem and prepares the public
release. It is **not** a stable 1.0 release: APIs, grammar, and adapter
behavior may change before 1.0. See `KNOWN_LIMITATIONS.md`.

## Highlights

* **Python SDK** (`oq-sdk-python`) — embed parsing, semantic checking, and
  compilation in Python. `Querly.check`, `Querly.compile_source`,
  `Querly.connect` + `ConnectionHandle.execute`, with an `ExecutionPolicy`
  (read-only, row-limit, role-based) enforced by the runtime.
* **TypeScript/JavaScript SDK** (`@querly/sdk`) — a thin, typed bridge over the
  `oq` CLI: `Querly.fromProject`, `check`, `compile`, with typed
  `Diagnostic`/`CheckResult`/`CompileResult` surfaces.
* **Optional AI assistant** (`oq-ai`) — a deterministic, rules-based assistant
  that turns natural-language requests into ordinary Querly source. Generated
  source always passes the compiler gate; execution requires explicit
  confirmation. The assistant never bypasses the compiler.
* **MySQL adapter** (`oq-adapter-mysql`) — new adapter package implementing the
  public adapter contract: type mapping, capability report, connection and
  transaction handling, schema/query compilation, execution, and version
  detection. Status: **Experimental** (unit-tested; not verified against a live
  server in CI).
* **Complete documentation** — language and grammar spec, IR spec, type system,
  adapter guide, CLI reference, conversion and migration guides, security
  model, Python and JavaScript SDK guides, architecture, decisions, changelog,
  database compatibility matrix, and known limitations.
* **Release engineering** — consistent versioning across all distributions
  (`0.1.0.dev0`, test-enforced), reproducible builds (hatchling sdists/wheels,
  PEP 420 `oq` and `oq_adapters` namespaces), and a maintained changelog.

## Security

* Read-only execution policy by default for SDK/CLI usage.
* Default row limit of 10,000; timeouts and allowed-role enforcement.
* Secrets never logged or emitted; connection errors are redacted.
* `--dry-run` never executes; destructive operations require `--yes-destructive`.
* AI-generated source must pass `parse` + `analyze` before execution.

## Compatibility

See `DATABASE_COMPATIBILITY.md`. All adapters are `Experimental` or
`Not started`; none are yet `Supported`.

## Known issues / limitations

See `KNOWN_LIMITATIONS.md`. No live-database integration suites are run in CI
(no external services). The JavaScript SDK is distributed as source and built at
release time (`npm run build`).

## Install (development)

```text
uv sync --all-packages --python 3.12
```

## Verify

```text
uv run pytest tests/test_adapters.py tests/test_architecture.py tests/test_packaging.py
```

Expected: `227 passed`.