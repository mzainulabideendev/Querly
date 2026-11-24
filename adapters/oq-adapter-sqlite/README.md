# oq-adapter-sqlite

SQLite adapter for Querly.

Implements the relational IR subset (schema and `SELECT`), catalog inspection,
and local file-backed execution through Python's standard `sqlite3` module.

- Data model: relational
- Driver: `sqlite3` (standard library)
- Entry point: `oq.adapters` → `sqlite`

See `docs/adapters/ADAPTER_GUIDE.md` for the adapter contract and
`DATABASE_COMPATIBILITY.md` for the supported subset.
