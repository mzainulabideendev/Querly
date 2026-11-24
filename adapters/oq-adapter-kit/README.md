# oq-adapter-kit

Driver-free helpers shared by Querly database adapters:

* capability report construction and validation;
* identifier quoting and literal safety checks;
* a native SQL compiler for the relational IR subset;
* document-store diagnostic helpers.

The kit contains no database driver and no engine-specific policy. Each
adapter still owns its driver dependency, native type map, catalog queries,
capability claims, and integration tests.

See [`docs/adapters/PLUGIN_INTERFACE.md`](../../docs/adapters/PLUGIN_INTERFACE.md).
