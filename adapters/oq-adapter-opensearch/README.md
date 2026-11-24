# oq-adapter-opensearch

OpenSearch adapter (search DSL) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_opensearch_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `opensearch-py>=2.6` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-opensearch[driver]
```

Supported IR subset: schema, search. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
