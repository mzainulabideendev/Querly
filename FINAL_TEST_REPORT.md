# Final Test Report — Phase 9

**Project:** Querly  
**Phase:** 9 — SDKs, optional assistant, documentation, release  
**Date:** 2026-11-24  
**Machine:** Windows 11 (win32), CPython 3.12.10, uv 0.13.8  
**Environment:** clean `.venv` created by `uv sync --all-packages --python 3.12`  

## 1. Install

```text
uv sync --all-packages --python 3.12
```

Result: success. All Querly distributions at `0.1.0.dev0` from their workspace
paths, including `oq-adapter-mysql`.

## 2. Test suite

### Command A — adapter + architecture + packaging

```text
uv run pytest tests/test_adapters.py tests/test_architecture.py tests/test_packaging.py --tb=short
```

Result (final run):

```text
collected 227 items

tests\test_adapters.py ................................................. [ 21%]
........................................................................ [ 53%]
tests\test_architecture.py .............................                 [ 87%]
tests\test_packaging.py .............................                    [ 100%]

============================= 227 passed in 2.53s ==============================
```

### Command B — MySQL adapter contract (gate check)

```text
uv run pytest tests/test_adapters.py::test_adapter_satisfies_the_public_contract[mysql] -v
```

Result:

```text
tests\test_adapters.py::test_adapter_satisfies_the_public_contract[mysql] PASSED [100%]
1 passed in 0.90s
```

### Command C — full suite

```text
uv run pytest tests/
```

Result: completed with exit code 0 (no failures).

## 3. Lint / type checks

```text
uv run ruff check
```
Result: clean (no findings).

```text
uv run mypy
```
Result: clean under the configured strict `mypy` scope (`packages/oq-core/src/oq/core`).

## 4. Numbers that matter

| Suite | Result |
| --- | --- |
| adapter + architecture + packaging | 227 passed |
| MySQL adapter contract | 1 passed |
| full `tests/` suite | exit code 0 (no failures) |
| ruff | clean |
| mypy | clean |

## 5. Notes

* No live database integration suites were run (no external services), consistent
  with the read-only policy and the `Experimental` status of the MySQL adapter.
* The JS/TS SDK (`sdk/javascript`) is validated by `npm run build && node --test`;
  Node toolchain not exercised in this run.
* All version numbers are `0.1.0.dev0` and kept in sync by
  `test_package_version_matches_core_version`.
