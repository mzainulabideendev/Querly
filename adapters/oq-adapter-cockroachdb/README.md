# oq-adapter-cockroachdb

CockroachDB adapter (PostgreSQL wire) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_cockroachdb_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `psycopg[binary]>=3.1` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-cockroachdb[driver]
```

Supported IR subset: schema, select. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
