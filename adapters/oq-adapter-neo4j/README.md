# oq-adapter-neo4j

Neo4j adapter (Cypher) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_neo4j_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `neo4j>=5.20` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-neo4j[driver]
```

Supported IR subset: schema, node CRUD. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
