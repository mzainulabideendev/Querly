"""Workspace packaging invariants.

These tests protect the monorepo from silent drift: every declared package
must be importable, every version must match the single source of truth, and
the CLI must not advertise a console script until it exists.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest
from oq.core.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = ROOT / "packages"

# distribution name -> importable module
PACKAGES: dict[str, str] = {
    "oq-core": "oq.core",
    "oq-ir": "oq.ir",
    "oq-types": "oq.types",
    "oq-compiler": "oq.compiler",
    "oq-runtime": "oq.runtime",
    "oq-converter": "oq.converter",
    "oq-migrations": "oq.migrations",
    "oq-cli": "oq.cli",
    "oq-sdk-python": "oq.sdk",
    "oq-ai": "oq.ai",
}

ADAPTERS: dict[str, str] = {
    path.name: "oq_adapters." + path.name.removeprefix("oq-adapter-").replace("-", "_")
    for path in (ROOT / "adapters").iterdir()
    if path.is_dir() and (path / "pyproject.toml").is_file()
}


def _load_pyproject(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_every_declared_package_imports() -> None:
    for module in PACKAGES.values():
        assert importlib.import_module(module) is not None


def test_package_directories_match_declared_packages() -> None:
    on_disk = {path.name for path in PACKAGES_DIR.iterdir() if path.is_dir()}
    assert on_disk == set(PACKAGES)


@pytest.mark.parametrize(("distribution", "module"), sorted(PACKAGES.items()))
def test_package_version_matches_core_version(distribution: str, module: str) -> None:
    project = _load_pyproject(PACKAGES_DIR / distribution / "pyproject.toml")["project"]
    assert isinstance(project, dict)
    assert project["name"] == distribution
    assert project["version"] == VERSION


@pytest.mark.parametrize(("distribution", "module"), sorted(PACKAGES.items()))
def test_package_ships_py_typed(distribution: str, module: str) -> None:
    package = importlib.import_module(module)
    package_dir = Path(package.__file__).parent  # type: ignore[arg-type]
    assert (package_dir / "py.typed").is_file(), f"{distribution} lacks py.typed"


def test_workspace_members_and_sources_are_consistent() -> None:
    root = _load_pyproject(ROOT / "pyproject.toml")
    uv = root["tool"]["uv"]  # type: ignore[index]
    assert uv["workspace"]["members"] == ["packages/*", "adapters/*"]
    sources = uv["sources"]
    expected = set(PACKAGES) | set(ADAPTERS)
    assert set(sources) == expected
    for distribution in expected:
        assert sources[distribution] == {"workspace": True}


def test_every_workspace_package_is_a_root_dependency() -> None:
    project = _load_pyproject(ROOT / "pyproject.toml")["project"]
    assert isinstance(project, dict)
    dependencies = set(project["dependencies"])
    assert dependencies == set(PACKAGES) | set(ADAPTERS)


def test_cli_has_console_script() -> None:
    project = _load_pyproject(PACKAGES_DIR / "oq-cli" / "pyproject.toml")["project"]
    assert isinstance(project, dict)
    assert project["scripts"] == {"oq": "oq.cli:main"}


def test_core_has_no_third_party_or_driver_dependencies() -> None:
    project = _load_pyproject(PACKAGES_DIR / "oq-core" / "pyproject.toml")["project"]
    assert isinstance(project, dict)
    assert project["dependencies"] == ["lark>=1.2,<2"]


def test_ir_and_types_do_not_depend_on_drivers() -> None:
    banned = {"psycopg", "psycopg2", "pymysql", "mysqlclient", "pymongo", "redis", "asyncpg"}
    for distribution in ("oq-core", "oq-ir", "oq-types", "oq-compiler"):
        project = _load_pyproject(PACKAGES_DIR / distribution / "pyproject.toml")["project"]
        assert isinstance(project, dict)
        assert not (set(project["dependencies"]) & banned)


def test_namespace_package_has_no_top_level_init() -> None:
    # A real `oq/__init__.py` would break multi-distribution namespace sharing.
    namespaces = {path for path in PACKAGES_DIR.glob("*/src/oq/__init__.py")}
    assert namespaces == set()


def test_source_distributions_share_the_namespace() -> None:
    for distribution, module in PACKAGES.items():
        package_dir = PACKAGES_DIR / distribution / "src" / Path(*module.split("."))
        assert package_dir.is_dir(), f"{distribution} is missing {package_dir}"
