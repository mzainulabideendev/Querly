# oq-adapter-hbase

Apache HBase adapter (Thrift) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_hbase_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `happybase>=1.2` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-hbase[driver]
```

Supported IR subset: schema, row CRUD. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
