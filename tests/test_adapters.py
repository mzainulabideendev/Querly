"""Conformance tests for every shipped adapter and the public plugin interface.

These tests never open a database connection. They protect the promises an
adapter makes before it talks to anything: a complete capability report, a
canonical type map, deterministic artifacts, structured diagnostics for
unsupported operations, and duplicate-free plugin discovery.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

from oq.ir import DocumentQuery, DocumentStoreSchema, QueryDocument, SchemaDocument, TableRef
from oq.runtime.contract import CAP_DOCUMENT_CRUD, DocumentAdapter
from oq.runtime.plugin import (
    AdapterPlugin,
    PluginError,
    discover_plugins,
    register_plugin,
    validate_adapter,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS_DIR = ROOT / "adapters"

REQUIRED_ADAPTERS = {
    "mariadb",
    "sqlserver",
    "oracle",
    "cassandra",
    "scylladb",
    "dynamodb",
    "neo4j",
    "elasticsearch",
    "opensearch",
    "clickhouse",
    "cockroachdb",
    "couchdb",
    "hbase",
    "db2",
    "neptune",
    "qdrant",
    "milvus",
    "weaviate",
    "aerospike",
    "documentdb",
}


def _specs() -> list[tuple[str, str, str]]:
    specs = []
    for path in sorted(ADAPTERS_DIR.glob("oq-adapter-*")):
        pyproject = path / "pyproject.toml"
        if not pyproject.is_file():
            continue
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        entry_points = document["project"].get("entry-points", {})
        for name, target in entry_points.get("oq.adapters", {}).items():
            module, attribute = target.split(":", 1)
            specs.append((name, module, attribute))
    return specs


SPECS = _specs()
SPEC_BY_NAME = {name: (module, attribute) for name, module, attribute in SPECS}


def _adapter(name: str) -> object:
    module_name, attribute = SPEC_BY_NAME[name]
    module = importlib.import_module(module_name)
    return getattr(module, attribute)()


@pytest.mark.parametrize("name", sorted(SPEC_BY_NAME))
def test_adapter_satisfies_the_public_contract(name: str) -> None:
    adapter = _adapter(name)
    report = validate_adapter(adapter)
    assert report.adapter == name
    assert report.engine
    assert report.adapter_version
    assert report.limitations, f"{name} must document at least one limitation"


@pytest.mark.parametrize("name", sorted(REQUIRED_ADAPTERS))
def test_every_phase_eight_adapter_is_shipped(name: str) -> None:
    assert name in SPEC_BY_NAME, f"Phase 8 adapter {name!r} has no oq.adapters entry point"


@pytest.mark.parametrize("name", sorted(SPEC_BY_NAME))
def test_capability_report_is_deterministic(name: str) -> None:
    adapter = _adapter(name)
    first = adapter.capabilities()
    second = adapter.capabilities()
    assert first.capabilities == second.capabilities
    assert first.types == second.types
    assert first.limitations == second.limitations


@pytest.mark.parametrize("name", sorted(SPEC_BY_NAME))
def test_tested_versions_are_never_invented(name: str) -> None:
    report = _adapter(name).capabilities()
    if not report.tested_versions:
        return
    for version in report.tested_versions:
        assert version.strip(), f"{name} has an empty tested version"
        assert any(char.isdigit() for char in version), (
            f"{name} claims tested version {version!r} with no version number"
        )


@pytest.mark.parametrize(
    "name",
    sorted(name for name, module, attribute in SPECS if name in REQUIRED_ADAPTERS),
)
def test_non_relational_adapters_report_unsupported_operations(name: str) -> None:
    adapter = _adapter(name)
    if not isinstance(adapter, DocumentAdapter):
        return
    plan = adapter.compile_query(
        DocumentQuery(operation="definitely_unsupported", collection="missing"),
        DocumentStoreSchema(collections=[]),
    )
    assert plan.supported is False
    assert plan.diagnostics
    assert all(item.message for item in plan.diagnostics)


@pytest.mark.parametrize(
    "name", ["mariadb", "sqlserver", "oracle", "clickhouse", "cockroachdb", "db2"]
)
def test_sql_mutations_emit_structured_diagnostics(name: str) -> None:
    adapter = _adapter(name)
    plan = adapter.compile_query(
        QueryDocument(operation="delete", source=TableRef(entity="missing")),
        SchemaDocument(entities=[]),
    )
    assert plan.supported is False
    assert plan.diagnostics
    assert any(item.code == "UNSUPPORTED" for item in plan.diagnostics)


@pytest.mark.parametrize(
    "name", sorted(name for name, module, attribute in SPECS if name in REQUIRED_ADAPTERS)
)
def test_document_adapters_advertise_document_crud(name: str) -> None:
    adapter = _adapter(name)
    if not isinstance(adapter, DocumentAdapter):
        return
    assert adapter.capabilities().capabilities[CAP_DOCUMENT_CRUD].supported is True


def test_plugin_discovery_finds_every_required_adapter_without_connecting() -> None:
    plugins = discover_plugins()
    assert REQUIRED_ADAPTERS.issubset(plugins)
    for name in REQUIRED_ADAPTERS:
        assert isinstance(plugins[name], AdapterPlugin)
        assert plugins[name].distribution


def test_duplicate_plugin_ids_are_rejected() -> None:
    plugins = discover_plugins()
    registry: dict[str, AdapterPlugin] = {}
    register_plugin(registry, plugins["mariadb"])
    with pytest.raises(PluginError):
        register_plugin(registry, plugins["mariadb"])


def test_plugin_metadata_rejects_unknown_data_models() -> None:
    plugins = discover_plugins()
    with pytest.raises(PluginError):
        AdapterPlugin(
            adapter=plugins["mariadb"].adapter,
            distribution="oq-adapter-bad",
            data_model="not-a-model",
            ir_subset=(),
        )


@pytest.mark.parametrize("name", sorted(SPEC_BY_NAME))
def test_adapter_ids_are_lowercase_and_stable(name: str) -> None:
    adapter = _adapter(name)
    assert adapter.id == name
    assert adapter.id == adapter.id.lower()
    assert adapter.display_name.strip()
