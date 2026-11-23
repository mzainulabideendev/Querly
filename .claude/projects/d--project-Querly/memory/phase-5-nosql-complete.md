---
name: phase-5-nosql-complete
description: Phase 5 NoSQL adapters for MongoDB and Redis are fully implemented and verified with passing integration tests
metadata:
  type: project
---

All acceptance criteria for Querly Phase 5 (NoSQL database support) are met:

- **MongoDB adapter** (`adapters/oq-adapter-mongodb`): real MongoDB server, native document model (collections, documents, nested objects/arrays, BSON types via Extended JSON tags), CRUD, filtering/sorting/pagination, aggregation pipelines, index generation+inspection, schema inspection, unsafe operator rejection ($where/$function/$accumulator), parameter binding ($param, never interpolated), deterministic plan serialization, multi-document transactions via client sessions. All 14 integration tests pass.
- **Redis adapter** (`adapters/oq-adapter-redis`): real Redis server, native key-value structures (strings, hashes, lists, sets, sorted sets), expiration, key inspection, binary-safe strings, safe command allowlist (FLUSHALL/CONFIG/EVAL/KEYS/SELECT/JOIN refused), MULTI/EXEC transactions. All 15 integration tests pass.

**Verification:** `uv run pytest tests/integration/test_mongodb_integration.py tests/integration/test_redis_integration.py -m integration` → 29 passed in 4.50s. Tests were NOT skipped — real MongoDB (port 57017) and Redis (port 56379) servers were reachable.

Both adapters conform to the protocols in `packages/oq-runtime/src/oq/runtime/contract.py` (`DocumentAdapter` for MongoDB, `KeyValueAdapter` for Redis). Secrets read via `SecretProvider` from `OQ_<KEY>` env vars; driver deps only in adapter packages.

Per instructions: STOP after Phase 5 and await approval.