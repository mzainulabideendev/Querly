# oq-adapter-milvus

Milvus adapter (vector search) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_milvus_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `pymilvus>=2.4` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-milvus[driver]
```

Supported IR subset: schema, vector search. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
