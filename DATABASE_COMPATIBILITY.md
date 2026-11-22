# Database Compatibility Matrix

**Read this first:** Querly supports **no database** at this time. Every row is
`Not started`. This matrix exists so that support status is explicit and cannot
be inflated by marketing language. A row changes only after the adapter's
integration suite passes against the listed engine versions in CI.

Status vocabulary:

| Status | Meaning |
| --- | --- |
| `Not started` | No adapter code exists. |
| `Experimental` | Adapter exists, passes unit tests, but is not yet verified against a real engine. Never advertised as supported. |
| `Supported` | Adapter passes the full contract and integration suite against the listed versions and is documented. |
| `Unsupported` | Deliberately out of scope; reason documented. |

## Matrix

| Database | Category | Adapter package id | Planned phase | Tested versions | Status |
| --- | --- | --- | --- | --- | --- |
| PostgreSQL | Relational | `oq-adapter-postgres` | 4 | none | Not started |
| MySQL | Relational | `oq-adapter-mysql` | 4 | none | Not started |
| SQLite | Relational | `oq-adapter-sqlite` | 4 | none | Not started |
| MariaDB | Relational | `oq-adapter-mariadb` | 8 | none | Not started |
| SQL Server | Relational | `oq-adapter-sqlserver` | 8 | none | Not started |
| Oracle | Relational | `oq-adapter-oracle` | 8 | none | Not started |
| IBM Db2 | Relational | `oq-adapter-db2` | 8 | none | Not started |
| CockroachDB | Relational | `oq-adapter-cockroachdb` | 8 | none | Not started |
| MongoDB | Document | `oq-adapter-mongodb` | 5 | none | Not started |
| CouchDB | Document | `oq-adapter-couchdb` | 8 | none | Not started |
| Amazon DocumentDB | Document | `oq-adapter-documentdb` | 8 | none | Not started |
| Redis | Key-value | `oq-adapter-redis` | 5 | none | Not started |
| DynamoDB | Key-value | `oq-adapter-dynamodb` | 8 | none | Not started |
| Aerospike | Key-value | `oq-adapter-aerospike` | 8 | none | Not started |
| Cassandra | Wide-column | `oq-adapter-cassandra` | 8 | none | Not started |
| ScyllaDB | Wide-column | `oq-adapter-scylladb` | 8 | none | Not started |
| HBase | Wide-column | `oq-adapter-hbase` | 8 | none | Not started |
| Neo4j | Graph | `oq-adapter-neo4j` | 8 | none | Not started |
| Amazon Neptune | Graph | `oq-adapter-neptune` | 8 | none | Not started |
| Elasticsearch | Search | `oq-adapter-elasticsearch` | 8 | none | Not started |
| OpenSearch | Search | `oq-adapter-opensearch` | 8 | none | Not started |
| ClickHouse | Analytical | `oq-adapter-clickhouse` | 8 | none | Not started |
| pgvector | Vector | `oq-adapter-pgvector` | 8 | none | Not started |
| Qdrant | Vector | `oq-adapter-qdrant` | 8 | none | Not started |
| Milvus | Vector | `oq-adapter-milvus` | 8 | none | Not started |
| Weaviate | Vector | `oq-adapter-weaviate` | 8 | none | Not started |

## What "tested versions" will mean

Only engine versions that the adapter's integration suite actually ran against
are listed. The capability report and this matrix come from the same test
manifest. If a version is not in the list, the adapter may work but makes no
claim.

## Capability differences will be explicit

Each supported adapter will publish a machine-readable capability document
(see `docs/adapters/ADAPTER_GUIDE.md`) covering at least:

* transactions and transactional DDL;
* `returning` support;
* null ordering control;
* each universal type, with native type and range/semantics notes;
* constraint enforcement (native vs runtime);
* identifier case folding and quoting;
* maximum identifier length and index limits;
* vector, JSON, geo, and binary support;
* known unsupported language constructs.

A relational construct is never silently emulated on a non-relational engine.
When there is no faithful representation, compilation fails with a capability
diagnostic.
