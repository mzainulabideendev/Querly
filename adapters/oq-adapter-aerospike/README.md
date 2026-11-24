# oq-adapter-aerospike

Aerospike adapter (native KV) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_aerospike_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `aerospike>=7.0` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-aerospike[driver]
```

Supported IR subset: schema, record CRUD. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
