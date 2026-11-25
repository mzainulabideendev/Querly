"""Architecture invariants for the monorepo.

The Phase 0 layout is a promise: dependencies point inward at the language core,
no core package knows about a database driver or an adapter, and the documents
that define the boundaries stay present. These tests make that promise
mechanical instead of a convention nobody checks.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = ROOT / "packages"

# distribution -> distributions it may depend on. Everything else is forbidden.
ALLOWED_DEPENDENCIES: dict[str, set[str]] = {
    "oq-core": set(),
    "oq-types": set(),
    "oq-ir": {"oq-core"},
    "oq-compiler": {"oq-core", "oq-ir", "oq-types"},
    "oq-runtime": {"oq-core", "oq-ir", "oq-types", "oq-compiler"},
    "oq-converter": {"oq-core", "oq-ir", "oq-types", "oq-compiler"},
    "oq-migrations": {
        "oq-core",
        "oq-ir",
        "oq-types",
        "oq-compiler",
        "oq-runtime",
    },
    "oq-cli": {
        "oq-core",
        "oq-ir",
        "oq-types",
        "oq-compiler",
        "oq-runtime",
        "oq-converter",
        "oq-migrations",
        "oq-sdk-python",
        "oq-ai",
    },
    "oq-sdk-python": {
        "oq-core",
        "oq-ir",
        "oq-types",
        "oq-compiler",
        "oq-runtime",
        "oq-converter",
        "oq-migrations",
    },
    "oq-ai": {"oq-core", "oq-ir", "oq-types", "oq-compiler"},
}

DRIVER_MODULES = {
    "psycopg",
    "psycopg2",
    "asyncpg",
    "pymysql",
    "mysqlclient",
    "MySQLdb",
    "pymongo",
    "redis",
    "aiosqlite",
    "sqlite3",
    "oracledb",
    "cx_Oracle",
    "pyodbc",
    "pymssql",
    "cassandra",
    "elasticsearch",
    "opensearchpy",
    "qdrant_client",
    "weaviate",
    "pymilvus",
    "boto3",
    "aerospike",
    "pycouchdb",
    "neo4j",
    "gremlin_python",
}


def _project(distribution: str) -> dict[str, object]:
    with (PACKAGES_DIR / distribution / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    project = document["project"]
    assert isinstance(project, dict)
    return project


def _dependencies(distribution: str) -> list[str]:
    dependencies = _project(distribution).get("dependencies", [])
    assert isinstance(dependencies, list)
    return [str(entry).split(";", 1)[0].strip() for entry in dependencies]


def _workspace_dependencies(distribution: str) -> set[str]:
    return {name for name in _dependencies(distribution) if name.startswith("oq-")}


@pytest.mark.parametrize("distribution", sorted(ALLOWED_DEPENDENCIES))
def test_workspace_dependencies_point_inward(distribution: str) -> None:
    actual = _workspace_dependencies(distribution)
    allowed = ALLOWED_DEPENDENCIES[distribution]
    forbidden = actual - allowed
    assert not forbidden, (
        f"{distribution} depends on {sorted(forbidden)}, which violates the documented "
        f"dependency direction in ARCHITECTURE.md"
    )


def test_dependency_graph_is_acyclic() -> None:
    remaining = {name: set(_workspace_dependencies(name)) for name in ALLOWED_DEPENDENCIES}
    resolved: list[str] = []
    while remaining:
        ready = [name for name, deps in remaining.items() if not deps - set(resolved)]
        assert ready, f"dependency cycle among {sorted(remaining)}"
        for name in ready:
            resolved.append(name)
            del remaining[name]


@pytest.mark.parametrize("distribution", ["oq-core", "oq-types"])
def test_core_and_types_have_no_runtime_dependencies(distribution: str) -> None:
    dependencies = _dependencies(distribution)
    if distribution == "oq-core":
        assert dependencies == ["lark>=1.2,<2"]
    else:
        assert dependencies == []


def test_no_core_package_imports_an_adapter_or_driver() -> None:
    pattern = re.compile(
        r"^\s*(?:from|import)\s+(" + "|".join(sorted(DRIVER_MODULES | {"oq_adapters"})) + r")\b",
        re.MULTILINE,
    )
    violations: list[str] = []
    for path in PACKAGES_DIR.glob("*/src/**/*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            violations.append(f"{path.relative_to(ROOT)} imports {match.group(1)}")
    assert not violations, violations


def test_adapters_are_not_shipped_inside_the_python_packages() -> None:
    inside = [path for path in PACKAGES_DIR.glob("*") if "adapter" in path.name]
    assert inside == []


def test_every_adapter_directory_is_a_real_distribution() -> None:
    adapters = [
        path
        for path in (ROOT / "adapters").iterdir()
        if path.is_dir() and path.name.startswith("oq-adapter-")
    ]
    assert adapters, "Phase 8 must ship adapter distributions"
    for path in adapters:
        if path.name == "oq-adapter-kit":
            continue
        assert (path / "pyproject.toml").is_file(), f"{path.name} lacks pyproject.toml"
        project = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
        name = project["project"]["name"]
        module = name.removeprefix("oq-adapter-").replace("-", "_")
        package_dir = path / "src" / "oq_adapters" / module
        assert package_dir.is_dir(), f"{name} is missing {package_dir}"
        assert (package_dir / "py.typed").is_file(), f"{name} lacks py.typed"
        entry_points = project["project"].get("entry-points", {})
        assert "oq.adapters" in entry_points, f"{name} has no oq.adapters entry point"


def test_cli_and_sdk_are_leaf_packages() -> None:
    leaves = {"oq-cli", "oq-sdk-python"}
    for distribution in ALLOWED_DEPENDENCIES:
        if distribution in leaves:
            continue
        assert not (_workspace_dependencies(distribution) & leaves), (
            f"{distribution} must not depend on a leaf package"
        )


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_architecture_documents_layers_and_dependency_direction() -> None:
    text = _read("ARCHITECTURE.md")
    assert "## 3. Dependency direction" in text
    assert "oq-core" in text
    assert "PEP 420" in text
    for layer in ["AST", "Semantic model", "IR", "Physical plan"]:
        assert layer in text, f"ARCHITECTURE.md does not describe the {layer} boundary"


def test_architecture_marks_current_state_honestly() -> None:
    text = _read("ARCHITECTURE.md")
    assert "## 10. Current state" in text
    for implemented in ["lexer", "parser", "adapters"]:
        assert implemented in text


@pytest.mark.parametrize(
    "relative",
    [
        "docs/adapters/ADAPTER_GUIDE.md",
        "docs/cli/CLI_REFERENCE.md",
        "docs/conversion/CONVERSION_GUIDE.md",
        "docs/migrations/MIGRATION_GUIDE.md",
        "docs/security/SECURITY.md",
        "docs/types/TYPE_SYSTEM.md",
    ],
)
def test_phase_zero_designs_are_labeled_as_designs(relative: str) -> None:
    text = _read(relative)
    assert "Phase" in text, f"{relative} must state its phase status"
    assert "Status" in text or "status" in text


def test_adapter_contract_names_every_required_operation() -> None:
    text = _read("docs/adapters/ADAPTER_GUIDE.md")
    for operation in [
        "capabilities",
        "connect",
        "inspect_schema",
        "compile_schema",
        "compile_query",
        "execute",
        "begin",
        "commit",
        "rollback",
        "detect_version",
        "close",
    ]:
        assert operation in text, f"adapter contract omits {operation}"


def test_capability_metadata_is_specified() -> None:
    text = _read("docs/adapters/ADAPTER_GUIDE.md")
    for field in ["tested_versions", "capabilities", "supported", "notes"]:
        assert field in text, f"capability report omits {field}"
    assert "oq.adapters" in text, "adapter discovery entry point group is unspecified"


def test_conversion_statuses_are_defined_once_and_consistently() -> None:
    statuses = ["exact", "compatible", "approximate", "unsupported", "requires_decision"]
    guide = _read("docs/conversion/CONVERSION_GUIDE.md")
    decisions = _read("DECISIONS.md")
    types = _read("docs/types/TYPE_SYSTEM.md")
    for status in statuses:
        assert f"`{status}`" in guide, f"conversion guide omits {status}"
        assert status in decisions
        assert status in types
    assert "best-effort" in guide
    assert "profiles" in guide.lower()


def test_type_system_document_covers_every_canonical_type() -> None:
    canonical = [
        "integer",
        "float",
        "decimal",
        "text",
        "boolean",
        "date",
        "time",
        "timestamp",
        "duration",
        "uuid",
        "email",
        "url",
        "json",
        "binary",
        "geo",
        "list",
        "set",
        "map",
        "vector",
        "reference",
    ]
    spec = _read("docs/language/SPECIFICATION.md")
    design = _read("docs/types/TYPE_SYSTEM.md")
    for name in canonical:
        assert f"`{name}" in spec or f"`{name}<" in spec or f"`{name}(" in spec, (
            f"language specification omits type {name}"
        )
        assert name in design, f"type-system design omits {name}"
