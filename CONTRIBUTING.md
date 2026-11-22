# Contributing to Querly

Querly is built in phases with explicit acceptance gates. Contributions are
welcome, but a change that claims more than it verifies will be rejected.

## Before you start

Read:

1. [`README.md`](README.md) for the current state.
2. [`DECISIONS.md`](DECISIONS.md) for binding architecture decisions.
3. [`PROJECT_STATUS.md`](PROJECT_STATUS.md) and
   [`IMPLEMENTATION_ROADMAP.md`](IMPLEMENTATION_ROADMAP.md) for what is in scope.
4. [`docs/language/SPECIFICATION.md`](docs/language/SPECIFICATION.md) if you are
   touching the language.

Do not add support claims for a database, platform, or feature that lacks
passing tests. Do not mark a scaffold as complete. Do not add placeholder code
and describe it as working.

## Setup

```text
uv sync --all-packages
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Python 3.12 or newer is required. Integration tests need Docker and are run with
`-m integration` once adapters exist.

## Repository rules

* Code lives in the smallest package that owns the responsibility. The
  dependency direction in [`ARCHITECTURE.md`](ARCHITECTURE.md) is enforced by
  review and by packaging tests.
* `oq-core` and `oq-types` must not gain third-party runtime dependencies
  without an ADR.
* No database driver may be imported by a core package. Drivers belong to
  adapters.
* The AST, semantic model, IR, and physical plan are separate. Do not merge
  layers to save code.
* Every new public behavior needs a test. Every new diagnostic needs a stable
  code and a rendering test.
* Type changes must update
  [`docs/types/TYPE_SYSTEM.md`](docs/types/TYPE_SYSTEM.md) and both surface
  syntaxes at once; the two forms may never disagree about a type's meaning.
* Update the relevant documentation in the same change. `TEST_REPORT.md`,
  `PROJECT_STATUS.md`, `CHANGELOG.md`, and `KNOWN_LIMITATIONS.md` are updated at
  phase boundaries.

## Style

* Python 3.12 syntax, full type annotations, `from __future__ import
  annotations`.
* Line length 100. Ruff is authoritative for lint and formatting.
* Mypy runs in strict mode on `oq-core` today and will expand as packages gain
  implementations.
* Prefer immutable dataclasses for value objects. Validate in `__post_init__`
  and raise `ValueError` with a specific message.
* Never swallow exceptions. Wrap with context or let them propagate.
* Never interpolate untrusted values into generated SQL. The core language has no
  such path; do not add one.

## Tests

* Use pytest. Use Hypothesis for properties, not for examples that should be
  explicit.
* Name tests by behavior, not by implementation.
* Do not weaken an assertion or delete a test to make a change pass.
* Golden files are reviewed artifacts. Commit a golden artifact only after it has
  been executed successfully against a real engine.
* Mark tests that need external services with `@pytest.mark.integration`.
* Security tests must include adversarial input and assert redaction.

## Adapters

Follow [`docs/adapters/ADAPTER_GUIDE.md`](docs/adapters/ADAPTER_GUIDE.md). An
adapter contribution is acceptable when it has:

* a real integration suite against listed engine versions;
* an honest capability report;
* error normalization and redaction tests;
* a compatibility matrix update;
* a documented list of unsupported constructs.

## Commits

Use conventional commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`,
`build:`, `chore:`. Keep a commit focused. Phase-gate commits include the phase
number, for example:

```text
feat(phase-0): repository foundation, language spec, diagnostics
```

Do not commit secrets, generated databases, `.venv`, or test credentials beyond
the documented disposable Compose values.

## Reporting issues

For bugs, include the Querly version (`oq version` once available), the operating
system, the exact command, the expected behavior, and the actual output. Redact
credentials and data.

For security issues, do not open a public issue. Follow
[`docs/security/SECURITY.md`](docs/security/SECURITY.md).

## License

By contributing, you agree that your contribution is licensed under the MIT
License in [`LICENSE`](LICENSE).
