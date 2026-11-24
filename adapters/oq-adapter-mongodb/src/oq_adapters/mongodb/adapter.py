"""MongoDB adapter for Querly.

This module implements the Phase 5 document-store adapter contract against a
real MongoDB server through pymongo. It deliberately represents MongoDB's own
data model: collections, documents, nested objects, arrays, and BSON types.
Nothing here claims that a collection is a relational table or that a document
is a row.

Design notes
------------
* Filters, update documents, projections, sort specifications, and aggregation
  pipelines are handled as native MongoDB documents. They are never converted
  into SQL and never interpolated into a string that is then parsed again.
* Compiled plans contain a JSON-safe *native* plan in ``PlanDocument.engine_plan``
  using MongoDB Extended JSON (canonical mode), so BSON values survive a
  round-trip through plan serialization.
* Server-side JavaScript (``$where``, ``$function``, ``$accumulator``) is
  rejected with a structured diagnostic. It cannot be represented faithfully in
  portable IR and is an execution-safety hazard.
* Unsupported operations return ``PlanDocument(supported=False)`` with
  ``AdapterDiagnostic`` entries instead of silently emulating a relational
  construct.
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

from bson import Binary, Code, ObjectId, Timestamp
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from bson.json_util import CANONICAL_JSON_OPTIONS
from bson.json_util import dumps as bson_dumps
from bson.json_util import loads as bson_loads
from bson.regex import Regex
from oq.ir import (
    AdapterDiagnostic,
    AggregationStage,
    Artifact,
    CollectionSchema,
    DocumentField,
    DocumentQuery,
    DocumentStoreSchema,
    Index,
    Safety,
    Type,
)
from oq.runtime.contract import (
    CAP_AGGREGATION_FRAMEWORK,
    CAP_ARRAY_TYPE,
    CAP_CHECK_CONSTRAINTS,
    CAP_CHANGE_STREAMS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DOCUMENT_CRUD,
    CAP_DOCUMENT_SCHEMA_VALIDATION,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_INDEXES,
    CAP_JSON,
    CAP_MATERIALIZED_VIEWS,
    CAP_NESTED_DOCUMENTS,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_TRANSACTIONS_MULTI_DOC,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_TYPE,
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    DocumentAdapter,
    DocumentResult,
    EngineVersion,
    PlanDocument,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

__all__ = ["MongoDBAdapter", "MongoDBConnection", "MongoDBTransaction"]


# ---------------------------------------------------------------------------
# BSON -> universal type mapping
# ---------------------------------------------------------------------------

MONGODB_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.EXACT, "int / long", range="-2^63..2^63-1"),
    "float": TypeSupport(TypeSupportLevel.EXACT, "double"),
    "decimal": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "decimal128",
        max_precision=34,
        notes="IEEE 754-2008 decimal128. Exact values, but no arbitrary precision.",
    ),
    "text": TypeSupport(TypeSupportLevel.EXACT, "string"),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "bool"),
    "date": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "date",
        notes="BSON date is signed 64-bit milliseconds since the Unix epoch, UTC.",
    ),
    "time": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "date",
        notes="No separate time-of-day BSON type; stored in a BSON date.",
    ),
    "timestamp": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "date",
        notes="Millisecond precision only; no sub-millisecond component.",
    ),
    "duration": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "long (milliseconds)",
        notes="No native duration; adapters store an integer by convention.",
    ),
    "uuid": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "binData subtype 4",
        notes="Native UUID representation exists in the BSON binary subtype; no generation.",
    ),
    "email": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "string",
        notes="No native email type or validation.",
    ),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "string", notes="No native URL type."),
    "json": TypeSupport(
        TypeSupportLevel.EXACT,
        "object / array",
        notes="BSON embedded documents and arrays retain nesting.",
    ),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "binData"),
    "geo": TypeSupport(
        TypeSupportLevel.COMPATIBLE,
        "GeoJSON object + 2dsphere index",
        notes="GeoJSON is stored as a nested object; query support needs a geo index.",
    ),
    "list": TypeSupport(TypeSupportLevel.EXACT, "array"),
    "set": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "array",
        notes="No native set type; uniqueness and set algebra are application-level.",
    ),
    "map": TypeSupport(TypeSupportLevel.EXACT, "object"),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No core vector type."),
    "reference": TypeSupport(
        TypeSupportLevel.APPROXIMATE,
        "ObjectId",
        notes="References are values; MongoDB does not enforce referential integrity.",
    ),
    "null": TypeSupport(TypeSupportLevel.EXACT, "null"),
}


MONGODB_CAPABILITIES: dict[str, Capability] = {
    CAP_DOCUMENT_CRUD: Capability(True, "insert, find, update, replace, delete, count, distinct"),
    CAP_NESTED_DOCUMENTS: Capability(True, "Arbitrary nesting of objects and arrays"),
    CAP_INDEXES: Capability(
        True,
        "Single, compound, unique, sparse, partial, TTL, text, hashed, and geospatial indexes",
    ),
    CAP_AGGREGATION_FRAMEWORK: Capability(True, "Native aggregation pipeline stages"),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(
        True, "Optional JSON Schema validator per collection"
    ),
    CAP_TRANSACTIONS: Capability(
        True, "Multi-document transactions require a replica set or sharded cluster"
    ),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(
        True, "Native multi-document ACID transactions on replica sets (MongoDB 4.0+)"
    ),
    CAP_CHANGE_STREAMS: Capability(True, "Change streams require a replica set"),
    CAP_FOREIGN_KEYS: Capability(
        False, "References are not enforced; use $lookup or application logic"
    ),
    CAP_ARRAY_TYPE: Capability(True, "BSON arrays are native"),
    CAP_CHECK_CONSTRAINTS: Capability(
        False, "No native CHECK constraints; use JSON Schema validation"
    ),
    CAP_DECIMAL_UNBOUNDED: Capability(
        False, "decimal128 is fixed 34-digit precision"
    ),
    CAP_DURATION_TYPE: Capability(
        False, "No native duration; store as integer by convention"
    ),
    CAP_EMAIL_TYPE: Capability(False, "No native email type or validation"),
    CAP_FULL_TEXT_SEARCH: Capability(
        True, "Atlas Search or $text with a text index"
    ),
    CAP_GEO_TYPE: Capability(
        True, "GeoJSON with a 2dsphere index"
    ),
    CAP_JSON: Capability(
        True, "BSON documents are the native JSON-like representation"
    ),
    CAP_MATERIALIZED_VIEWS: Capability(
        False, "No materialized views; use $merge or change streams"
    ),
    CAP_ORDER_BY_NULLS: Capability(
        False, "No NULLS FIRST/LAST; sort order is application-level"
    ),
    CAP_PARTIAL_INDEXES: Capability(
        False, "Partial indexes are not supported in MongoDB core"
    ),
    CAP_PARTITIONING: Capability(
        False, "No native partitioning; use sharding"
    ),
    CAP_REPLICATION: Capability(
        True, "Replica sets and sharded clusters"
    ),
    CAP_RETURNING: Capability(
        False, "No RETURNING clause; use find after update"
    ),
    CAP_TRANSACTIONAL_DDL: Capability(
        False, "DDL is not transactional"
    ),
    CAP_UNIQUE_CONSTRAINTS: Capability(
        True, "Unique indexes enforce uniqueness"
    ),
    CAP_URL_TYPE: Capability(False, "No native URL type or validation"),
    CAP_UUID_TYPE: Capability(
        True, "binData subtype 4 stores UUIDs"
    ),
    CAP_VECTOR_TYPE: Capability(
        False, "No core vector type; requires Atlas Vector Search"
    ),
}


# ---------------------------------------------------------------------------
# Native operator allowlists
# ---------------------------------------------------------------------------

UNSAFE_FILTER_OPERATORS = {"$where", "$function", "$accumulator"}

ALLOWED_FILTER_OPERATORS = {
    "$eq",
    "$ne",
    "$gt",
    "$gte",
    "$lt",
    "$lte",
    "$in",
    "$nin",
    "$exists",
    "$type",
    "$mod",
    "$regex",
    "$options",
    "$text",
    "$search",
    "$language",
    "$caseSensitive",
    "$diacriticSensitive",
    "$all",
    "$elemMatch",
    "$size",
    "$and",
    "$or",
    "$nor",
    "$not",
    "$expr",
    "$jsonSchema",
    "$geoWithin",
    "$geoIntersects",
    "$near",
    "$nearSphere",
    "$maxDistance",
    "$minDistance",
    "$box",
    "$center",
    "$centerSphere",
    "$polygon",
    "$geometry",
    "$bitsAllSet",
    "$bitsAnySet",
    "$bitsAllClear",
    "$bitsAnyClear",
    "$comment",
    "$slice",
}

ALLOWED_UPDATE_OPERATORS = {
    "$set",
    "$unset",
    "$inc",
    "$mul",
    "$min",
    "$max",
    "$rename",
    "$push",
    "$pull",
    "$pop",
    "$addToSet",
    "$currentDate",
    "$setOnInsert",
    "$bit",
    "$pullAll",
}

ALLOWED_AGGREGATION_STAGES = {
    "$addFields",
    "$bucket",
    "$bucketAuto",
    "$collStats",
    "$count",
    "$densify",
    "$documents",
    "$facet",
    "$fill",
    "$geoNear",
    "$graphLookup",
    "$group",
    "$indexStats",
    "$limit",
    "$lookup",
    "$match",
    "$merge",
    "$out",
    "$project",
    "$redact",
    "$replaceRoot",
    "$replaceWith",
    "$sample",
    "$set",
    "$setWindowFields",
    "$skip",
    "$sort",
    "$sortByCount",
    "$unionWith",
    "$unset",
    "$unwind",
}

UNSAFE_AGGREGATION_STAGES = {"$function", "$accumulator"}

SUPPORTED_OPERATIONS = {
    "find",
    "find_one",
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "delete_one",
    "delete_many",
    "aggregate",
    "count",
    "distinct",
}

WRITE_OPERATIONS = {"insert_one", "insert_many", "update_one", "update_many", "replace_one"}
DESTRUCTIVE_OPERATIONS = {"delete_one", "delete_many"}
READ_OPERATIONS = {"find", "find_one", "aggregate", "count", "distinct"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _to_json_safe(value: Any) -> Any:
    """Convert a BSON value tree to canonical Extended JSON values.

    The result contains only JSON-native types, so it can be stored in a
    ``PlanDocument`` and serialized deterministically. ``_from_json_safe``
    reverses the conversion and reconstructs real BSON values for execution.

    Plain JSON scalars (strings, booleans, int32-range integers, and doubles)
    stay plain because that is how MongoDB's own Extended JSON represents
    them. Values whose BSON type cannot be inferred from a JSON number or
    string carry the canonical tag (``$oid``, ``$numberLong``,
    ``$numberDecimal``, ``$binary``, ``$date``, ...). That distinction is what
    keeps a type-lossless round trip possible: ``{"$numberLong": "1"}`` is a
    64-bit integer, not the int32 ``1``.
    """

    if value is None or isinstance(value, (str, bool)):
        return value
    # Int64 and Timestamp are int subclasses, so test them before ``int``.
    if isinstance(value, Int64):
        return {"$numberLong": str(value)}
    if isinstance(value, Timestamp):
        return {"$timestamp": {"t": value.time, "i": value.inc}}
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return value
        return {"$numberLong": str(value)}
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal128):
        return {"$numberDecimal": str(value)}
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    if isinstance(value, Binary):
        return {
            "$binary": {
                "base64": base64.b64encode(bytes(value)).decode("ascii"),
                "subType": f"{value.subtype:02x}",
            }
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "$binary": {
                "base64": base64.b64encode(bytes(value)).decode("ascii"),
                "subType": "00",
            }
        }
    if isinstance(value, datetime):
        milliseconds = int(value.astimezone(UTC).timestamp() * 1000)
        return {"$date": {"$numberLong": str(milliseconds)}}
    if isinstance(value, Regex):
        return {
            "$regularExpression": {
                "pattern": value.pattern,
                "options": value.flags if isinstance(value.flags, str) else "",
            }
        }
    if isinstance(value, Code):
        return {"$code": str(value), "scope": _to_json_safe(value.scope)}
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_to_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    # Unknown BSON scalar: let the library's canonical encoder describe it,
    # then parse that JSON back so the result stays JSON-native.
    return json.loads(bson_dumps(value, json_options=CANONICAL_JSON_OPTIONS))


def _from_json_safe(value: Any) -> Any:
    """Reconstruct BSON values from canonical Extended JSON values."""

    return bson_loads(json.dumps(value))


def _bson_kind(value: Any) -> str:
    """Return a stable name for a BSON value's type."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Int64):
        return "long"
    if isinstance(value, int):
        return "int" if -(2**31) <= value < 2**31 else "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, Decimal128):
        return "decimal128"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, datetime):
        return "date"
    if isinstance(value, (bytes, bytearray)):
        return "binData"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Regex):
        return "regex"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


_BSON_TO_UNIVERSAL = {
    "int": "integer",
    "long": "integer",
    "double": "float",
    "decimal128": "decimal",
    "string": "text",
    "bool": "boolean",
    "date": "timestamp",
    "objectId": "uuid",
    "binData": "binary",
    "object": "map",
    "array": "list",
    "null": "null",
    "regex": "text",
}


def _universal_type(bson_kinds: Iterable[str]) -> Type:
    kinds = sorted({kind for kind in bson_kinds if kind != "null"})
    if not kinds:
        return Type(kind="null")
    if len(kinds) == 1:
        return Type(kind=_BSON_TO_UNIVERSAL.get(kinds[0], "json"))
    variants = [Type(kind=_BSON_TO_UNIVERSAL.get(kind, "json")) for kind in kinds]
    return Type(kind="union", variants=variants)


def _native_key(index: Index) -> dict[str, Any]:
    if index.keys:
        return {str(entry["field"]): entry.get("direction", 1) for entry in index.keys}
    return {column: 1 for column in index.columns}


# ---------------------------------------------------------------------------
# Connection & transaction
# ---------------------------------------------------------------------------


class MongoDBConnection(Connection):
    """An open MongoDB connection bound to one logical database."""

    def __init__(self, config: ConnectionConfig, client: MongoClient, database: Any) -> None:
        self._config = config
        self._client = client
        self._db = database
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except PyMongoError:
            return False

    @property
    def client(self) -> MongoClient:
        return self._client

    @property
    def database(self) -> Any:
        return self._db

    def close(self) -> None:
        if not self._closed:
            self._client.close()
            self._closed = True


class MongoDBTransaction(Transaction):
    """A native MongoDB multi-document transaction backed by a client session."""

    def __init__(self, connection: MongoDBConnection, options: TransactionOptions) -> None:
        self._connection = connection
        self._options = options
        self._session = connection.client.start_session()
        self._session.start_transaction()
        self._active = True

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def is_active(self) -> bool:
        return self._active and not self._connection.is_closed

    @property
    def session(self) -> Any:
        return self._session

    def commit(self) -> None:
        if self._active:
            self._session.commit_transaction()
            self._session.end_session()
            self._active = False

    def rollback(self) -> None:
        if self._active:
            try:
                self._session.abort_transaction()
            except PyMongoError:
                pass  # rollback must not raise
            finally:
                with contextlib.suppress(PyMongoError):
                    self._session.end_session()
                self._active = False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FieldStats:
    kinds: set[str]
    present_in: int
    total: int


class MongoDBAdapter(DocumentAdapter):
    """Document-store adapter for MongoDB."""

    @property
    def id(self) -> str:
        return "mongodb"

    @property
    def display_name(self) -> str:
        return "MongoDB"

    @property
    def package_version(self) -> str:
        from oq_adapters.mongodb import __version__

        return __version__

    # -- capability reporting -------------------------------------------------

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.mongodb import __version__

        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="MongoDB",
            tested_versions=["7.0"],
            capabilities=dict(MONGODB_CAPABILITIES),
            types=dict(MONGODB_TYPE_MAP),
            limitations=[
                "No enforced referential integrity between collections.",
                "Joins exist only inside aggregation pipelines ($lookup), not as relational joins.",
                "Multi-document transactions require a replica set or sharded cluster.",
                "BSON dates have millisecond precision and are always UTC.",
                "Documents are limited to 16 MiB by the server.",
                "Server-side JavaScript ($where, $function, $accumulator) is refused as unsafe.",
            ],
            generated_at=datetime.now(UTC),
        )

    # -- connection -----------------------------------------------------------

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "mongodb":
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Expected adapter 'mongodb', got {config.adapter!r}",
            )

        host = config.host or "localhost"
        port = config.port or 27017
        database = config.database or "querly_test"
        user = config.user
        password = secrets.get("password") if secrets is not None else None

        options = dict(config.options)
        auth_source = options.pop("auth_source", "admin")
        tls = options.pop("tls", "false").lower() in {"1", "true", "yes"}
        direct = options.pop("direct_connection", "false").lower() in {"1", "true", "yes"}

        credentials = ""
        if user:
            credentials = quote_plus(user)
            if password:
                credentials += ":" + quote_plus(password)
            credentials += "@"

        uri = f"mongodb://{credentials}{host}:{port}/{quote_plus(database)}"
        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": int(options.get("server_selection_timeout_ms", "5000")),
        }
        if user:
            kwargs["authSource"] = auth_source
            kwargs["username"] = user
            if password:
                kwargs["password"] = password
        if tls:
            kwargs["tls"] = True
        if direct:
            kwargs["directConnection"] = True

        try:
            client = MongoClient(uri, **kwargs)
            client.admin.command("ping")
            return MongoDBConnection(config, client, client[database])
        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            if "client" in locals():
                client.close()
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Failed to connect to MongoDB at {host}:{port}",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc
        except OperationFailure as exc:
            if "client" in locals():
                client.close()
            raise AdapterError(
                AdapterErrorCode.AUTH,
                "MongoDB rejected the connection credentials",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc
        except PyMongoError as exc:
            if "client" in locals():
                client.close()
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "MongoDB connection error",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc

    # -- inspection -----------------------------------------------------------

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require_connection(connection)
        db = conn.database
        sample_limit = int(conn.config.options.get("inspect_sample_size", "1000"))
        collections: list[CollectionSchema] = []

        for name in sorted(db.list_collection_names()):
            if name.startswith("system."):
                continue
            coll = db[name]
            document_count = coll.estimated_document_count()
            sample = list(coll.find({}, limit=sample_limit))
            fields = self._infer_fields(sample)
            indexes = [self._index_from_native(info) for info in coll.list_indexes()]
            collections.append(
                CollectionSchema(
                    name=name,
                    fields=fields,
                    document_count=document_count,
                    indexes=indexes,
                )
            )

        return DocumentStoreSchema(collections=collections)

    def _infer_fields(self, documents: list[dict[str, Any]]) -> list[DocumentField]:
        stats: dict[str, _FieldStats] = {}
        total = len(documents)
        for document in documents:
            paths: dict[str, set[str]] = {}
            self._collect_paths(document, "", paths)
            for path, kinds in paths.items():
                current = stats.get(path)
                if current is None:
                    stats[path] = _FieldStats(kinds=set(kinds), present_in=1, total=total)
                else:
                    current.kinds.update(kinds)
                    stats[path] = _FieldStats(
                        kinds=current.kinds, present_in=current.present_in + 1, total=total
                    )

        fields: list[DocumentField] = []
        for path in sorted(stats):
            stat = stats[path]
            nullable = "null" in stat.kinds or stat.present_in < max(stat.total, 1)
            fields.append(
                DocumentField(
                    path=path,
                    type=_universal_type(stat.kinds),
                    nullable=nullable,
                    frequency=(stat.present_in / stat.total) if stat.total else 0.0,
                )
            )
        return fields

    def _collect_paths(self, value: Any, prefix: str, out: dict[str, set[str]]) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            out.setdefault(path, set()).add(_bson_kind(item))
            if isinstance(item, dict):
                self._collect_paths(item, path, out)
            elif isinstance(item, (list, tuple)):
                for element in item:
                    out.setdefault(path + "[]", set()).add(_bson_kind(element))
                    if isinstance(element, dict):
                        self._collect_paths(element, path + "[]", out)

    def _index_from_native(self, info: Mapping[str, Any]) -> Index:
        keys = list(info.get("key", {}).items())
        direction_values = [direction for _, direction in keys]
        if any(value == "text" for value in direction_values):
            kind = "text"
        elif any(value in {"2d", "2dsphere", "geoHaystack"} for value in direction_values):
            kind = "geospatial"
        elif any(value == "hashed" for value in direction_values):
            kind = "hashed"
        elif "expireAfterSeconds" in info:
            kind = "ttl"
        elif len(keys) > 1:
            kind = "compound"
        else:
            kind = "single"

        options = {
            key: _to_json_safe(value)
            for key, value in info.items()
            if key
            not in {
                "key",
                "name",
                "unique",
                "sparse",
                "expireAfterSeconds",
                "partialFilterExpression",
                "v",
                "ns",
            }
        }
        partial = info.get("partialFilterExpression")
        return Index(
            name=str(info.get("name", "")),
            columns=[str(field) for field, _ in keys],
            unique=bool(info.get("unique", False)),
            kind=kind,
            keys=[
                {"field": str(field), "direction": _to_json_safe(direction)}
                for field, direction in keys
            ],
            sparse=bool(info.get("sparse", False)),
            expire_after_seconds=info.get("expireAfterSeconds"),
            partial_filter_expression=(_to_json_safe(partial) if partial is not None else None),
            options=options,
        )

    # -- schema compilation ---------------------------------------------------

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        artifacts: list[DDLArtifact] = []
        for collection in schema.collections:
            command: dict[str, Any] = {"create": collection.name}
            validator = self._json_schema(collection)
            if validator:
                command["validator"] = {"$jsonSchema": validator}
                command["validationLevel"] = "strict"
                command["validationAction"] = "error"
            artifacts.append(
                DDLArtifact(
                    sql=self._render_artifact(command, options),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
            for index in collection.indexes:
                spec: dict[str, Any] = {
                    "key": _native_key(index),
                    "name": index.name,
                }
                if index.unique:
                    spec["unique"] = True
                if index.sparse:
                    spec["sparse"] = True
                if index.expire_after_seconds is not None:
                    spec["expireAfterSeconds"] = index.expire_after_seconds
                if index.partial_filter_expression is not None:
                    spec["partialFilterExpression"] = _from_json_safe(
                        index.partial_filter_expression
                    )
                command = {"createIndexes": collection.name, "indexes": [spec]}
                artifacts.append(
                    DDLArtifact(
                        sql=self._render_artifact(command, options),
                        statement_type="create_index",
                        entity=collection.name,
                        depends_on=[collection.name],
                    )
                )
        return artifacts

    def _json_schema(self, collection: CollectionSchema) -> dict[str, Any]:
        root: dict[str, Any] = {"bsonType": "object"}
        properties: dict[str, Any] = {}
        required: list[str] = []

        for field in collection.fields:
            if "[]" in field.path or "." in field.path:
                continue
            schema = self._field_json_schema(field)
            properties[field.path] = schema
            if not field.nullable and field.frequency >= 0.999:
                required.append(field.path)

        if properties:
            root["properties"] = properties
        if required:
            root["required"] = sorted(required)
        return root

    def _field_json_schema(self, field: DocumentField) -> dict[str, Any]:
        mapping = {
            "integer": "long",
            "float": "double",
            "decimal": "decimal",
            "text": "string",
            "boolean": "bool",
            "timestamp": "date",
            "date": "date",
            "binary": "binData",
            "json": "object",
            "map": "object",
            "list": "array",
            "set": "array",
            "null": "null",
        }
        bson_type = mapping.get(field.type.kind)
        if bson_type is None:
            return {}
        schema: dict[str, Any] = {"bsonType": bson_type}
        if field.nullable and bson_type != "null":
            schema["bsonType"] = [bson_type, "null"]
        return schema

    # -- query compilation ----------------------------------------------------

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics: list[AdapterDiagnostic] = []

        if query.operation not in SUPPORTED_OPERATIONS:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.UNSUPPORTED.value,
                    message=(
                        f"MongoDB does not support document operation {query.operation!r}. "
                        "Relational operations are not emulated on a document store."
                    ),
                    operation=query.operation,
                    feature=query.operation,
                    hint=f"Supported operations: {', '.join(sorted(SUPPORTED_OPERATIONS))}",
                )
            )
            return self._unsupported_plan(query, diagnostics)

        collection_names = {collection.name for collection in schema.collections}
        if collection_names and query.collection not in collection_names:
            diagnostics.append(
                AdapterDiagnostic(
                    code=AdapterErrorCode.CAPABILITY.value,
                    message=f"Collection {query.collection!r} is not present in the schema.",
                    operation=query.operation,
                    feature="collection",
                    path=query.collection,
                )
            )
            return self._unsupported_plan(query, diagnostics)

        diagnostics.extend(self._validate_filter(query.filter, "filter"))
        diagnostics.extend(self._validate_update(query.updates))
        diagnostics.extend(self._validate_pipeline(query.pipeline))
        diagnostics.extend(self._validate_sort(query.sort))
        diagnostics.extend(self._validate_unsafe(query.projection, "projection"))

        if diagnostics:
            return self._unsupported_plan(query, diagnostics)

        plan = self._native_plan(query)
        safety = self._safety(query, plan)
        artifact = self._render_artifact(plan, CompileOptions())
        required = [CAP_AGGREGATION_FRAMEWORK] if query.operation == "aggregate" else []
        if query.operation == "aggregate":
            required = [CAP_AGGREGATION_FRAMEWORK]

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="mongodb",
                text=artifact,
                dialect="mongodb",
            ),
            parameters=[
                {"name": parameter.name, "type": parameter.type.kind}
                for parameter in query.parameters
            ],
            required_capabilities=required,
            safety=safety,
            result_columns=self._result_columns(query, schema),
            engine_plan=plan,
            diagnostics=[],
            supported=True,
        )

    def _unsupported_plan(
        self, query: DocumentQuery, diagnostics: list[AdapterDiagnostic]
    ) -> PlanDocument:
        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(kind="mongodb", text="", dialect="mongodb"),
            parameters=[
                {"name": parameter.name, "type": parameter.type.kind}
                for parameter in query.parameters
            ],
            safety=Safety(classification="read"),
            engine_plan=None,
            diagnostics=diagnostics,
            supported=False,
        )

    def _native_plan(self, query: DocumentQuery) -> dict[str, Any]:
        plan: dict[str, Any] = {
            "adapter": self.id,
            "operation": query.operation,
            "collection": query.collection,
        }
        if query.filter is not None:
            plan["filter"] = _to_json_safe(query.filter)
        if query.projection is not None:
            plan["projection"] = _to_json_safe(query.projection)
        if query.sort:
            plan["sort"] = [
                [
                    str(item.get("field")),
                    self._direction(item.get("direction", 1)),
                ]
                for item in query.sort
            ]
        if query.skip is not None:
            plan["skip"] = int(query.skip)
        if query.limit is not None:
            plan["limit"] = int(query.limit)
        if query.documents:
            plan["documents"] = [_to_json_safe(document) for document in query.documents]
        if query.updates is not None:
            plan["update"] = _to_json_safe(query.updates)
        if query.pipeline:
            plan["pipeline"] = [
                {stage.operator: _to_json_safe(stage.value)} for stage in query.pipeline
            ]
        if query.upsert:
            plan["upsert"] = True
        if query.array_filters:
            plan["array_filters"] = [_to_json_safe(item) for item in query.array_filters]
        if query.hint is not None:
            plan["hint"] = _to_json_safe(query.hint)
        if query.collation is not None:
            plan["collation"] = _to_json_safe(query.collation)
        if query.distinct_field is not None:
            plan["distinct_field"] = query.distinct_field
        if query.options:
            plan["options"] = _to_json_safe(query.options)
        return _to_json_safe(plan)

    def _safety(self, query: DocumentQuery, plan: Mapping[str, Any]) -> Safety:
        operation = query.operation
        unfiltered = not query.filter
        if operation in READ_OPERATIONS:
            stages = {stage.operator for stage in query.pipeline}
            if stages & {"$out", "$merge"}:
                return Safety(
                    classification="destructive_write",
                    unfiltered=False,
                    destructive=True,
                )
            return Safety(classification="read")
        if operation in DESTRUCTIVE_OPERATIONS:
            return Safety(
                classification="destructive_write",
                unfiltered=unfiltered,
                destructive=True,
            )
        if operation in WRITE_OPERATIONS:
            return Safety(classification="write", unfiltered=unfiltered)
        return Safety(classification="read")

    def _validate_filter(self, value: Any, path: str) -> list[AdapterDiagnostic]:
        diagnostics: list[AdapterDiagnostic] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str) and key == "$param":
                    # Binding placeholder, substituted at execution time.
                    continue
                if isinstance(key, str) and key.startswith("$"):
                    if key in UNSAFE_FILTER_OPERATORS:
                        diagnostics.append(
                            AdapterDiagnostic(
                                code=AdapterErrorCode.UNSUPPORTED.value,
                                message=f"{key} is server-side JavaScript and is refused.",
                                operation="filter",
                                feature=key,
                                path=path,
                                hint="Use $expr or an application-side predicate.",
                            )
                        )
                    elif key not in ALLOWED_FILTER_OPERATORS:
                        diagnostics.append(
                            AdapterDiagnostic(
                                code=AdapterErrorCode.UNSUPPORTED.value,
                                message=f"Unknown or unsupported MongoDB filter operator {key!r}.",
                                operation="filter",
                                feature=key,
                                path=path,
                            )
                        )
                    if key in {"$expr", "$jsonSchema"}:
                        # These carry expression trees with their own operator
                        # vocabulary. Only the unsafe-JavaScript check applies.
                        diagnostics.extend(self._validate_unsafe(item, f"{path}.{key}"))
                        continue
                diagnostics.extend(self._validate_filter(item, f"{path}.{key}"))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                diagnostics.extend(self._validate_filter(item, f"{path}[{index}]"))
        return diagnostics

    def _validate_unsafe(self, value: Any, path: str) -> list[AdapterDiagnostic]:
        """Find server-side JavaScript anywhere in a native value tree."""

        diagnostics: list[AdapterDiagnostic] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str) and key in UNSAFE_FILTER_OPERATORS:
                    diagnostics.append(
                        AdapterDiagnostic(
                            code=AdapterErrorCode.UNSUPPORTED.value,
                            message=f"{key} is server-side JavaScript and is refused.",
                            operation="native",
                            feature=key,
                            path=path,
                            hint="Use built-in query/aggregation operators.",
                        )
                    )
                diagnostics.extend(self._validate_unsafe(item, f"{path}.{key}"))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                diagnostics.extend(self._validate_unsafe(item, f"{path}[{index}]"))
        return diagnostics

    def _validate_update(self, update: Any) -> list[AdapterDiagnostic]:
        if update is None:
            return []
        if not isinstance(update, Mapping):
            return [
                AdapterDiagnostic(
                    code=AdapterErrorCode.SYNTAX.value,
                    message="An update document must be a mapping.",
                    operation="update",
                    feature="update",
                )
            ]
        operator_keys = [key for key in update if str(key).startswith("$")]
        if not operator_keys:
            return []  # replacement document
        diagnostics: list[AdapterDiagnostic] = []
        for key in operator_keys:
            if key not in ALLOWED_UPDATE_OPERATORS:
                diagnostics.append(
                    AdapterDiagnostic(
                        code=AdapterErrorCode.UNSUPPORTED.value,
                        message=f"Unsupported MongoDB update operator {key!r}.",
                        operation="update",
                        feature=str(key),
                    )
                )
        diagnostics.extend(self._validate_unsafe(update, "update"))
        return diagnostics

    def _validate_pipeline(self, pipeline: list[AggregationStage]) -> list[AdapterDiagnostic]:
        diagnostics: list[AdapterDiagnostic] = []
        for position, stage in enumerate(pipeline):
            operator = stage.operator
            if not operator.startswith("$"):
                operator = "$" + operator
            if operator in UNSAFE_AGGREGATION_STAGES:
                diagnostics.append(
                    AdapterDiagnostic(
                        code=AdapterErrorCode.UNSUPPORTED.value,
                        message=f"{operator} is server-side JavaScript and is refused.",
                        operation="aggregate",
                        feature=operator,
                        path=f"pipeline[{position}]",
                        hint="Use built-in aggregation operators.",
                    )
                )
                continue
            if operator not in ALLOWED_AGGREGATION_STAGES:
                diagnostics.append(
                    AdapterDiagnostic(
                        code=AdapterErrorCode.UNSUPPORTED.value,
                        message=f"Unsupported MongoDB aggregation stage {operator!r}.",
                        operation="aggregate",
                        feature=operator,
                        path=f"pipeline[{position}]",
                    )
                )
                continue
            diagnostics.extend(
                self._validate_unsafe(stage.value, f"pipeline[{position}].{operator}")
            )
        return diagnostics

    def _validate_sort(self, sort: list[dict[str, Any]]) -> list[AdapterDiagnostic]:
        diagnostics: list[AdapterDiagnostic] = []
        for position, item in enumerate(sort):
            if "field" not in item:
                diagnostics.append(
                    AdapterDiagnostic(
                        code=AdapterErrorCode.SYNTAX.value,
                        message="A sort item requires a 'field'.",
                        operation="sort",
                        feature="sort",
                        path=f"sort[{position}]",
                    )
                )
        return diagnostics

    def _result_columns(
        self, query: DocumentQuery, schema: DocumentStoreSchema
    ) -> list[dict[str, Any]]:
        if query.projection:
            include = [
                {"name": field, "type": "unknown", "nullable": True}
                for field, value in query.projection.items()
                if value and not str(field).startswith("$") and field != "_id"
            ]
            if include:
                return include
        collection = next(
            (item for item in schema.collections if item.name == query.collection), None
        )
        if collection is None:
            return []
        return [
            {"name": field.path, "type": field.type.kind, "nullable": field.nullable}
            for field in collection.fields
            if "[]" not in field.path and "." not in field.path
        ]

    @staticmethod
    def _direction(value: Any) -> int:
        if value in (1, "asc", "ascending", ASCENDING, "1"):
            return 1
        if value in (-1, "desc", "descending", DESCENDING, "-1"):
            return -1
        return 1

    def _render_artifact(self, command: Mapping[str, Any], options: CompileOptions) -> str:
        if options.pretty:
            return json.dumps(command, sort_keys=True, indent=2, ensure_ascii=False)
        return json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    # -- execution ------------------------------------------------------------

    def execute(
        self,
        connection: Connection,
        plan: PlanDocument,
        params: Mapping[str, Any],
    ) -> DocumentResult:
        if plan.target_adapter != self.id:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                f"Plan targets {plan.target_adapter!r}, not 'mongodb'.",
            )
        if not plan.supported:
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                "The plan contains unsupported operations.",
                diagnostics=[diagnostic.model_dump(mode="json") for diagnostic in plan.diagnostics],
            )
        if plan.engine_plan is None:
            raise AdapterError(
                AdapterErrorCode.SYNTAX,
                "MongoDB plan has no structured native plan.",
            )

        conn = self._require_connection(connection)
        native = self._bind_plan(plan.engine_plan, params)
        operation = str(native["operation"])
        collection_name = str(native["collection"])
        collection = conn.database[collection_name]

        try:
            return self._dispatch(collection, operation, native)
        except DuplicateKeyError as exc:
            raise AdapterError(
                AdapterErrorCode.CONSTRAINT,
                "MongoDB rejected a duplicate key.",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc
        except OperationFailure as exc:
            raise self._operation_error(exc) from exc
        except PyMongoError as exc:
            raise AdapterError(
                AdapterErrorCode.DATA,
                "MongoDB operation failed.",
                cause=exc,
                redacted_cause=redact_message(str(exc)),
            ) from exc

    def _bind_plan(self, plan: dict[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
        safe_params = {name: _to_json_safe(value) for name, value in params.items()}

        def substitute(value: Any, path: str) -> Any:
            if isinstance(value, dict):
                if set(value) == {"$param"}:
                    name = value["$param"]
                    if name not in safe_params:
                        raise AdapterError(
                            AdapterErrorCode.DATA,
                            f"Missing bound parameter {name!r}.",
                        )
                    return safe_params[name]
                return {key: substitute(item, f"{path}.{key}") for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item, f"{path}[{index}]") for index, item in enumerate(value)]
            return value

        bound = substitute(plan, "plan")
        return _from_json_safe(bound)

    def _dispatch(
        self, collection: Any, operation: str, native: Mapping[str, Any]
    ) -> DocumentResult:
        options = dict(native.get("options", {}))

        if operation in {"find", "find_one"}:
            filters = native.get("filter") or {}
            projection = native.get("projection")
            sort = [tuple(item) for item in native.get("sort", [])]
            skip = int(native.get("skip", 0))
            limit = int(native.get("limit", 0))
            if operation == "find_one":
                document = collection.find_one(
                    filters,
                    projection,
                    sort=sort or None,
                )
                return DocumentResult(
                    documents=[_to_json_safe(document)] if document is not None else [],
                    affected_count=None,
                )
            cursor = collection.find(
                filters,
                projection,
                sort=sort or None,
                skip=skip,
                limit=limit,
            )
            if "hint" in native:
                cursor = cursor.hint(native["hint"])
            if "collation" in native:
                cursor = cursor.collation(native["collation"])
            documents = [_to_json_safe(document) for document in cursor]
            return DocumentResult(documents=documents, affected_count=None)

        if operation == "insert_one":
            documents = native.get("documents") or [{}]
            result = collection.insert_one(documents[0])
            return DocumentResult(
                documents=[{"_id": _to_json_safe(result.inserted_id)}],
                affected_count=1,
            )

        if operation == "insert_many":
            documents = native.get("documents") or []
            result = collection.insert_many(documents)
            return DocumentResult(
                documents=[{"_id": _to_json_safe(oid)} for oid in result.inserted_ids],
                affected_count=len(result.inserted_ids),
            )

        if operation in {"update_one", "update_many"}:
            filters = native.get("filter") or {}
            update = native.get("update") or {}
            kwargs: dict[str, Any] = {}
            if native.get("upsert"):
                kwargs["upsert"] = True
            if native.get("array_filters"):
                kwargs["array_filters"] = native["array_filters"]
            if "hint" in native:
                kwargs["hint"] = native["hint"]
            if "collation" in native:
                kwargs["collation"] = native["collation"]
            method = collection.update_one if operation == "update_one" else collection.update_many
            result = method(filters, update, **kwargs)
            return DocumentResult(
                documents=[
                    {
                        "matched_count": result.matched_count,
                        "modified_count": result.modified_count,
                        "upserted_id": _to_json_safe(result.upserted_id),
                    }
                ],
                affected_count=result.modified_count,
            )

        if operation == "replace_one":
            filters = native.get("filter") or {}
            replacement = native.get("update") or {}
            result = collection.replace_one(filters, replacement, upsert=bool(native.get("upsert")))
            return DocumentResult(
                documents=[
                    {
                        "matched_count": result.matched_count,
                        "modified_count": result.modified_count,
                        "upserted_id": _to_json_safe(result.upserted_id),
                    }
                ],
                affected_count=result.modified_count,
            )

        if operation in {"delete_one", "delete_many"}:
            filters = native.get("filter") or {}
            method = collection.delete_one if operation == "delete_one" else collection.delete_many
            result = method(filters)
            return DocumentResult(
                documents=[{"deleted_count": result.deleted_count}],
                affected_count=result.deleted_count,
            )

        if operation == "aggregate":
            pipeline = native.get("pipeline") or []
            kwargs = {}
            if options.get("allow_disk_use"):
                kwargs["allowDiskUse"] = True
            if "hint" in native:
                kwargs["hint"] = native["hint"]
            if "collation" in native:
                kwargs["collation"] = native["collation"]
            cursor = collection.aggregate(pipeline, **kwargs)
            documents = [_to_json_safe(document) for document in cursor]
            return DocumentResult(documents=documents, affected_count=None)

        if operation == "count":
            filters = native.get("filter") or {}
            count = collection.count_documents(filters)
            return DocumentResult(documents=[{"count": count}], affected_count=None)

        if operation == "distinct":
            field = native.get("distinct_field")
            if not field:
                raise AdapterError(
                    AdapterErrorCode.SYNTAX,
                    "The distinct operation requires 'distinct_field'.",
                )
            values = collection.distinct(field, native.get("filter") or {})
            documents = [{"value": _to_json_safe(value)} for value in values]
            return DocumentResult(documents=documents, affected_count=None)

        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED,
            f"Unsupported native MongoDB operation {operation!r}.",
        )

    @staticmethod
    def _operation_error(exc: OperationFailure) -> AdapterError:
        if exc.code in {50}:
            code = AdapterErrorCode.TIMEOUT
        elif exc.code in {13, 18}:
            code = AdapterErrorCode.AUTH
        elif exc.code in {11000}:
            code = AdapterErrorCode.CONSTRAINT
        else:
            code = AdapterErrorCode.DATA
        return AdapterError(
            code,
            f"MongoDB operation failed with code {exc.code}.",
            retryable=exc.code in {112, 251},
            cause=exc,
            redacted_cause=redact_message(str(exc)),
        )

    def _require_connection(self, connection: Connection) -> MongoDBConnection:
        if not isinstance(connection, MongoDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "MongoDB adapter received a connection it did not create.",
            )
        if connection.is_closed:
            raise AdapterError(AdapterErrorCode.CONNECTION, "MongoDB connection is closed.")
        return connection

    # -- transactions & lifecycle --------------------------------------------

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        conn = self._require_connection(connection)
        return MongoDBTransaction(conn, options)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MongoDBTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "MongoDB adapter received a transaction it did not create.",
            )
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MongoDBTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "MongoDB adapter received a transaction it did not create.",
            )
        transaction.rollback()

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require_connection(connection)
        build_info = conn.client.admin.command("buildInfo")
        version = str(build_info.get("version", "0.0.0")).split("-")[0]
        parts = version.split(".")
        major = int(parts[0]) if parts and parts[0].isdigit() else 0
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return EngineVersion(
            product="MongoDB", version=version, major=major, minor=minor, patch=patch
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, MongoDBConnection):
            connection.close()
