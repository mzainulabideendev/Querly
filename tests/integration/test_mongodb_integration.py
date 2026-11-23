"""Integration tests for the MongoDB adapter against a real server.

These tests use the native MongoDB data model: collections, documents, nested
objects, arrays, and BSON values. They do not assert anything relational.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from bson import ObjectId
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from oq.ir import (
    AggregationStage,
    CollectionSchema,
    DocumentQuery,
    DocumentStoreSchema,
    Index,
)
from oq.runtime.contract import (
    AdapterError,
    AdapterErrorCode,
    CompileOptions,
    TransactionOptions,
)
from pymongo.errors import OperationFailure

pytestmark = pytest.mark.integration

COLLECTION = "querly_people"


def _clean(connection) -> None:
    connection.database.drop_collection(COLLECTION)
    connection.database.drop_collection("querly_tx")


def _execute(adapter, connection, query: DocumentQuery, params=None):
    plan = adapter.compile_query(query, DocumentStoreSchema())
    assert plan.supported, plan.diagnostics
    return adapter.execute(connection, plan, params or {})


def _insert(adapter, connection, documents):
    return _execute(
        adapter,
        connection,
        DocumentQuery(operation="insert_many", collection=COLLECTION, documents=documents),
    )


def _find(adapter, connection, **kwargs):
    query = DocumentQuery(operation="find", collection=COLLECTION, **kwargs)
    return _execute(adapter, connection, query).documents


def test_connect_and_detect_version(mongodb_adapter, mongodb_connection) -> None:
    assert mongodb_connection.ping() is True
    version = mongodb_adapter.detect_version(mongodb_connection)
    assert version.product == "MongoDB"
    assert version.major >= 4


def test_capability_report_is_honest(mongodb_adapter) -> None:
    report = mongodb_adapter.capabilities()
    assert report.engine == "MongoDB"
    assert report.tested_versions
    assert report.capabilities["aggregation_framework"].supported is True
    assert report.capabilities["nested_documents"].supported is True
    assert report.capabilities["indexes"].supported is True
    assert report.capabilities["foreign_keys"].supported is False
    for canonical in [
        "integer",
        "decimal",
        "text",
        "boolean",
        "timestamp",
        "binary",
        "list",
        "map",
        "vector",
    ]:
        assert canonical in report.types
    assert report.types["vector"].support.value == "unsupported"
    assert report.types["decimal"].support.value == "compatible"


def test_insert_find_nested_objects_and_bson_types(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    expected_id = ObjectId()
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [
            {
                "_id": expected_id,
                "name": "Ada",
                "profile": {"city": "London", "address": {"street": "1 Analytical Way"}},
                "tags": ["math", "engine"],
                "decimal": Decimal128("12345.6789"),
                "exact_time": datetime(2024, 5, 1, 12, 30, tzinfo=UTC),
                "big": Int64(2**40),
                "payload": b"\x00\x01\xfe",
                "active": True,
                "score": 10,
            },
            {
                "name": "Grace",
                "profile": {"city": "New York"},
                "tags": ["compiler"],
                "score": 20,
            },
        ],
    )

    documents = _find(mongodb_adapter, mongodb_connection, filter={"name": "Ada"})
    assert len(documents) == 1
    document = documents[0]

    # BSON values are represented with canonical Extended JSON tags, not
    # Python objects and not strings-with-lost-type.
    assert document["_id"] == {"$oid": str(expected_id)}
    assert document["decimal"] == {"$numberDecimal": "12345.6789"}
    assert document["exact_time"]["$date"]["$numberLong"].startswith("171")
    assert document["big"] == {"$numberLong": str(2**40)}
    assert document["payload"]["$binary"]["base64"] == "AAH+"
    assert document["profile"]["address"]["street"] == "1 Analytical Way"
    assert document["tags"] == ["math", "engine"]
    assert document["active"] is True


def test_filtering_nested_fields_arrays_and_operators(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [
            {"name": "a", "profile": {"city": "Paris"}, "tags": ["blue", "red"], "score": 3},
            {"name": "b", "profile": {"city": "Berlin"}, "tags": ["blue"], "score": 7},
            {"name": "c", "profile": {"city": "Paris"}, "tags": ["green"], "score": 9},
        ],
    )

    nested = _find(
        mongodb_adapter,
        mongodb_connection,
        filter={"profile.city": "Paris", "score": {"$gte": 5}},
    )
    assert [doc["name"] for doc in nested] == ["c"]

    array_match = _find(
        mongodb_adapter, mongodb_connection, filter={"tags": {"$in": ["green", "blue"]}}
    )
    assert {doc["name"] for doc in array_match} == {"a", "b", "c"}

    projection = _find(
        mongodb_adapter,
        mongodb_connection,
        filter={"name": "b"},
        projection={"_id": 0, "name": 1, "profile.city": 1},
    )
    assert projection == [{"name": "b", "profile": {"city": "Berlin"}}]


def test_sorting_and_pagination(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [{"name": str(index), "score": index} for index in range(6)],
    )

    page = _find(
        mongodb_adapter,
        mongodb_connection,
        sort=[{"field": "score", "direction": -1}],
        skip=1,
        limit=3,
    )
    assert [doc["score"] for doc in page] == [4, 3, 2]


def test_update_replace_and_delete_semantics(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [
            {"name": "a", "score": 1, "profile": {"city": "Paris"}, "tags": ["x"]},
            {"name": "b", "score": 2, "profile": {"city": "Rome"}, "tags": []},
        ],
    )

    updated = _execute(
        mongodb_adapter,
        mongodb_connection,
        DocumentQuery(
            operation="update_one",
            collection=COLLECTION,
            filter={"name": "a"},
            updates={
                "$set": {"profile.city": "Berlin"},
                "$inc": {"score": 4},
                "$push": {"tags": "y"},
            },
        ),
    )
    assert updated.affected_count == 1
    document = _find(mongodb_adapter, mongodb_connection, filter={"name": "a"})[0]
    assert document["score"] == 5
    assert document["profile"]["city"] == "Berlin"
    assert document["tags"] == ["x", "y"]

    many = _execute(
        mongodb_adapter,
        mongodb_connection,
        DocumentQuery(
            operation="update_many",
            collection=COLLECTION,
            filter={"score": {"$gte": 2}},
            updates={"$mul": {"score": 2}},
        ),
    )
    assert many.affected_count == 2
    assert sorted(doc["score"] for doc in _find(mongodb_adapter, mongodb_connection)) == [4, 10]

    upsert = _execute(
        mongodb_adapter,
        mongodb_connection,
        DocumentQuery(
            operation="update_one",
            collection=COLLECTION,
            filter={"name": "c"},
            updates={"$set": {"score": 100}},
            upsert=True,
        ),
    )
    assert upsert.documents[0]["upserted_id"] is not None

    deleted = _execute(
        mongodb_adapter,
        mongodb_connection,
        DocumentQuery(
            operation="delete_many",
            collection=COLLECTION,
            filter={"score": {"$lt": 10}},
        ),
    )
    assert deleted.affected_count == 1
    remaining = _find(mongodb_adapter, mongodb_connection)
    assert {doc["name"] for doc in remaining} == {"a", "c"}


def test_aggregation_pipeline(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [
            {"category": "books", "amount": 10, "tags": ["a", "b"]},
            {"category": "books", "amount": 15, "tags": ["a"]},
            {"category": "games", "amount": 30, "tags": ["c"]},
        ],
    )

    pipeline = [
        AggregationStage(operator="$unwind", value={"path": "$tags"}),
        AggregationStage(
            operator="$group",
            value={"_id": "$category", "total": {"$sum": "$amount"}, "count": {"$sum": 1}},
        ),
        AggregationStage(operator="$sort", value={"_id": 1}),
    ]
    result = _execute(
        mongodb_adapter,
        mongodb_connection,
        DocumentQuery(operation="aggregate", collection=COLLECTION, pipeline=pipeline),
    )
    assert result.documents == [
        {"_id": "books", "count": 3, "total": 35},
        {"_id": "games", "count": 1, "total": 30},
    ]


def test_index_generation_and_inspection(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    schema = DocumentStoreSchema(
        collections=[
            CollectionSchema(
                name=COLLECTION,
                indexes=[
                    Index(
                        name="score_desc",
                        columns=["score"],
                        kind="single",
                        keys=[{"field": "score", "direction": -1}],
                    ),
                    Index(
                        name="city_score_unique",
                        columns=["profile.city", "score"],
                        unique=True,
                        kind="compound",
                        keys=[
                            {"field": "profile.city", "direction": 1},
                            {"field": "score", "direction": 1},
                        ],
                    ),
                    Index(
                        name="tag_text",
                        columns=["tags"],
                        kind="text",
                        keys=[{"field": "tags", "direction": "text"}],
                    ),
                ],
            )
        ]
    )
    artifacts = mongodb_adapter.compile_schema(schema, options=CompileOptions())
    assert [artifact.statement_type for artifact in artifacts] == [
        "create_collection",
        "create_index",
        "create_index",
        "create_index",
    ]
    for artifact in artifacts:
        command = json.loads(artifact.sql)
        mongodb_connection.database.command(command)

    inspected = mongodb_adapter.inspect_schema(mongodb_connection)
    collection = next(item for item in inspected.collections if item.name == COLLECTION)
    indexes = {index.name: index for index in collection.indexes}
    assert indexes["score_desc"].keys == [{"field": "score", "direction": -1}]
    assert indexes["city_score_unique"].unique is True
    assert indexes["city_score_unique"].kind == "compound"
    assert [column for column in indexes["city_score_unique"].columns] == [
        "profile.city",
        "score",
    ]
    assert indexes["tag_text"].kind == "text"


def test_schema_inspection_discovers_nested_fields(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [
            {"name": "a", "profile": {"city": "Paris"}, "tags": ["x"]},
            {"name": "b", "profile": {"city": "Rome", "zip": "00100"}, "tags": []},
        ],
    )
    inspected = mongodb_adapter.inspect_schema(mongodb_connection)
    collection = next(item for item in inspected.collections if item.name == COLLECTION)
    fields = {field.path: field for field in collection.fields}
    assert collection.document_count == 2
    assert "profile.city" in fields
    assert fields["profile.city"].type.kind == "text"
    assert "profile.zip" in fields
    assert fields["profile.zip"].frequency == pytest.approx(0.5)
    assert fields["profile.zip"].nullable is True


def test_unsupported_operations_return_structured_diagnostics(
    mongodb_adapter, mongodb_connection
) -> None:
    join_plan = mongodb_adapter.compile_query(
        DocumentQuery(operation="join", collection=COLLECTION),
        DocumentStoreSchema(),
    )
    assert join_plan.supported is False
    assert join_plan.diagnostics
    assert join_plan.diagnostics[0].code == AdapterErrorCode.UNSUPPORTED.value
    assert "join" in join_plan.diagnostics[0].message

    javascript_filter = mongodb_adapter.compile_query(
        DocumentQuery(
            operation="find",
            collection=COLLECTION,
            filter={"$where": "this.score > 1"},
        ),
        DocumentStoreSchema(),
    )
    assert javascript_filter.supported is False
    assert "$where" in javascript_filter.diagnostics[0].message
    assert javascript_filter.diagnostics[0].hint

    javascript_stage = mongodb_adapter.compile_query(
        DocumentQuery(
            operation="aggregate",
            collection=COLLECTION,
            pipeline=[AggregationStage(operator="$function", value={"body": "return 1"})],
        ),
        DocumentStoreSchema(),
    )
    assert javascript_stage.supported is False

    query = DocumentQuery(operation="find", collection=COLLECTION, filter={"$where": "true"})
    unsupported = mongodb_adapter.compile_query(query, DocumentStoreSchema())
    with pytest.raises(AdapterError) as error:
        mongodb_adapter.execute(mongodb_connection, unsupported, {})
    assert error.value.code is AdapterErrorCode.UNSUPPORTED
    assert error.value.diagnostics


def test_parameter_binding_does_not_interpolate_values(mongodb_adapter, mongodb_connection) -> None:
    _clean(mongodb_connection)
    _insert(
        mongodb_adapter,
        mongodb_connection,
        [{"name": "safe", "note": "original"}],
    )
    query = DocumentQuery(
        operation="update_one",
        collection=COLLECTION,
        filter={"name": {"$param": "target"}},
        updates={"$set": {"note": {"$param": "note"}}},
    )
    plan = mongodb_adapter.compile_query(query, DocumentStoreSchema())
    hostile = {"target": "safe", "note": "$where: function(){ return true }"}
    assert "original" not in plan.artifact.text
    assert hostile["note"] not in plan.artifact.text
    _execute(mongodb_adapter, mongodb_connection, query, hostile)
    document = _find(mongodb_adapter, mongodb_connection, filter={"name": "safe"})[0]
    assert document["note"] == hostile["note"]


def test_hostile_update_operator_is_refused(mongodb_adapter) -> None:
    plan = mongodb_adapter.compile_query(
        DocumentQuery(
            operation="update_one",
            collection=COLLECTION,
            filter={},
            updates={"$function": {"body": "return true"}},
        ),
        DocumentStoreSchema(),
    )
    assert plan.supported is False
    assert plan.diagnostics[0].code == AdapterErrorCode.UNSUPPORTED.value


def test_plan_serializes_deterministically(mongodb_adapter) -> None:
    query = DocumentQuery(
        operation="find",
        collection=COLLECTION,
        filter={"score": {"$gte": 1}},
        sort=[{"field": "score", "direction": -1}],
        limit=5,
    )
    first = mongodb_adapter.compile_query(query, DocumentStoreSchema())
    second = mongodb_adapter.compile_query(query, DocumentStoreSchema())
    assert first.artifact.text == second.artifact.text
    payload = json.loads(first.model_dump_json())
    assert payload["engine_plan"]["collection"] == COLLECTION
    assert payload["supported"] is True
    assert payload["diagnostics"] == []


def test_multi_document_transaction_commit_and_rollback(
    mongodb_adapter, mongodb_connection
) -> None:
    _clean(mongodb_connection)
    collection = mongodb_connection.database["querly_tx"]
    try:
        transaction = mongodb_adapter.begin(mongodb_connection, options=TransactionOptions())
    except OperationFailure as exc:
        pytest.skip(f"Transactions require a replica set: {exc}")
        return

    try:
        collection.insert_one({"value": 1}, session=transaction.session)
        mongodb_adapter.commit(transaction)
    except OperationFailure as exc:
        pytest.skip(f"Transactions require a replica set: {exc}")
        return
    assert collection.count_documents({}) == 1

    rollback = mongodb_adapter.begin(mongodb_connection, options=TransactionOptions())
    collection.insert_one({"value": 2}, session=rollback.session)
    mongodb_adapter.rollback(rollback)
    assert collection.count_documents({}) == 1
