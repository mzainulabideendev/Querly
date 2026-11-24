# oq-adapter-clickhouse

ClickHouse adapter (analytical SQL) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_clickhouse_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `clickhouse-connect>=0.7` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-clickhouse[driver]
```

Supported IR subset: schema, select. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
