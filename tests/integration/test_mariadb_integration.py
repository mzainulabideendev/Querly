"""Integration tests for the MariaDB adapter against a real server."""

from __future__ import annotations

import pytest
from oq.ir import SchemaDocument, Entity, Attribute, Type
from oq.runtime.contract import ConnectionConfig, SecretProvider

pytestmark = pytest.mark.integration

def test_connect_and_version(mariadb_adapter, mariadb_connection):
    """Test basic connection and version detection."""
    assert mariadb_connection.ping() is True
    version = mariadb_adapter.detect_version(mariadb_connection)
    assert version.product == "MariaDB"
    assert version.major >= 10

def test_capability_report_is_honest(mariadb_adapter):
    """Test that capability report reflects MariaDB's actual capabilities."""
    report = mariadb_adapter.capabilities()
    assert report.engine == "MariaDB"
    assert report.tested_versions
    # Key MariaDB capabilities
    assert report.capabilities["transactions"].supported is True
    assert report.capabilities["indexes"].supported is True
    assert report.capabilities["json"].supported is True
    assert report.capabilities["foreign_keys"].supported is True
    # Known limitations
    assert report.capabilities["returning"].supported is False
    assert report.capabilities["vector_type"].supported is False

def test_compile_schema_artifacts(mariadb_adapter, mariadb_connection):
    """Test schema compilation produces valid MariaDB DDL."""
    schema = SchemaDocument(entities=[
        Entity(
            name="users",
            attributes=[
                Attribute(name="id", type=Type(kind="integer"), primary=True, auto=True),
                Attribute(name="name", type=Type(kind="text"), nullable=False),
                Attribute(name="email", type=Type(kind="text"), nullable=True),
                Attribute(name="age", type=Type(kind="integer"), nullable=True),
                Attribute(name="created_at", type=Type(kind="datetime"), nullable=True),
            ],
            indexes=[],
            unique_constraints=[],
        ),
        Entity(
            name="products",
            attributes=[
                Attribute(name="id", type=Type(kind="integer"), primary=True, auto=True),
                Attribute(name="name", type=Type(kind="text"), nullable=False),
                Attribute(name="price", type=Type(kind="decimal"), nullable=False),
                Attribute(name="in_stock", type=Type(kind="boolean"), nullable=False),
                Attribute(name="metadata", type=Type(kind="json"), nullable=True),
            ],
            indexes=[],
            unique_constraints=[],
        ),
    ], enums=[], extensions=[])

    artifacts = mariadb_adapter.compile_schema(schema, None)
    assert len(artifacts) > 0

    # Check first artifact is CREATE TABLE
    create_tables = [a for a in artifacts if a.statement_type == "create_table"]
    assert len(create_tables) >= 1

    # Users table should have INT id with AUTO_INCREMENT
    users_sql = create_tables[0].sql
    assert "CREATE TABLE `users`" in users_sql
    assert "INT" in users_sql or "BIGINT" in users_sql
    assert "AUTO_INCREMENT" in users_sql
    assert "`id`" in users_sql
    assert "`name`" in users_sql

    # Products table should have DECIMAL price
    products_sql = create_tables[1].sql if len(create_tables) > 1 else ""
    assert "CREATE TABLE `products`" in products_sql
    assert "DECIMAL" in products_sql

def test_execute_simple_query(mariadb_adapter, mariadb_connection):
    """Test executing a simple SELECT query."""
    # Create a test table and insert data
    cursor = mariadb_connection.execute("DROP TABLE IF EXISTS test_items")
    mariadb_connection.execute("CREATE TABLE test_items (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), value INT)")
    mariadb_connection.execute("INSERT INTO test_items (name, value) VALUES ('test1', 42)")
    mariadb_connection.execute("INSERT INTO test_items (name, value) VALUES ('test2', 99)")

    # Execute SELECT
    cursor = mariadb_connection.execute("SELECT * FROM test_items WHERE value > 50")
    rows = [row for row in cursor.fetchall()]
    assert len(rows) == 1
    assert rows[0][1] == "test2"  # name column
    assert rows[0][2] == 99  # value column

def test_transaction_commit(mariadb_adapter, mariadb_connection):
    """Test transaction commit."""
    mariadb_connection.execute("DROP TABLE IF EXISTS tx_test")
    mariadb_connection.execute("CREATE TABLE tx_test (id INT AUTO_INCREMENT PRIMARY KEY, value INT)")

    # Start transaction
    tx = mariadb_adapter.begin(mariadb_connection)
    assert tx.is_active is True

    mariadb_connection.execute("INSERT INTO tx_test (value) VALUES (100)")
    mariadb_adapter.commit(tx)

    # Verify data was committed
    cursor = mariadb_connection.execute("SELECT value FROM tx_test")
    rows = list(cursor.fetchall())
    assert len(rows) == 1
    assert rows[0][0] == 100

def test_transaction_rollback(mariadb_adapter, mariadb_connection):
    """Test transaction rollback."""
    mariadb_connection.execute("DROP TABLE IF EXISTS tx_test_rollback")
    mariadb_connection.execute("CREATE TABLE tx_test_rollback (id INT AUTO_INCREMENT PRIMARY KEY, value INT)")

    # Start transaction
    tx = mariadb_adapter.begin(mariadb_connection)
    assert tx.is_active is True

    mariadb_connection.execute("INSERT INTO tx_test_rollback (value) VALUES (200)")
    mariadb_adapter.rollback(tx)

    # Verify data was rolled back
    cursor = mariadb_connection.execute("SELECT value FROM tx_test_rollback")
    rows = list(cursor.fetchall())
    assert len(rows) == 0

def test_json_type_support(mariadb_adapter, mariadb_connection):
    """Test JSON type handling."""
    mariadb_connection.execute("DROP TABLE IF EXISTS json_test")
    mariadb_connection.execute("""
        CREATE TABLE json_test (
            id INT AUTO_INCREMENT PRIMARY KEY,
            data JSON
        )
    """)

    # Insert JSON data
    mariadb_connection.execute(
        "INSERT INTO json_test (data) VALUES ('{\"name\": \"test\", \"value\": 123}')"
    )

    cursor = mariadb_connection.execute("SELECT data FROM json_test")
    rows = list(cursor.fetchall())
    assert len(rows) == 1
    # MariaDB returns JSON as string
    assert "test" in str(rows[0][0]) or "name" in str(rows[0][0])

def test_schema_inspection(mariadb_adapter, mariadb_connection):
    """Test schema inspection via information_schema."""
    mariadb_connection.execute("DROP TABLE IF EXISTS inspect_test")
    mariadb_connection.execute("""
        CREATE TABLE inspect_test (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(50) NOT NULL,
            age INT,
            email VARCHAR(100) UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Inspect schema
    schema = mariadb_adapter.inspect_schema(mariadb_connection)

    # Check that we can find our test table
    table_names = [entity.name for entity in schema.entities]
    assert "inspect_test" in table_names

    # Check columns of inspect_test
    table = next(e for e in schema.entities if e.name == "inspect_test")
    attr_names = [attr.name for attr in table.attributes]
    assert "id" in attr_names
    assert "name" in attr_names
    assert "age" in attr_names
    assert "email" in attr_names
    assert "created_at" in attr_names