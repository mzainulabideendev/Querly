# oq-adapter-dynamodb

Amazon DynamoDB adapter (native API) for Querly.

Status: **Experimental**. A real implementation exists, but no engine version is
claimed as tested until `tests/integration/test_dynamodb_integration.py` passes against
the versions in `docs/adapters/TEST_MATRIX.md`.

Driver: `boto3>=1.34` (optional extra `driver`). Importing the adapter never opens a
connection.

```bash
pip install oq-adapter-dynamodb[driver]
```

Supported IR subset: schema, item CRUD. Everything else is reported through
`AdapterDiagnostic` rather than emulated. See `DATABASE_COMPATIBILITY.md`.
