# Test Report

**Phase:** 0 — specification and repository foundation
**Date:** 2026-11-22
**Machine:** Windows 11 (win32), CPython 3.12.10, uv 0.13.8
**Environment:** clean `.venv` created by `uv sync --all-packages --python 3.12`

This report records commands that were actually run and the output they actually
produced. Numbers here are not estimates.

## 1. Install

```text
uv sync --all-packages --python 3.12
```

Result: success. Twenty-seven packages installed, including all ten Querly
distributions at `0.1.0.dev0` from their workspace paths.

## 2. Test suite

```text
uv run pytest -q
```

Result (final run, after the type-system document and architecture tests were
added):

```text
150 passed in 1.03s
```

Collected files:

| File | Scope |
| --- | --- |
| `tests/test_architecture.py` | dependency direction matches `ARCHITECTURE.md`, acyclic graph, leaf packages, no driver/adapter imports in core, no adapter code, adapter contract and capability metadata present, conversion statuses consistent, type contract complete |
| `tests/test_documentation.py` | required documents, heading/text assertions, compatibility matrix honesty |
| `tests/test_grammar.py` | EBNF parses, ≥90 productions, no undefined references, no unreachable productions, required productions present |
| `tests/test_packaging.py` | package/module map, version sync, namespace package shape, no premature CLI script, no driver dependency in core |
| `packages/oq-core/tests/test_source.py` | encoding, BOM, NUL rejection, line breaks, offsets, spans, Unicode columns, Hypothesis properties |
| `packages/oq-core/tests/test_diagnostics.py` | severity ordering, codes, bag ordering, exception path, exact rendered output and caret alignment |
| `packages/oq-core/tests/test_text.py` | line-break scanning and code-point helpers |

Tests marked `integration` are not part of this count; none exist yet because no
adapter exists.

## 3. Lint

```text
uv run ruff check .
```

Result:

```text
All checks passed!
```

## 4. Format

```text
uv run ruff format --check .
```

Result:

```text
53 files already formatted
```

## 5. Types

```text
uv run mypy
```

Result:

```text
Success: no issues found in 5 source files
```

Strict mode applies to `packages/oq-core/src/oq/core`. `mypy_path` and
`explicit_package_bases` are set so mypy can resolve the PEP 420 namespace
correctly; without them mypy reports the same module under two names.

## 6. Package builds and isolated install

```text
uv build --all-packages
```

Result: all ten distributions built, each producing an sdist and a
`py3-none-any` wheel:

```text
Successfully built dist\oq_ai-0.1.0.dev0.tar.gz
Successfully built dist\oq_ai-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_cli-0.1.0.dev0.tar.gz
Successfully built dist\oq_cli-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_compiler-0.1.0.dev0.tar.gz
Successfully built dist\oq_compiler-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_converter-0.1.0.dev0.tar.gz
Successfully built dist\oq_converter-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_core-0.1.0.dev0.tar.gz
Successfully built dist\oq_core-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_ir-0.1.0.dev0.tar.gz
Successfully built dist\oq_ir-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_migrations-0.1.0.dev0.tar.gz
Successfully built dist\oq_migrations-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_runtime-0.1.0.dev0.tar.gz
Successfully built dist\oq_runtime-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_sdk_python-0.1.0.dev0.tar.gz
Successfully built dist\oq_sdk_python-0.1.0.dev0-py3-none-any.whl
Successfully built dist\oq_types-0.1.0.dev0.tar.gz
Successfully built dist\oq_types-0.1.0.dev0-py3-none-any.whl
```

Because the packages are not published, a consumer cannot install them from an
index. The equivalent check is a clean virtual environment fed only by the built
wheels:

```text
uv venv %TEMP%\oq-install-check --python 3.12
uv pip install --python %TEMP%\oq-install-check\Scripts\python.exe \
  --no-index --find-links dist oq-cli oq-sdk-python oq-ai
```

Result: all ten distributions installed into one environment. Importing
`oq.core`, `oq.ir`, `oq.types`, `oq.compiler`, `oq.runtime`, `oq.converter`,
`oq.migrations`, `oq.cli`, `oq.sdk`, and `oq.ai` succeeded from
`site-packages\oq\...`, and no top-level `oq\__init__.py` exists, confirming the
namespace-package layout survives packaging and installation.

The `oq-core` wheel contains `oq/core/__init__.py`, `_text.py`,
`diagnostics.py`, `source.py`, `version.py`, `py.typed`.

## 7. Failures found and fixed during Phase 0 verification

Recorded because the report must not pretend the first run was green.

| Problem | Resolution |
| --- | --- |
| `uv sync` selected Python 3.14, which is not the supported minimum | added `.python-version` with `3.12` and pinned `--python 3.12` |
| Hatchling `packages = [...]` produced editable installs that did not expose `oq` | switched every package to `sources = ["src"]` plus `only-include` for its subpackage |
| Grammar test stripped EBNF special sequences with a regular expression, mangling the `"?"` optional-marker terminal and `"*/"` | terminal-protection pass before comment/special-sequence removal |
| Grammar used terminals containing a bare `"` (the apostrophe), which broke quote-aware parsing | quoted characters are expressed as special sequences |
| Diagnostic renderer misaligned the gutter bar | gutter width derived from the line-number width, verified by exact-output test |
| Unicode test asserted a wrong offset for an emoji column | corrected to code-point semantics with explicit offsets |
| `pytest.raises(match=...)` patterns contained unescaped `.` | made raw strings with escaped dots |
| mypy resolved namespace modules twice | `explicit_package_bases` and `mypy_path` configured |
| ADR-0004's canonical type list omitted `float` while the specification defines it | ADR corrected and now links the type-system contract; a test compares the documents |

## 8. Regression check for earlier functionality

Phase 0 is the first phase, so there is no earlier phase to regress. Within
Phase 0, the source and diagnostic modules were re-verified after every change
listed in section 7: their tests were part of each `pytest` run, and the final
run includes all of them. Every package was also imported after the packaging fix
(`oq.core`, `oq.ir`, `oq.types`, `oq.sdk`) to prove the namespace still resolves.

## 9. What this report does not prove

* No test exercises a lexer, parser, compiler, adapter, runtime, CLI, or SDK,
  because none exists.
* No test connects to a database.
* Results are from one Windows machine. CI is configured for Linux, macOS,
  Windows, and Python 3.13, but hosted CI has not run yet.
* Coverage is not used as a quality gate at this phase; the foundation modules
  are small and exercised example-by-example plus by Hypothesis properties.
