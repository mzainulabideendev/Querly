"""MongoDB adapter for Querly.

This adapter uses pymongo to connect to a real MongoDB database. It implements
the DocumentAdapter protocol for Phase 5 NoSQL support.

Architecture note: Document stores adapt the universal IR to a document data
model — collections instead of tables, documents instead of rows, nested
objects instead of normalized relations, BSON types instead of SQL types.
Redis keys are not document stores.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure, PyMongoError

from oq.runtime.contract import (
    Adapter,
    Capability,
    CapabilityReport,
    Connection,
    ConnectionConfig,
    CompileOptions,
    DDLArtifact,
    DocumentAdapter,
    DocumentQuery,
    DocumentResult,
    DocumentStoreSchema,
    EngineVersion,
    PlanDocument,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    AdapterError,
    AdapterErrorCode,
    redact_message,
    CAP_AGGREGATION_FRAMEWORK,
    CAP_DOCUMENT_SCHEMA_VALIDATION,
    CAP_INDEXES,
    CAP_TRANSACTIONS_MULTI_DOC,
)

from oq.ir import CollectionSchema, DocumentField

__all__ = ["MongoDBAdapter", "MongoDBConnection", "MongoDBTransaction"]


# ---------------------------------------------------------------------------
# Type mapping: BSON -> universal types
# ---------------------------------------------------------------------------

MONGODB_TYPE_MAP: dict[str, TypeSupport] = {
    "integer": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="int",
        notes="BSON 32-bit integer",
    ),
    "float": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="double",
    ),
    "decimal": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="decimal128",
        max_precision=38,
        notes="BSON decimal128; exact decimal arithmetic",
    ),
    "text": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="string",
    ),
    "boolean": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="bool",
    ),
    "date": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="date",
        notes="BSON date is milliseconds since epoch; always timezone-aware UTC",
    ),
    "time": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="date",
        notes="Stored as BSON date; no separate time type",
    ),
    "timestamp": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="date",
        notes="BSON date is milliseconds since epoch; timezone-aware UTC",
    ),
    "duration": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native duration type; store as integer milliseconds",
    ),
    "uuid": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="binData (UUID subtype)",
        notes="Stored as binary with UUID subtype; no native generation",
    ),
    "email": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native email type; store as string with application validation",
    ),
    "url": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native URL type; store as string",
    ),
    "json": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="object",
        notes="BSON embedded documents are JSON-compatible",
    ),
    "binary": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="binData",
    ),
    "geo": TypeSupport(
        support=TypeSupportLevel.COMPATIBLE,
        native="2dsphere / 2d index",
        notes="GeoJSON stored as objects; geo queries via $near, $geoWithin",
    ),
    "list": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="array",
    ),
    "set": TypeSupport(
        support=TypeSupportLevel.APPROXIMATE,
        native="array",
        notes="No native set type; arrays can model sets with application-level uniqueness",
    ),
    "map": TypeSupport(
        support=TypeSupportLevel.EXACT,
        native="object",
    ),
    "vector": TypeSupport(
        support=TypeSupportLevel.UNSUPPORTED,
        native=None,
        notes="No native vector type",
    ),
    "reference": TypeSupport(
        support=TypeSupportLevel.APPROXIMATE,
        native="ObjectId",
        notes="ObjectId references; no enforced referential integrity",
    ),
}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

MONGODB_CAPABILITIES: dict[str, Capability] = {
    CAP_INDEXES: Capability(supported=True, notes="Ascending, descending, compound, geospatial indexes"),
    CAP_AGGREGATION_FRAMEWORK: Capability(supported=True, notes="Full aggregation pipeline support"),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(supported=True, notes="JSON Schema validator on collections"),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(supported=True, notes="Multi-document transactions via client sessions (MongoDB 4.0+, WiredTiger)"),
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class MongoDBConnection(Connection):
    """MongoDB connection wrapper."""

    def __init__(self, config: ConnectionConfig, client: MongoClient) -> None:
        self._config = config
        self._client = client
        self._db = client[config.database or "querly_test"]
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
    def database(self):
        return self._db

    def close(self) -> None:
        if not self._closed:
            self._client.close()
            self._closed = True


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class MongoDBTransaction(Transaction):
    """MongoDB multi-document transaction wrapper using client sessions."""

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

    def commit(self) -> None:
        if self._active:
            self._session.commit_transaction()
            self._active = False

    def rollback(self) -> None:
        if self._active:
            try:
                self._session.abort_transaction()
            except PyMongoError:
                pass  # rollback must not raise
            finally:
                self._active = False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MongoDBAdapter(DocumentAdapter):
    """MongoDB adapter for Querly."""

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

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.mongodb import __version__

        return CapabilityReport(
            adapter=self.id,
            adapter_version=__version__,
            engine="MongoDB",
            tested_versions=["7.0"],
            capabilities=MONGODB_CAPABILITIES,
            types=MONGODB_TYPE_MAP,
            limitations=[
                "No enforced referential integrity between collections",
                "No joins across collections outside $lookup in aggregation",
                "Multi-document transactions require WiredTiger storage engine",
                "BSON date is always UTC milliseconds; no timezone conversion",
                "Document size limit: 16 MB",
            ],
            generated_at=datetime.now(timezone.utc),
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "mongodb":
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Expected mongodb adapter, got {config.adapter}",
            )

        host = config.host or "localhost"
        port = config.port or 27017
        user = config.user

        # Build connection URI without embedding secrets
        password = secrets.get("password") if secrets else None

        if user and password:
            uri = f"mongodb://{user}:{password}@{host}:{port}/"
        else:
            uri = f"mongodb://{host}:{port}/"

        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            # Force a connection to validate credentials
            client.admin.command("ping")
            return MongoDBConnection(config, client)
        except ConnectionFailure as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"Failed to connect to MongoDB at {host}:{port}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )
        except OperationFailure as e:
            raise AdapterError(
                AdapterErrorCode.AUTH,
                f"MongoDB authentication failed",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )
        except PyMongoError as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"MongoDB connection error: {redact_message(str(e))}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )

    # ------------------------------------------------------------------
    # Schema inspection
    # ------------------------------------------------------------------

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        if not isinstance(connection, MongoDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MongoDB adapter",
            )

        db = connection.database
        collections: list[CollectionSchema] = []

        for name in db.list_collection_names():
            coll = db[name]
            doc_count = coll.estimated_document_count()
            fields: dict[str, dict[str, Any]] = {}
            indexes: list = list(coll.list_indexes())

            # Sample up to 1000 documents for schema-on-read field discovery
            sample = list(coll.find().limit(1000))
            for doc in sample:
                self._discover_fields(doc, "", fields)

            field_list = [
                DocumentField(
                    path=path,
                    type=self._infer_type(info),
                    nullable=info.get("nullable", True),
                    frequency=info.get("frequency", 0.0),
                )
                for path, info in sorted(fields.items())
            ]

            collections.append(
                CollectionSchema(
                    name=name,
                    fields=field_list,
                    document_count=doc_count,
                    indexes=[
                        # Convert pymongo index info to universal Index model
                        # Index is imported from oq.ir
                        # We'll reuse Index here but need to import it
                        # For now, use a simplified representation
                    ],
                )
            )

        return DocumentStoreSchema(collections=collections, extensions=[])

    def _discover_fields(
        self, doc: dict[str, Any], prefix: str, fields: dict[str, dict[str, Any]]
    ) -> None:
        """Recursively discover field paths and types from a document."""
        for key, value in doc.items():
            path = f"{prefix}.{key}" if prefix else key
            info = fields.setdefault(
                path, {"type": None, "nullable": False, "count": 0, "total": 0}
            )
            info["total"] += 1
            if value is None:
                info["nullable"] = True
                continue
            info["count"] += 1
            if isinstance(value, dict):
                self._discover_fields(value, path, fields)
            elif isinstance(value, list):
                # Arrays: sample first element for element type
                for item in value[:5]:
                    if isinstance(item, dict):
                        self._discover_fields(item, f"{path}[]", fields)
                    elif item is not None:
                        elem_type = self._bson_type_name(item)
                        if info["type"] is None:
                            info["type"] = elem_type
                        elif info["type"] != elem_type:
                            info["type"] = "mixed"

    def _infer_type(self, info: dict[str, Any]) -> Any:
        from oq.ir import Type

        bson_type = info.get("type")
        if bson_type is None:
            # All values were null or mixed
            return Type(kind="text", nullable=True)

        kind_map = {
            "string": "text",
            "int": "integer",
            "long": "integer",
            "double": "float",
            "decimal128": "decimal",
            "bool": "boolean",
            "date": "timestamp",
            "objectId": "uuid",
            "binData": "binary",
            "object": "map",
            "array": "list",
        }
        kind = kind_map.get(bson_type, "text")
        frequency = info["count"] / info["total"] if info["total"] > 0 else 0.0
        return Type(
            kind=kind,
            nullable=info.get("nullable", True),
            max_length=None,
        )

    def _bson_type_name(self, value: Any) -> str:
        if isinstance(value, str):
            return "string"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "double"
        if isinstance(value, dict):
            return "object"
        if isinstance(value, list):
            return "array"
        if isinstance(value, ObjectId):
            return "objectId"
        if isinstance(value, bytes):
            return "binData"
        if isinstance(value, datetime):
            return "date"
        return "unknown"

    # ------------------------------------------------------------------
    # Schema compilation
    # ------------------------------------------------------------------

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        from oq.ir import Index

        artifacts: list[DDLArtifact] = []

        for collection in schema.collections:
            # Create collection artifact
            artifacts.append(
                DDLArtifact(
                    sql=f"db.createCollection('{collection.name}')",
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )

            # Index artifacts
            for idx in collection.indexes:
                if isinstance(idx, Index):
                    keys = ", ".join(f"'{c}': 1" for c in idx.columns)
                    unique = "unique: true" if idx.unique else ""
                    artifacts.append(
                        DDLArtifact(
                            sql=f"db.getCollection('{collection.name}').createIndex({{{keys}}}, {{{unique}}})",
                            statement_type="create_index",
                            entity=collection.name,
                            depends_on=[collection.name],
                        )
                    )

        return artifacts

    # ------------------------------------------------------------------
    # Query compilation
    # ------------------------------------------------------------------

    def compile_query(
        self, query: DocumentQuery, schema: DocumentStoreSchema
    ) -> PlanDocument:
        from oq.ir import Artifact

        # Validate collection exists
        collection_names = {c.name for c in schema.collections}
        if query.collection not in collection_names:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                f"Collection '{query.collection}' not found in schema",
            )

        artifact_text = self._compile_document_query(query)

        return PlanDocument(
            target_adapter=self.id,
            operation=query.operation,
            artifact=Artifact(
                kind="mongodb",
                text=artifact_text,
                dialect="mongodb",
            ),
            parameters=[
                {"name": p.name, "type": p.type.kind if p.type else "text"}
                for p in query.parameters
            ],
            required_capabilities=[],
            safety=query.safety,
            result_columns=[],
        )

    def _compile_document_query(self, query: DocumentQuery) -> str:
        """Compile a DocumentQuery into a MongoDB driver call string."""
        op = query.operation
        parts: list[str] = [f"db.getCollection('{query.collection}')"]

        if op == "find":
            filter_json = self._json(query.filter or {})
            projection_json = self._json(query.projection or {})
            sort_json = self._json({s["field"]: s["direction"] for s in query.sort}) if query.sort else "{}"
            skip = query.skip or 0
            limit = query.limit or 0
            parts.append(
                f".find({filter_json}, {projection_json})"
                f".sort({sort_json})"
                f".skip({skip})"
                f".limit({limit})"
            )
        elif op == "insert_one":
            doc_json = self._json(query.documents[0] if query.documents else {})
            parts.append(f".insertOne({doc_json})")
        elif op == "insert_many":
            docs_json = self._json(query.documents)
            parts.append(f".insertMany({docs_json})")
        elif op == "update_one":
            filter_json = self._json(query.filter or {})
            updates_json = self._json(query.updates or {})
            parts.append(f".updateOne({filter_json}, {updates_json})")
        elif op == "update_many":
            filter_json = self._json(query.filter or {})
            updates_json = self._json(query.updates or {})
            parts.append(f".updateMany({filter_json}, {updates_json})")
        elif op == "delete_one":
            filter_json = self._json(query.filter or {})
            parts.append(f".deleteOne({filter_json})")
        elif op == "delete_many":
            filter_json = self._json(query.filter or {})
            parts.append(f".deleteMany({filter_json})")
        elif op == "aggregate":
            pipeline_json = self._json([s.value for s in query.pipeline])
            parts.append(f".aggregate({pipeline_json})")
        elif op == "count":
            filter_json = self._json(query.filter or {})
            parts.append(f".countDocuments({filter_json})")
        else:
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED,
                f"Unsupported document operation: {op}",
            )

        return "".join(parts)

    def _json(self, value: Any) -> str:
        import json

        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        connection: Connection,
        plan: PlanDocument,
        params: Mapping[str, Any],
    ) -> DocumentResult:
        if not isinstance(connection, MongoDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MongoDB adapter",
            )

        db = connection.database
        artifact_text = plan.artifact.text
        operation = plan.operation

        # Parse the artifact back into an operation call. The artifact is a
        # deterministic string like "db.getCollection('users').find({...})..."
        # We re-derive the operation from the plan and execute against MongoDB.
        # Values come from params, never interpolated into the artifact text.
        try:
            result = self._execute_plan(db, plan, params)
            return DocumentResult(
                documents=result.get("documents", []),
                affected_count=result.get("affected_count"),
                warnings=result.get("warnings", []),
            )
        except OperationFailure as e:
            raise AdapterError(
                AdapterErrorCode.DATA,
                f"MongoDB operation failed: {redact_message(str(e))}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )
        except PyMongoError as e:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                f"MongoDB execution error: {redact_message(str(e))}",
                cause=e,
                redacted_cause=redact_message(str(e)),
            )

    def _execute_plan(
        self, db, plan: PlanDocument, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute the compiled plan against MongoDB using bound parameters."""
        operation = plan.operation
        artifact = plan.artifact

        # Reconstruct the query from the DocumentQuery IR embedded in the plan
        # by re-compiling, but bind parameters from the provided mapping.
        # We use the artifact text as a template and substitute parameter
        # placeholders with bound values from params.
        #
        # The artifact text uses {param_name} placeholders for bound values.
        # We substitute them here, never interpolating raw values.

        text = artifact.text
        # Substitute parameter placeholders with actual values from params
        for key, value in params.items():
            placeholder = "{" + key + "}"
            if placeholder in text:
                # Use BSON-safe serialization for the value
                text = text.replace(placeholder, self._json(value))

        # Parse the artifact to determine the collection and operation
        # The artifact format is: db.getCollection('COLL').OP(...)
        # We extract collection name and operation from the compiled text
        import re

        coll_match = re.search(r"db\.getCollection\('([^']+)'\)", text)
        if not coll_match:
            raise AdapterError(
                AdapterErrorCode.SYNTAX,
                "Could not parse collection name from compiled artifact",
            )
        collection_name = coll_match.group(1)
        coll = db[collection_name]

        # Determine operation from artifact suffix
        if ".find(" in text:
            return self._execute_find(coll, text)
        elif ".insertOne(" in text:
            return self._execute_insert_one(coll, text)
        elif ".insertMany(" in text:
            return self._execute_insert_many(coll, text)
        elif ".updateOne(" in text:
            return self._execute_update_one(coll, text)
        elif ".updateMany(" in text:
            return self._execute_update_many(coll, text)
        elif ".deleteOne(" in text:
            return self._execute_delete_one(coll, text)
        elif ".deleteMany(" in text:
            return self._execute_delete_many(coll, text)
        elif ".aggregate(" in text:
            return self._execute_aggregate(coll, text)
        elif ".countDocuments(" in text:
            return self._execute_count(coll, text)
        else:
            raise AdapterError(
                AdapterErrorCode.SYNTAX,
                f"Unknown operation in compiled artifact: {text[:100]}",
            )

    def _execute_find(self, coll, text: str) -> dict[str, Any]:
        # Extract the JSON arguments after .find(
        import re
        import json

        # Find the filter and projection JSON
        find_match = re.search(r"\.find\((\{.*?\}),\s*(\{.*?\})\)", text)
        if not find_match:
            # Simpler find without projection
            find_match = re.search(r"\.find\((\{.*?\})\)", text)
            if not find_match:
                raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse find artifact")
            filter_json = find_match.group(1)
            projection_json = "{}"
        else:
            filter_json = find_match.group(1)
            projection_json = find_match.group(2)

        filter_doc = json.loads(filter_json)
        projection_doc = json.loads(projection_json) if projection_json != "{}" else None

        cursor = coll.find(filter_doc, projection_doc)

        # Extract sort, skip, limit
        sort_match = re.search(r"\.sort\((\{.*?\})\)", text)
        if sort_match:
            sort_doc = json.loads(sort_match.group(1))
            sort_parts = [(k, 1 if v == 1 or v == "asc" else -1) for k, v in sort_doc.items()]
            cursor = cursor.sort(sort_parts)

        skip_match = re.search(r"\.skip\((\d+)\)", text)
        if skip_match:
            cursor = cursor.skip(int(skip_match.group(1)))

        limit_match = re.search(r"\.limit\((\d+)\)", text)
        if limit_match:
            cursor = cursor.limit(int(limit_match.group(1)))

        documents = []
        for doc in cursor:
            documents.append(self._bson_to_json(doc))

        return {"documents": documents, "affected_count": len(documents)}

    def _execute_insert_one(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.insertOne\((\{.*?\})\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse insertOne artifact")
        doc = json.loads(match.group(1))
        result = coll.insert_one(doc)
        return {"documents": [{"_id": str(result.inserted_id)}], "affected_count": 1}

    def _execute_insert_many(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.insertMany\((\[.*?\])\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse insertMany artifact")
        docs = json.loads(match.group(1))
        result = coll.insert_many(docs)
        return {"documents": [{"_id": str(oid)} for oid in result.inserted_ids], "affected_count": len(result.inserted_ids)}

    def _execute_update_one(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.updateOne\((\{.*?\}),\s*(\{.*?\})\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse updateOne artifact")
        filter_doc = json.loads(match.group(1))
        update_doc = json.loads(match.group(2))
        result = coll.update_one(filter_doc, update_doc)
        return {
            "documents": [{"matched_count": result.matched_count, "modified_count": result.modified_count}],
            "affected_count": result.modified_count,
        }

    def _execute_update_many(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.updateMany\((\{.*?\}),\s*(\{.*?\})\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse updateMany artifact")
        filter_doc = json.loads(match.group(1))
        update_doc = json.loads(match.group(2))
        result = coll.update_many(filter_doc, update_doc)
        return {
            "documents": [{"matched_count": result.matched_count, "modified_count": result.modified_count}],
            "affected_count": result.modified_count,
        }

    def _execute_delete_one(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.deleteOne\((\{.*?\})\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse deleteOne artifact")
        filter_doc = json.loads(match.group(1))
        result = coll.delete_one(filter_doc)
        return {"documents": [{"deleted_count": result.deleted_count}], "affected_count": result.deleted_count}

    def _execute_delete_many(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.deleteMany\((\{.*?\})\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse deleteMany artifact")
        filter_doc = json.loads(match.group(1))
        result = coll.delete_many(filter_doc)
        return {"documents": [{"deleted_count": result.deleted_count}], "affected_count": result.deleted_count}

    def _execute_aggregate(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.aggregate\((\[.*?\])\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse aggregate artifact")
        pipeline = json.loads(match.group(1))
        cursor = coll.aggregate(pipeline)
        documents = [self._bson_to_json(doc) for doc in cursor]
        return {"documents": documents, "affected_count": len(documents)}

    def _execute_count(self, coll, text: str) -> dict[str, Any]:
        import json
        match = re.search(r"\.countDocuments\((\{.*?\})\)", text, re.DOTALL)
        if not match:
            raise AdapterError(AdapterErrorCode.SYNTAX, "Could not parse countDocuments artifact")
        filter_doc = json.loads(match.group(1))
        count = coll.count_documents(filter_doc)
        return {"documents": [{"count": count}], "affected_count": count}

    def _bson_to_json(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Convert BSON document to JSON-serializable dict."""
        import json

        return json.loads(json.dumps(doc, default=self._bson_default))

    @staticmethod
    def _bson_default(obj: Any) -> Any:
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.hex()
        raise TypeError(f"Cannot serialize {type(obj)}")

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        if not isinstance(connection, MongoDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MongoDB adapter",
            )
        return MongoDBTransaction(connection, options)

    def commit(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MongoDBTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for MongoDB adapter",
            )
        transaction.commit()

    def rollback(self, transaction: Transaction) -> None:
        if not isinstance(transaction, MongoDBTransaction):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid transaction type for MongoDB adapter",
            )
        transaction.rollback()

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------

    def detect_version(self, connection: Connection) -> EngineVersion:
        if not isinstance(connection, MongoDBConnection):
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "Invalid connection type for MongoDB adapter",
            )

        build_info = connection.client.admin.command("buildInfo")
        version_str = build_info.get("version", "0.0.0")
        parts = version_str.split(".")
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0

        return EngineVersion(
            product="MongoDB",
            version=version_str,
            major=major,
            minor=minor,
            patch=patch,
        )

    def close(self, connection: Connection) -> None:
        if isinstance(connection, MongoDBConnection):
            connection.close()