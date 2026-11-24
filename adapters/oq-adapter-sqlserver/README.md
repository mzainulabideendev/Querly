# oq-adapter-sqlserver

SQL Server adapter (T-SQL, ODBC) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_sqlserver_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `pyodbc>=5.1` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-sqlserver[driver]
```

Supported IR subset: schema, select. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
