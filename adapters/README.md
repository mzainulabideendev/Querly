# Adapters

This directory will contain independently installable adapter distributions,
one per database engine, each named `oq-adapter-<engine>`.

**Status: Phase 0.** This directory contains no adapter code yet. No database is
supported by Querly at this time. The contract adapters must implement is in
[`docs/adapters/ADAPTER_GUIDE.md`](../docs/adapters/ADAPTER_GUIDE.md), and the
honest current support status is in
[`DATABASE_COMPATIBILITY.md`](../DATABASE_COMPATIBILITY.md).

Planned layout:

```text
adapters/
  oq-adapter-sqlite/
    pyproject.toml
    src/oq_adapters/sqlite/
    tests/
  oq-adapter-postgres/
  oq-adapter-mysql/
  ...
```

Rules:

1. Adapters depend on the Querly contract packages, never the reverse.
2. Adapters are discovered through the `oq.adapters` entry point group.
3. An adapter that has not passed integration tests against a real database is
   not listed as supported anywhere in the project.
4. Every adapter ships a machine-readable capability report and a tested-version
   list.
