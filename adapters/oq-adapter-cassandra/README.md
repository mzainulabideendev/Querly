# oq-adapter-cassandra

Apache Cassandra adapter (CQL) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_cassandra_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `cassandra-driver>=3.29` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-cassandra[driver]
```

Supported IR subset: schema, cql. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
