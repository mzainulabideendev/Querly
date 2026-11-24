# oq-adapter-neptune

Amazon Neptune adapter (Gremlin) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_neptune_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `gremlinpython>=3.7` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-neptune[driver]
```

Supported IR subset: schema, traversal. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
