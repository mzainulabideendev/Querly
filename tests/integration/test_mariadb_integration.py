"""Integration tests for the MariaDB adapter against a real server."""

from __future__ import annotations

import pytest
from oq.ir import SchemaDocument, Entity, Attribute, Type
from oq.runtime.contract import ConnectionConfig, SecretProvider

pytestmark = pytest.mark.integration

class DictSecrets(SecretProvider):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self._values = values

    def get(self, key: str) -> str | None:
        return self._values.get(key)

@pytest.fixture(scope="session")
def mariadb_adapter():
    from oq_adapters.mariadb import MariaDBAdapter
    return MariaDBAdapter()

@pytest.fixture
def mariadb_connection(mariadb_adapter):
    # Simplified config for now
    config = ConnectionConfig(
        adapter="mariadb",
        host="127.0.0.1",
        port=3306,
        database="test",
        user="root",
    )
    secrets = DictSecrets({"password": "password"})
    try:
        return mariadb_adapter.connect(config, secrets=secrets)
    except Exception as e:
        pytest.skip(f"MariaDB not reachable: {e}")

def test_connect_and_version(mariadb_adapter, mariadb_connection):
    assert mariadb_connection.ping() is True
    version = mariadb_adapter.detect_version(mariadb_connection)
    assert version.product == "MariaDB"

def test_compile_schema_artifacts(mariadb_adapter):
    schema = SchemaDocument(entities=[
        Entity(name="users", attributes=[
            Attribute(name="id", type=Type(kind="integer"), primary=True, auto=True),
            Attribute(name="name", type=Type(kind="text"), nullable=False)
        ], indexes=[], unique_constraints=[])
    ], enums=[], extensions=[])

    artifacts = mariadb_adapter.compile_schema(schema, None)
    assert len(artifacts) > 0
    assert "CREATE TABLE `users`" in artifacts[0].sql
