"""Integration tests for the Redis adapter against a real server.

The tests exercise Redis' native structures (strings, hashes, lists, sets,
sorted sets), key expiry, key inspection, and the safe command allowlist.
"""

from __future__ import annotations

import time

import pytest
from oq.ir import KeySpaceSchema, KeyValueOperation, KeyValueQuery
from oq.runtime.contract import (
    AdapterError,
    AdapterErrorCode,
    TransactionOptions,
)

pytestmark = pytest.mark.integration

PREFIX = "querly:it:"


def _key(name: str) -> str:
    return f"{PREFIX}{name}"


def _execute(adapter, connection, operations, params=None):
    query = KeyValueQuery(operations=operations)
    plan = adapter.compile_query(query, KeySpaceSchema())
    assert plan.supported, plan.diagnostics
    return adapter.execute(connection, plan, params or {})


def _op(command: str, key: str = "", args=None, **kwargs):
    return KeyValueOperation(command=command, key=key, args=args or [], **kwargs)


def _clean(connection) -> None:
    client = connection.client
    for key in client.scan_iter(match=f"{PREFIX}*"):
        client.delete(key)


def test_connect_and_detect_version(redis_adapter, redis_connection) -> None:
    assert redis_connection.ping() is True
    version = redis_adapter.detect_version(redis_connection)
    assert version.product == "Redis"
    assert version.major >= 5


def test_capability_report_is_honest(redis_adapter) -> None:
    report = redis_adapter.capabilities()
    assert report.engine == "Redis"
    assert report.tested_versions
    for capability in [
        "key_value_strings",
        "key_value_hashes",
        "key_value_lists",
        "key_value_sets",
        "key_value_sorted_sets",
        "key_expiration",
        "key_inspection",
        "safe_command_execution",
    ]:
        assert report.capabilities[capability].supported is True
    assert report.capabilities["document_crud"].supported is False
    assert report.types["list"].support.value == "exact"
    assert report.types["map"].native == "hash"
    assert report.types["vector"].support.value == "unsupported"
    assert any("no rollback" in note.lower() for note in report.limitations)


def test_strings_and_parameter_binding(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("string")
    hostile = "$where: function(){ return true }\r\nFLUSHALL"

    operation = _op("SET", key, args=[{"$param": "value"}])
    query = KeyValueQuery(operations=[operation])
    plan = redis_adapter.compile_query(query, KeySpaceSchema())
    assert hostile not in plan.artifact.text
    redis_adapter.execute(redis_connection, plan, {"value": hostile})

    result = _execute(redis_adapter, redis_connection, [_op("GET", key)])
    assert result.results[0]["value"] == hostile
    assert result.results[0]["command"] == "GET"

    appended = _execute(redis_adapter, redis_connection, [_op("APPEND", key, args=["!"])])
    assert appended.results[0]["value"] == len(hostile) + 1
    assert (
        _execute(redis_adapter, redis_connection, [_op("STRLEN", key)]).results[0]["value"]
        == len(hostile) + 1
    )


def test_integer_atomic_operations(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("counter")
    _execute(redis_adapter, redis_connection, [_op("SET", key, args=[10])])
    result = _execute(
        redis_adapter,
        redis_connection,
        [_op("INCRBY", key, args=[5]), _op("DECR", key), _op("GET", key)],
    )
    assert result.results[0]["value"] == 15
    assert result.results[1]["value"] == 14
    assert result.results[2]["value"] == "14"


def test_hash_operations(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("hash")
    created = _execute(
        redis_adapter,
        redis_connection,
        [_op("HSET", key, args=[{"name": "Ada", "role": "engineer"}])],
    )
    assert created.results[0]["value"] == 2

    fetched = _execute(redis_adapter, redis_connection, [_op("HGETALL", key)])
    assert fetched.results[0]["value"] == {"name": "Ada", "role": "engineer"}

    one = _execute(redis_adapter, redis_connection, [_op("HGET", key, args=["name"])])
    assert one.results[0]["value"] == "Ada"

    incremented = _execute(
        redis_adapter, redis_connection, [_op("HINCRBY", key, args=["logins", 3])]
    )
    assert incremented.results[0]["value"] == 3

    deleted = _execute(redis_adapter, redis_connection, [_op("HDEL", key, args=["role"])])
    assert deleted.results[0]["value"] == 1
    assert _execute(redis_adapter, redis_connection, [_op("HLEN", key)]).results[0]["value"] == 2


def test_list_operations(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("list")
    _execute(
        redis_adapter,
        redis_connection,
        [_op("RPUSH", key, args=["a", "b"]), _op("LPUSH", key, args=["z"])],
    )
    ranged = _execute(redis_adapter, redis_connection, [_op("LRANGE", key, args=[0, -1])])
    assert ranged.results[0]["value"] == ["z", "a", "b"]

    popped = _execute(redis_adapter, redis_connection, [_op("LPOP", key)])
    assert popped.results[0]["value"] == "z"
    assert _execute(redis_adapter, redis_connection, [_op("LLEN", key)]).results[0]["value"] == 2

    moved = _execute(
        redis_adapter,
        redis_connection,
        [_op("LMOVE", key, args=[_key("list2"), "LEFT", "RIGHT"])],
    )
    assert moved.results[0]["value"] == "a"


def test_set_operations(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    first = _key("set1")
    second = _key("set2")
    _execute(
        redis_adapter,
        redis_connection,
        [
            _op("SADD", first, args=["a", "b", "c"]),
            _op("SADD", second, args=["b", "d"]),
        ],
    )
    members = _execute(redis_adapter, redis_connection, [_op("SMEMBERS", first)])
    assert members.results[0]["value"] == ["a", "b", "c"]  # sorted deterministically

    inter = _execute(redis_adapter, redis_connection, [_op("SINTER", first, args=[second])])
    assert inter.results[0]["value"] == ["b"]
    assert _execute(redis_adapter, redis_connection, [_op("SCARD", first)]).results[0]["value"] == 3


def test_sorted_set_operations(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("zset")
    added = _execute(
        redis_adapter,
        redis_connection,
        [_op("ZADD", key, args=[{"alice": 10, "bob": 20}])],
    )
    assert added.results[0]["value"] == 2

    ordered = _execute(
        redis_adapter,
        redis_connection,
        [_op("ZRANGE", key, args=[0, -1], options={"withscores": True})],
    )
    assert ordered.results[0]["value"] == [["alice", 10.0], ["bob", 20.0]]

    score = _execute(redis_adapter, redis_connection, [_op("ZSCORE", key, args=["bob"])])
    assert score.results[0]["value"] == 20.0

    increased = _execute(redis_adapter, redis_connection, [_op("ZINCRBY", key, args=[5, "alice"])])
    assert increased.results[0]["value"] == 15.0

    ranked = _execute(redis_adapter, redis_connection, [_op("ZRANK", key, args=["bob"])])
    assert ranked.results[0]["value"] == 1
    assert _execute(redis_adapter, redis_connection, [_op("ZCARD", key)]).results[0]["value"] == 2


def test_expiration_behavior(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("expiring")
    _execute(
        redis_adapter,
        redis_connection,
        [_op("SET", key, args=["temporary"], ttl_seconds=1)],
    )

    ttl = _execute(redis_adapter, redis_connection, [_op("TTL", key)])
    assert 0 <= ttl.results[0]["value"] <= 1
    time.sleep(1.2)
    assert _execute(redis_adapter, redis_connection, [_op("GET", key)]).results[0]["value"] is None
    assert _execute(redis_adapter, redis_connection, [_op("TTL", key)]).results[0]["value"] == -2

    persistent = _key("persistent")
    _execute(redis_adapter, redis_connection, [_op("SET", persistent, args=["keep"])])
    expire = _execute(redis_adapter, redis_connection, [_op("EXPIRE", persistent, args=[60])])
    assert expire.results[0]["value"] is True
    assert (
        _execute(redis_adapter, redis_connection, [_op("TTL", persistent)]).results[0]["value"] > 0
    )
    persisted = _execute(redis_adapter, redis_connection, [_op("PERSIST", persistent)])
    assert persisted.results[0]["value"] is True
    assert (
        _execute(redis_adapter, redis_connection, [_op("TTL", persistent)]).results[0]["value"]
        == -1
    )


def test_key_inspection(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    string_key = _key("inspect:string")
    hash_key = _key("inspect:hash")
    list_key = _key("inspect:list")
    _execute(
        redis_adapter,
        redis_connection,
        [
            _op("SET", string_key, args=["hello"], ttl_seconds=60),
            _op("HSET", hash_key, args=[{"a": "1", "b": "2"}]),
            _op("RPUSH", list_key, args=["x", "y", "z"]),
        ],
    )
    schema = redis_adapter.inspect_schema(redis_connection)
    specs = {spec.pattern: spec for spec in schema.key_specs if spec.pattern.startswith(PREFIX)}
    assert specs[string_key].data_type == "string"
    assert specs[string_key].size == 5
    assert specs[string_key].ttl_seconds is not None
    assert specs[string_key].encoding
    assert specs[hash_key].data_type == "hash"
    assert specs[hash_key].size == 2
    assert specs[list_key].data_type == "list"
    assert specs[list_key].size == 3
    assert all(spec.memory_bytes is None or spec.memory_bytes > 0 for spec in specs.values())


def test_binary_safe_strings(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("binary")
    _execute(redis_adapter, redis_connection, [_op("SET", key, args=[b"\xff\x00\x01"])])
    result = _execute(redis_adapter, redis_connection, [_op("GET", key)])
    assert result.results[0]["value"] == {"$binary": {"base64": "/wAB", "subType": "00"}}


def test_key_commands_and_scan(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    first = _key("scan:a")
    second = _key("scan:b")
    _execute(
        redis_adapter,
        redis_connection,
        [_op("SET", first, args=["1"]), _op("SET", second, args=["2"])],
    )
    scanned = _execute(
        redis_adapter,
        redis_connection,
        [_op("SCAN", args=[0], options={"match": f"{PREFIX}scan:*", "count": 100})],
    )
    keys = scanned.results[0]["value"]["keys"]
    assert sorted(keys) == sorted([first, second])

    exists = _execute(redis_adapter, redis_connection, [_op("EXISTS", first, args=[second])])
    assert exists.results[0]["value"] == 2

    deleted = _execute(redis_adapter, redis_connection, [_op("DEL", first, args=[second])])
    assert deleted.results[0]["value"] == 2
    assert deleted.affected_count == 2


def test_unsafe_and_unsupported_commands_are_reported(redis_adapter) -> None:
    for operation in [
        _op("FLUSHALL"),
        _op("CONFIG", args=["GET", "maxmemory"]),
        _op("EVAL", args=["return 1", 0]),
        _op("KEYS", args=["*"]),
        _op("SELECT", args=[1]),
        _op("JOIN", args=["x"]),
    ]:
        plan = redis_adapter.compile_query(KeyValueQuery(operations=[operation]), KeySpaceSchema())
        assert plan.supported is False, operation.command
        assert plan.diagnostics
        assert plan.diagnostics[0].code == AdapterErrorCode.UNSUPPORTED.value
        assert operation.command in plan.diagnostics[0].message

    plan = redis_adapter.compile_query(
        KeyValueQuery(operations=[_op("FLUSHALL")]), KeySpaceSchema()
    )
    with pytest.raises(AdapterError) as error:
        redis_adapter.execute(
            # The connection object is not needed to prove refusal happens
            # before any command reaches Redis; a sentinel is sufficient.
            object(),  # type: ignore[arg-type]
            plan,
            {},
        )
    assert error.value.code is AdapterErrorCode.UNSUPPORTED
    assert error.value.diagnostics


def test_no_schema_ddl_is_claimed(redis_adapter) -> None:
    schema = KeySpaceSchema(key_specs=[])
    assert redis_adapter.compile_schema(schema, options=None) == []  # type: ignore[arg-type]
    diagnostics = redis_adapter.schema_diagnostics(schema)
    assert diagnostics[0].code == AdapterErrorCode.UNSUPPORTED.value
    assert "no schema DDL" in diagnostics[0].message


def test_multi_exec_commit_and_discard(redis_adapter, redis_connection) -> None:
    _clean(redis_connection)
    key = _key("transaction")
    transaction = redis_adapter.begin(redis_connection, TransactionOptions())
    transaction.pipeline.set(key, "1")
    transaction.pipeline.incr(key)
    redis_adapter.commit(transaction)
    assert _execute(redis_adapter, redis_connection, [_op("GET", key)]).results[0]["value"] == "2"

    discard_key = _key("discarded")
    discarded = redis_adapter.begin(redis_connection, TransactionOptions())
    discarded.pipeline.set(discard_key, "never")
    redis_adapter.rollback(discarded)
    assert (
        _execute(redis_adapter, redis_connection, [_op("EXISTS", discard_key)]).results[0]["value"]
        == 0
    )
