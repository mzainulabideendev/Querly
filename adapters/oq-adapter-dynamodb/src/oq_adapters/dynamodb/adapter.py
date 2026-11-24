"""Amazon DynamoDB adapter for Querly.

DynamoDB is a managed key-value/document store with its own expression language,
capacity model, and consistency rules. This adapter maps a collection to a table
and a document to an item. Filters are compiled into DynamoDB expression
attribute names/values; values are never concatenated into an expression.

The adapter supports point reads, scans, key-condition queries, item writes, and
deletes. It does not pretend that ``TransactWriteItems`` is an interactive
transaction: ``begin`` reports a capability error.

Driver: ``boto3`` with the low-level DynamoDB client, imported lazily.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Mapping

from oq.ir import (
    Artifact,
    CollectionSchema,
    DocumentField,
    DocumentQuery,
    DocumentStoreSchema,
    Index,
    PlanDocument,
    Type,
)
from oq.runtime.contract import (
    CAP_AGGREGATION_FRAMEWORK,
    CAP_ARRAY_TYPE,
    CAP_BATCH_WRITES,
    CAP_CHANGE_STREAMS,
    CAP_CHECK_CONSTRAINTS,
    CAP_DECIMAL_UNBOUNDED,
    CAP_DOCUMENT_CRUD,
    CAP_DOCUMENT_SCHEMA_VALIDATION,
    CAP_DURATION_TYPE,
    CAP_EMAIL_TYPE,
    CAP_FOREIGN_KEYS,
    CAP_FULL_TEXT_SEARCH,
    CAP_GEO_TYPE,
    CAP_GROUP_BY,
    CAP_HAVING,
    CAP_INDEXES,
    CAP_JOINS,
    CAP_JSON,
    CAP_MATERIALIZED_VIEWS,
    CAP_MULTI_KEY_ATOMICITY,
    CAP_NESTED_DOCUMENTS,
    CAP_ORDER_BY_NULLS,
    CAP_PARTIAL_INDEXES,
    CAP_PARTITIONING,
    CAP_REPLICATION,
    CAP_RETURNING,
    CAP_SEARCH_QUERY_DSL,
    CAP_SECONDARY_INDEXES,
    CAP_SERVER_SIDE_SCRIPTING,
    CAP_STRONG_CONSISTENCY,
    CAP_TRANSACTIONAL_DDL,
    CAP_TRANSACTIONS,
    CAP_TRANSACTIONS_MULTI_DOC,
    CAP_TTL,
    CAP_UNIQUE_CONSTRAINTS,
    CAP_UPSERT,
    CAP_UPSERT_BY_KEY,
    CAP_URL_TYPE,
    CAP_UUID_TYPE,
    CAP_VECTOR_SEARCH,
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
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
    redact_message,
)
from oq_adapters.kit import build_report, diagnostic, json_safe, type_kind
from oq_adapters.kit.document import (
    check_collection,
    check_document_operation,
    check_unsafe_filter_operators,
    document_safety,
    normalize_document,
    unsupported_document_plan,
)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    boto3 = None
    BotoCoreError = ClientError = Exception

__all__ = ["DynamoDBAdapter", "DynamoDBConnection"]

_SUPPORTED = {
    "find",
    "find_one",
    "scan",
    "count",
    "get",
    "insert_one",
    "insert_many",
    "put",
    "update_one",
    "upsert",
    "delete_one",
    "delete_many",
}
_WRITES = {"insert_one", "insert_many", "put", "update_one", "upsert"}
_DESTRUCTIVE = {"delete_one", "delete_many"}


class DynamoDBConnection(Connection):
    def __init__(self, config: ConnectionConfig, client: Any) -> None:
        self._config = config
        self._client = client
        self._closed = False

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def client(self) -> Any:
        return self._client

    def ping(self) -> bool:
        try:
            self._client.list_tables(Limit=1)
            return True
        except Exception:
            return False

    def close(self) -> None:
        if not self._closed:
            close = getattr(self._client, "close", None)
            if close:
                close()
            self._closed = True


class DynamoDBAdapter(DocumentAdapter):
    data_model = "key-value"
    ir_subset = ("schema", "document CRUD", "scan", "query", "count")

    @property
    def id(self) -> str:
        return "dynamodb"

    @property
    def display_name(self) -> str:
        return "Amazon DynamoDB"

    @property
    def package_version(self) -> str:
        from oq_adapters.dynamodb import __version__

        return __version__

    def capabilities(self) -> CapabilityReport:
        from oq_adapters.dynamodb import __version__

        return build_report(
            adapter=self.id,
            adapter_version=__version__,
            engine="Amazon DynamoDB",
            tested_versions=[],
            capabilities=DYNAMODB_CAPS,
            types=DYNAMODB_TYPES,
            limitations=[
                "No tested engine version is claimed in this environment.",
                "No interactive multi-statement transactions; TransactWriteItems is limited to 100 atomic items and is not exposed as a transaction object.",
                "No joins, GROUP BY, HAVING, aggregation pipeline, or full-text search.",
                "Scans are eventually consistent unless ConsistentRead is requested; queries are partition-scoped.",
                "The item count reported by DescribeTable can lag by roughly six hours.",
                "Secondary indexes only project selected attributes unless ALL is requested.",
            ],
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        if config.adapter != "dynamodb":
            raise AdapterError(AdapterErrorCode.CONNECTION, "Expected a dynamodb config.")
        if boto3 is None:
            raise AdapterError(
                AdapterErrorCode.CONNECTION,
                "boto3 is not installed. Install oq-adapter-dynamodb[driver].",
            )
        region = config.options.get("region") or config.options.get("aws_region")
        if not region:
            raise AdapterError(AdapterErrorCode.CONNECTION, "DynamoDB requires an AWS region.")
        credentials: dict[str, Any] = {}
        access_key = secrets.get("access_key_id")
        secret_key = secrets.get("secret_access_key")
        if access_key and secret_key:
            credentials["aws_access_key_id"] = access_key
            credentials["aws_secret_access_key"] = secret_key
            token = secrets.get("session_token")
            if token:
                credentials["aws_session_token"] = token
        try:
            client = boto3.client(
                "dynamodb",
                region_name=region,
                endpoint_url=config.options.get("endpoint_url") or None,
                **credentials,
            )
        except Exception as exc:
            raise self._error(exc, "connect") from exc
        return DynamoDBConnection(config, client)

    def detect_version(self, connection: Connection) -> EngineVersion:
        conn = self._require(connection)
        try:
            # DynamoDB has no version banner. The API itself is versioned by the
            # botocore service model; report that honestly instead of inventing
            # an engine release.
            model = conn.client.meta.service_model
            api_version = str(getattr(model, "api_version", "unknown"))
        except Exception as exc:
            raise self._error(exc, "detect_version") from exc
        return EngineVersion(product="Amazon DynamoDB", version=api_version, major=0, minor=0, patch=0)

    def close(self, connection: Connection) -> None:
        if isinstance(connection, DynamoDBConnection):
            try:
                connection.close()
            except Exception:
                pass

    def inspect_schema(self, connection: Connection) -> DocumentStoreSchema:
        conn = self._require(connection)
        try:
            names: list[str] = []
            paginator = conn.client.get_paginator("list_tables")
            for page in paginator.paginate():
                names.extend(page.get("TableNames", []))
            collections = []
            for name in names:
                description = conn.client.describe_table(TableName=name)["Table"]
                fields = []
                for key in description.get("KeySchema", []):
                    native = next(
                        (
                            item["AttributeType"]
                            for item in description.get("AttributeDefinitions", [])
                            if item["AttributeName"] == key["AttributeName"]
                        ),
                        "S",
                    )
                    fields.append(
                        DocumentField(
                            path=str(key["AttributeName"]),
                            type=Type(kind=_dynamo_kind(native)),
                            nullable=False,
                            frequency=1.0,
                        )
                    )
                indexes = []
                for kind, key in (
                    ("gsi", "GlobalSecondaryIndexes"),
                    ("lsi", "LocalSecondaryIndexes"),
                ):
                    for item in description.get(key, []) or []:
                        indexes.append(
                            Index(
                                name=str(item["IndexName"]),
                                columns=[str(part["AttributeName"]) for part in item.get("KeySchema", [])],
                                unique=False,
                                kind=f"dynamodb-{kind}",
                                keys=[
                                    {
                                        "field": str(part["AttributeName"]),
                                        "role": str(part["KeyType"]),
                                    }
                                    for part in item.get("KeySchema", [])
                                ],
                                options={"projection": item.get("Projection", {})},
                            )
                        )
                collections.append(
                    CollectionSchema(
                        name=name,
                        fields=fields,
                        document_count=int(description.get("ItemCount", 0)),
                        indexes=indexes,
                    )
                )
        except Exception as exc:
            raise self._error(exc, "inspect_schema") from exc
        return DocumentStoreSchema(collections=collections)

    def compile_schema(
        self, schema: DocumentStoreSchema, options: CompileOptions
    ) -> list[DDLArtifact]:
        artifacts: list[DDLArtifact] = []
        for collection in schema.collections:
            if not collection.fields:
                raise AdapterError(
                    AdapterErrorCode.CAPABILITY,
                    f"DynamoDB table {collection.name!r} needs at least a partition key.",
                )
            key_names = [field.path for field in collection.fields]
            definitions = [
                {"AttributeName": field.path, "AttributeType": _dynamo_type(field.type.kind)}
                for field in collection.fields
            ]
            key_schema = [
                {"AttributeName": key_names[0], "KeyType": "HASH"},
            ]
            if len(key_names) > 1:
                key_schema.append({"AttributeName": key_names[1], "KeyType": "RANGE"})
            table: dict[str, Any] = {
                "TableName": collection.name,
                "KeySchema": key_schema,
                "AttributeDefinitions": definitions,
                "BillingMode": "PAY_PER_REQUEST",
            }
            if len(key_names) > 2:
                artifacts.append(
                    DDLArtifact(
                        sql=json_safe(
                            diagnostic(
                                AdapterErrorCode.CAPABILITY,
                                "DynamoDB supports at most two key attributes (partition + sort); extra keys must become a secondary index.",
                                path=collection.name,
                            ).model_dump(mode="json")
                        ),
                        statement_type="diagnostic",
                        entity=collection.name,
                    )
                )
            for index in collection.indexes:
                gsi = {
                    "IndexName": index.name,
                    "KeySchema": [
                        {
                            "AttributeName": str(item["field"]),
                            "KeyType": "HASH" if position == 0 else "RANGE",
                        }
                        for position, item in enumerate(index.keys or [{"field": c} for c in index.columns])
                    ],
                    "Projection": index.options.get("projection", {"ProjectionType": "ALL"}),
                }
                table.setdefault("GlobalSecondaryIndexes", []).append(gsi)
            artifacts.append(
                DDLArtifact(
                    sql=json_safe({"CreateTable": table}),
                    statement_type="create_collection",
                    entity=collection.name,
                )
            )
        return artifacts

    def compile_query(self, query: DocumentQuery, schema: DocumentStoreSchema) -> PlanDocument:
        diagnostics = check_document_operation(query, _SUPPORTED, engine="DynamoDB")
        diagnostics.extend(check_collection(query, {c.name for c in schema.collections}, engine="DynamoDB"))
        diagnostics.extend(
            check_unsafe_filter_operators(
                query.filter,
                forbidden={"$where", "$function", "$expr", "$accumulator"},
                engine="DynamoDB",
            )
        )
        if query.pipeline:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "DynamoDB has no aggregation pipeline.",
                    operation=query.operation,
                    feature="pipeline",
                )
            )
        if query.skip or query.projection or query.sort:
            diagnostics.append(
                diagnostic(
                    AdapterErrorCode.UNSUPPORTED,
                    "DynamoDB does not support relation-style skip/sort/projection; use a key query or an index.",
                    operation=query.operation,
                    feature="query_shape",
                )
            )
        if diagnostics:
            return unsupported_document_plan(self.id, "dynamodb", query, diagnostics)

        table = query.collection
        request: dict[str, Any] = {"TableName": table}
        operation = query.operation
        names: dict[str, str] = {}
        values: dict[str, Any] = {}

        def name_ref(field: str) -> str:
            key = f"#n{len(names)}"
            names[key] = field
            return key

        def value_ref(value: Any) -> str:
            key = f":v{len(values)}"
            values[key] = value
            return key

        expression = _filter_expression(query.filter, name_ref, value_ref) if query.filter else None
        key_expression = _key_expression(query.filter, schema, table, name_ref, value_ref) if query.filter else None

        if operation in ("find", "find_one", "scan", "count"):
            request["Limit"] = int(query.limit or (1 if operation == "find_one" else 100))
            if key_expression and operation not in ("scan",):
                request["KeyConditionExpression"] = key_expression
                request["Select"] = "COUNT" if operation == "count" else "ALL_ATTRIBUTES"
                native_operation = "query"
            else:
                if expression:
                    request["FilterExpression"] = expression
                request["Select"] = "COUNT" if operation == "count" else "ALL_ATTRIBUTES"
                native_operation = "scan"
            request["ConsistentRead"] = bool(getattr(query, 'options', {}).get("consistent_read", False))
        elif operation in ("get",):
            key_names = _key_names(schema, table)
            if not query.filter:
                raise AdapterError(AdapterErrorCode.SYNTAX, "DynamoDB get requires a key filter.")
            request["Key"] = _key_values(query.filter, key_names, value_ref)
            native_operation = "get_item"
        elif operation in ("insert_one", "insert_many", "put") and query.documents:
            request["Item"] = _to_dynamo_item(query.documents[0])
            native_operation = "put_item"
        elif operation in ("update_one", "upsert"):
            if not query.filter:
                raise AdapterError(AdapterErrorCode.SYNTAX, "DynamoDB update requires a key filter.")
            key_names = _key_names(schema, table)
            request["Key"] = _key_values(query.filter, key_names, value_ref)
            updates = query.updates or {}
            if not updates:
                raise AdapterError(AdapterErrorCode.SYNTAX, "DynamoDB update requires update values.")
            set_parts = []
            for field, value in updates.items():
                set_parts.append(f"{name_ref(str(field))} = {value_ref(_to_dynamo_value(value))}")
            request["UpdateExpression"] = "SET " + ", ".join(set_parts)
            native_operation = "update_item"
        elif operation in ("delete_one", "delete_many"):
            if not query.filter:
                raise AdapterError(AdapterErrorCode.SYNTAX, "DynamoDB delete requires a key filter.")
            key_names = _key_names(schema, table)
            request["Key"] = _key_values(query.filter, key_names, value_ref)
            native_operation = "delete_item"
        else:  # pragma: no cover
            raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported operation {operation!r}.")

        if names:
            request["ExpressionAttributeNames"] = names
        if values:
            request["ExpressionAttributeValues"] = values

        return PlanDocument(
            target_adapter=self.id,
            operation=operation,
            artifact=Artifact(
                kind="dynamodb-request",
                text=json_safe({"operation": native_operation, "request": request}),
                dialect="dynamodb",
            ),
            parameters=[
                {"name": parameter.name, "type": type_kind(parameter.type)}
                for parameter in query.parameters
            ],
            safety=document_safety(query, write_ops=_WRITES | _DESTRUCTIVE),
            result_columns=[{"name": "item", "type": "map", "nullable": True}],
            engine_plan={"kind": "dynamodb", "operation": native_operation, "request": json_safe(request)},
            diagnostics=[],
            supported=True,
        )

    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, Any]
    ) -> DocumentResult:
        conn = self._require(connection)
        if not plan.supported or not plan.engine_plan:
            raise AdapterError(
                AdapterErrorCode.CAPABILITY,
                "Plan is unsupported and was not executed.",
                diagnostics=[item.model_dump(mode="json") for item in plan.diagnostics],
            )
        native = plan.engine_plan
        operation = native["operation"]
        request = native["request"]
        client = conn.client
        try:
            if operation == "scan":
                response = client.scan(**request)
                return DocumentResult(
                    documents=[normalize_document(_from_dynamo_item(item)) for item in response.get("Items", [])],
                    affected_count=int(response.get("Count", 0)),
                    warnings=[],
                )
            if operation == "query":
                response = client.query(**request)
                if request.get("Select") == "COUNT":
                    return DocumentResult(documents=[], affected_count=int(response.get("Count", 0)), warnings=[])
                return DocumentResult(
                    documents=[normalize_document(_from_dynamo_item(item)) for item in response.get("Items", [])],
                    affected_count=int(response.get("Count", 0)),
                    warnings=[],
                )
            if operation == "get_item":
                response = client.get_item(**request)
                item = response.get("Item")
                return DocumentResult(
                    documents=[normalize_document(_from_dynamo_item(item))] if item else [],
                    affected_count=1 if item else 0,
                    warnings=[],
                )
            if operation == "put_item":
                client.put_item(**request)
                return DocumentResult(documents=[], affected_count=1, warnings=[])
            if operation == "update_item":
                response = client.update_item(**request, ReturnValues="ALL_NEW")
                return DocumentResult(
                    documents=[normalize_document(_from_dynamo_item(response.get("Attributes", {})))],
                    affected_count=1,
                    warnings=[],
                )
            if operation == "delete_item":
                client.delete_item(**request)
                return DocumentResult(documents=[], affected_count=1, warnings=[])
        except Exception as exc:
            raise self._error(exc, operation) from exc
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"Unsupported DynamoDB operation {operation!r}.")

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.CAPABILITY,
            "DynamoDB has no interactive transactions; use TransactWriteItems explicitly (max 100 items).",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(AdapterErrorCode.CAPABILITY, "DynamoDB has no interactive transactions.")

    def rollback(self, transaction: Transaction) -> None:
        return None

    def _require(self, connection: Connection) -> DynamoDBConnection:
        if not isinstance(connection, DynamoDBConnection):
            raise AdapterError(AdapterErrorCode.CONNECTION, "Wrong connection type for DynamoDB.")
        return connection

    @staticmethod
    def _error(exc: BaseException, operation: str) -> AdapterError:
        text = redact_message(str(exc))
        code = AdapterErrorCode.CONNECTION
        retryable = False
        if isinstance(exc, ClientError):
            error = exc.response.get("Error", {})
            name = str(error.get("Code", ""))
            if name in ("UnrecognizedClientException", "InvalidSignatureException", "AccessDeniedException"):
                code = AdapterErrorCode.AUTH
            elif name in ("ConditionalCheckFailedException", "ResourceNotFoundException"):
                code = AdapterErrorCode.CONSTRAINT if "Conditional" in name else AdapterErrorCode.CAPABILITY
            elif name in ("ProvisionedThroughputExceededException", "ThrottlingException", "RequestLimitExceeded"):
                code = AdapterErrorCode.TIMEOUT
                retryable = True
            elif name in ("ValidationException", "SerializationException"):
                code = AdapterErrorCode.SYNTAX
        elif isinstance(exc, BotoCoreError):
            retryable = True
        return AdapterError(code, f"DynamoDB {operation} failed: {text}", retryable=retryable, cause=exc, redacted_cause=text)


def _dynamo_type(kind: str) -> str:
    return {
        "integer": "N",
        "bigint": "N",
        "float": "N",
        "double": "N",
        "decimal": "N",
        "text": "S",
        "date": "S",
        "time": "S",
        "timestamp": "S",
        "datetime": "S",
        "duration": "N",
        "uuid": "S",
        "email": "S",
        "url": "S",
        "json": "M",
        "binary": "B",
        "list": "L",
        "set": "SS",
        "map": "M",
    }.get(kind, "S")


def _dynamo_kind(native: str) -> str:
    return {
        "S": "text",
        "N": "decimal",
        "B": "binary",
        "BOOL": "boolean",
        "NULL": "null",
        "L": "list",
        "M": "map",
        "SS": "set",
        "NS": "set",
        "BS": "set",
    }.get(native, "text")


def _to_dynamo_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"B": bytes(value)}
    if isinstance(value, (date, datetime, time)):
        return {"S": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return {"L": [_to_dynamo_value(item) for item in value]}
    if isinstance(value, set):
        return {"SS": [str(item) for item in sorted(value, key=str)]}
    if isinstance(value, dict):
        return {"M": {str(key): _to_dynamo_value(item) for key, item in value.items()}}
    return {"S": str(value)}


def _to_dynamo_item(document: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _to_dynamo_value(value) for key, value in document.items()}


def _from_dynamo_value(value: dict[str, Any]) -> Any:
    if "NULL" in value:
        return None
    if "BOOL" in value:
        return value["BOOL"]
    if "N" in value:
        number = Decimal(value["N"])
        return int(number) if number == number.to_integral_value() else number
    if "S" in value:
        return value["S"]
    if "B" in value:
        return value["B"]
    if "L" in value:
        return [_from_dynamo_value(item) for item in value["L"]]
    if "M" in value:
        return {key: _from_dynamo_value(item) for key, item in value["M"].items()}
    for key in ("SS", "NS", "BS"):
        if key in value:
            return list(value[key])
    return None


def _from_dynamo_item(item: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _from_dynamo_value(value) for key, value in item.items()}


def _filter_expression(node: Any, name_ref: Any, value_ref: Any) -> str:
    if not isinstance(node, dict):
        raise AdapterError(AdapterErrorCode.SYNTAX, "DynamoDB filter must be an object.")
    if "and" in node and isinstance(node["and"], list):
        return "(" + " AND ".join(_filter_expression(item, name_ref, value_ref) for item in node["and"]) + ")"
    if "or" in node and isinstance(node["or"], list):
        return "(" + " OR ".join(_filter_expression(item, name_ref, value_ref) for item in node["or"]) + ")"
    if "not" in node:
        return f"(NOT {_filter_expression(node['not'], name_ref, value_ref)})"
    field = node.get("field") or node.get("name")
    if not field:
        raise AdapterError(AdapterErrorCode.SYNTAX, "DynamoDB filter has no field.")
    if "eq" in node:
        return f"{name_ref(str(field))} = {value_ref(_to_dynamo_value(node['eq']))}"
    if "in" in node:
        return f"{name_ref(str(field))} IN (" + ", ".join(value_ref(_to_dynamo_value(item)) for item in node["in"]) + ")"
    if "exists" in node:
        return f"attribute_exists({name_ref(str(field))})" if node["exists"] else f"attribute_not_exists({name_ref(str(field))})"
    if "range" in node:
        parts = []
        for key, operator in (("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<=")):
            if key in node["range"]:
                parts.append(f"{name_ref(str(field))} {operator} {value_ref(_to_dynamo_value(node['range'][key]))}")
        return " AND ".join(parts) or "attribute_exists(" + name_ref(str(field)) + ")"
    operator = str(node.get("operator") or "").upper()
    operand = _to_dynamo_value(node.get("value") if "value" in node else node.get("operand"))
    mapping = {"=": "=", "EQ": "=", "!=": "<>", "<>": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
    if operator not in mapping:
        raise AdapterError(AdapterErrorCode.UNSUPPORTED, f"DynamoDB filter operator {operator!r} is unsupported.")
    if operator in ("!=", "<>"):
        return f"({name_ref(str(field))} {mapping[operator]} {value_ref(operand)})"
    return f"{name_ref(str(field))} {mapping[operator]} {value_ref(operand)}"


def _key_names(schema: DocumentStoreSchema, table: str) -> list[str]:
    for collection in schema.collections:
        if collection.name == table:
            return [field.path for field in collection.fields[:2]]
    raise AdapterError(AdapterErrorCode.CAPABILITY, f"Table {table!r} is not in the schema.")


def _key_values(
    node: Any, key_names: list[str], value_ref: Any
) -> dict[str, Any]:
    values = _extract_equalities(node)
    result = {}
    for name in key_names:
        if name not in values:
            raise AdapterError(
                AdapterErrorCode.SYNTAX,
                f"DynamoDB key operation requires equality on key attribute {name!r}.",
            )
        result[_dynamo_key_name(name)] = _to_dynamo_value(values[name])
    return result


def _dynamo_key_name(name: str) -> str:
    return name


def _extract_equalities(node: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(node, dict):
        field = node.get("field") or node.get("name")
        if isinstance(field, str) and "eq" in node:
            found[field] = node["eq"]
        elif isinstance(field, str) and str(node.get("operator", "")).upper() in ("=", "EQ"):
            found[field] = node.get("value", node.get("operand"))
        for key in ("and", "or"):
            if isinstance(node.get(key), list):
                for item in node[key]:
                    found.update(_extract_equalities(item))
        for key, value in node.items():
            if key not in ("field", "name", "eq", "operator", "value", "operand"):
                found.update(_extract_equalities(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_extract_equalities(item))
    return found


def _key_expression(
    node: Any,
    schema: DocumentStoreSchema,
    table: str,
    name_ref: Any,
    value_ref: Any,
) -> str | None:
    key_names = _key_names(schema, table)
    values = _extract_equalities(node)
    if key_names and key_names[0] not in values:
        return None
    parts = []
    for name in key_names:
        if name in values:
            parts.append(f"{name_ref(name)} = {value_ref(_to_dynamo_value(values[name]))}")
    return " AND ".join(parts) if parts else None


DYNAMODB_TYPES: dict[str, TypeSupport] = {
    "integer": TypeSupport(TypeSupportLevel.COMPATIBLE, "N", notes="All numbers are arbitrary-precision decimals."),
    "bigint": TypeSupport(TypeSupportLevel.COMPATIBLE, "N"),
    "float": TypeSupport(TypeSupportLevel.COMPATIBLE, "N"),
    "double": TypeSupport(TypeSupportLevel.COMPATIBLE, "N"),
    "decimal": TypeSupport(TypeSupportLevel.EXACT, "N", notes="Up to 38 significant digits."),
    "text": TypeSupport(TypeSupportLevel.EXACT, "S", notes="UTF-8 string; 400 KB item limit."),
    "boolean": TypeSupport(TypeSupportLevel.EXACT, "BOOL"),
    "date": TypeSupport(TypeSupportLevel.APPROXIMATE, "S", notes="ISO-8601 string by convention."),
    "time": TypeSupport(TypeSupportLevel.APPROXIMATE, "S", notes="ISO-8601 string."),
    "timestamp": TypeSupport(TypeSupportLevel.APPROXIMATE, "S", notes="ISO-8601 string; no native temporal type."),
    "datetime": TypeSupport(TypeSupportLevel.APPROXIMATE, "S"),
    "duration": TypeSupport(TypeSupportLevel.APPROXIMATE, "N", notes="Seconds by convention."),
    "uuid": TypeSupport(TypeSupportLevel.APPROXIMATE, "S", notes="No native UUID type."),
    "email": TypeSupport(TypeSupportLevel.APPROXIMATE, "S"),
    "url": TypeSupport(TypeSupportLevel.APPROXIMATE, "S"),
    "json": TypeSupport(TypeSupportLevel.EXACT, "M", notes="Native map/list nesting."),
    "binary": TypeSupport(TypeSupportLevel.EXACT, "B"),
    "geo": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native geo type; geohash strings are application-defined."),
    "list": TypeSupport(TypeSupportLevel.EXACT, "L"),
    "set": TypeSupport(TypeSupportLevel.EXACT, "SS/NS/BS", notes="Set elements must share one scalar type."),
    "map": TypeSupport(TypeSupportLevel.EXACT, "M"),
    "vector": TypeSupport(TypeSupportLevel.UNSUPPORTED, None, notes="No native vector type; binary or list encoding is application-defined."),
    "reference": TypeSupport(TypeSupportLevel.APPROXIMATE, "S", notes="Application-level reference; no foreign keys."),
}

DYNAMODB_CAPS: dict[str, Capability] = {
    CAP_TRANSACTIONS: Capability(False, "No interactive transactions; TransactWriteItems is a bounded batch, not a session."),
    CAP_TRANSACTIONAL_DDL: Capability(False),
    CAP_RETURNING: Capability(False, "ReturnValues exists on writes only."),
    CAP_ORDER_BY_NULLS: Capability(False),
    CAP_PARTIAL_INDEXES: Capability(False),
    CAP_VECTOR_TYPE: Capability(False),
    CAP_JSON: Capability(True, "Native map/list documents."),
    CAP_ARRAY_TYPE: Capability(True, "List attribute type."),
    CAP_UUID_TYPE: Capability(False),
    CAP_GEO_TYPE: Capability(False),
    CAP_DURATION_TYPE: Capability(False),
    CAP_EMAIL_TYPE: Capability(False),
    CAP_URL_TYPE: Capability(False),
    CAP_DECIMAL_UNBOUNDED: Capability(False, "38 significant digits."),
    CAP_CHECK_CONSTRAINTS: Capability(False),
    CAP_FOREIGN_KEYS: Capability(False),
    CAP_UNIQUE_CONSTRAINTS: Capability(False, "Only key uniqueness; conditional writes can enforce invariants."),
    CAP_INDEXES: Capability(True, "GSI/LSI only."),
    CAP_FULL_TEXT_SEARCH: Capability(False),
    CAP_MATERIALIZED_VIEWS: Capability(False),
    CAP_PARTITIONING: Capability(True, "Partition key and sort key."),
    CAP_REPLICATION: Capability(True, "Multi-AZ managed replication; global tables optional."),
    CAP_JOINS: Capability(False),
    CAP_GROUP_BY: Capability(False),
    CAP_HAVING: Capability(False),
    CAP_SUBQUERIES: Capability(False),
    CAP_UNION: Capability(False),
    CAP_WINDOW_FUNCTIONS: Capability(False),
    CAP_UPSERT: Capability(True, "PutItem replaces by key."),
    CAP_TTL: Capability(True, "TTL attribute in epoch seconds."),
    CAP_SECONDARY_INDEXES: Capability(True),
    CAP_GRAPH_TRAVERSAL: Capability(False),
    CAP_VECTOR_SEARCH: Capability(False),
    CAP_SEARCH_QUERY_DSL: Capability(False),
    CAP_SERVER_SIDE_SCRIPTING: Capability(False),
    CAP_BATCH_WRITES: Capability(True, "BatchWriteItem; 25 items per request."),
    CAP_STRONG_CONSISTENCY: Capability(False, "Strong read consistency per item is optional; not global."),
    CAP_MULTI_KEY_ATOMICITY: Capability(True, "TransactWriteItems up to 100 items."),
    CAP_SCHEMA_EVOLUTION: Capability(True, "Schemaless items; key schema changes require a new table."),
    CAP_UPSERT_BY_KEY: Capability(True),
    CAP_AGGREGATION_FRAMEWORK: Capability(False),
    CAP_DOCUMENT_CRUD: Capability(True),
    CAP_DOCUMENT_SCHEMA_VALIDATION: Capability(False),
    CAP_NESTED_DOCUMENTS: Capability(True),
    CAP_CHANGE_STREAMS: Capability(True, "DynamoDB Streams."),
    CAP_TRANSACTIONS_MULTI_DOC: Capability(False),
}
