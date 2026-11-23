"""Fixtures for real MongoDB and Redis integration tests.

The services are the disposable instances from ``docker-compose.test.yml`` (or
locally installed equivalents). Tests are skipped when a service cannot be
reached, so the default unit suite stays fast. Run them with::

    uv run pytest -m integration

Environment overrides:

* ``OQ_TEST_MONGODB_HOST``, ``OQ_TEST_MONGODB_PORT``, ``OQ_TEST_MONGODB_DB``,
  ``OQ_TEST_MONGODB_USER``, ``OQ_TEST_MONGODB_PASSWORD``
* ``OQ_TEST_REDIS_HOST``, ``OQ_TEST_REDIS_PORT``, ``OQ_TEST_REDIS_PASSWORD``
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from oq.runtime.contract import (
    AdapterError,
    Connection,
    ConnectionConfig,
    SecretProvider,
)


class DictSecrets(SecretProvider):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self._values = values

    def get(self, key: str) -> str | None:
        return self._values.get(key)


def _mongodb_config() -> tuple[ConnectionConfig, dict[str, str]]:
    host = os.environ.get("OQ_TEST_MONGODB_HOST", "127.0.0.1")
    port = int(os.environ.get("OQ_TEST_MONGODB_PORT", "57017"))
    database = os.environ.get("OQ_TEST_MONGODB_DB", "querly_it")
    user = os.environ.get("OQ_TEST_MONGODB_USER", "querly")
    password = os.environ.get("OQ_TEST_MONGODB_PASSWORD", "querly_test_only")
    config = ConnectionConfig(
        adapter="mongodb",
        host=host,
        port=port,
        database=database,
        user=user or None,
        options={"auth_source": "admin", "server_selection_timeout_ms": "3000"},
    )
    secrets = {"password": password} if password else {}
    return config, secrets


def _redis_config() -> tuple[ConnectionConfig, dict[str, str]]:
    host = os.environ.get("OQ_TEST_REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("OQ_TEST_REDIS_PORT", "56379"))
    password = os.environ.get("OQ_TEST_REDIS_PASSWORD", "querly_test_only")
    config = ConnectionConfig(adapter="redis", host=host, port=port, database="0")
    secrets = {"password": password} if password else {}
    return config, secrets


@pytest.fixture(scope="session")
def mongodb_adapter():
    from oq_adapters.mongodb import MongoDBAdapter

    return MongoDBAdapter()


@pytest.fixture(scope="session")
def redis_adapter():
    from oq_adapters.redis import RedisAdapter

    return RedisAdapter()


@pytest.fixture
def mongodb_connection(mongodb_adapter) -> Iterator[Connection]:
    config, secrets = _mongodb_config()
    try:
        connection = mongodb_adapter.connect(config, secrets=DictSecrets(secrets))
    except AdapterError as exc:
        # A local unauthenticated mongod is also a valid test target. Retry
        # without credentials only when the server explicitly rejected them.
        if config.user and exc.code.value == "OQ-ADAPTER-AUTH":
            unauthenticated = ConnectionConfig(
                adapter="mongodb",
                host=config.host,
                port=config.port,
                database=config.database,
                options=config.options,
            )
            try:
                connection = mongodb_adapter.connect(unauthenticated, secrets=DictSecrets({}))
            except AdapterError as retry_exc:
                pytest.skip(f"MongoDB is not reachable: {retry_exc}")
                return
        else:
            pytest.skip(f"MongoDB is not reachable: {exc}")
            return
    try:
        yield connection
    finally:
        mongodb_adapter.close(connection)


@pytest.fixture
def redis_connection(redis_adapter) -> Iterator[Connection]:
    config, secrets = _redis_config()
    try:
        connection = redis_adapter.connect(config, secrets=DictSecrets(secrets))
    except AdapterError as exc:
        pytest.skip(f"Redis is not reachable: {exc}")
        return
    try:
        yield connection
    finally:
        redis_adapter.close(connection)
