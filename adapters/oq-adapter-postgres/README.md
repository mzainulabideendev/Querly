# oq-adapter-postgres

PostgreSQL adapter for Querly.

Implements the relational IR subset (schema and `SELECT`), catalog inspection,
and execution through `psycopg`.

- Data model: relational
- Driver: `psycopg` (optional extra `driver`)
- Entry point: `oq.adapters` → `postgres`

See `docs/adapters/ADAPTER_GUIDE.md` for the adapter contract and
`DATABASE_COMPATIBILITY.md` for the supported subset.
