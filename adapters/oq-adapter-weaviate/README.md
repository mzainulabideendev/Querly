# oq-adapter-weaviate

Weaviate adapter (vector + hybrid search) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_weaviate_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `weaviate-client>=4.5` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-weaviate[driver]
```

Supported IR subset: schema, vector search. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
