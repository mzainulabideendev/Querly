# oq-adapter-couchdb

Apache CouchDB adapter (HTTP/Mango) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_couchdb_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `pycouchdb>=1.14` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-couchdb[driver]
```

Supported IR subset: schema, document CRUD. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
