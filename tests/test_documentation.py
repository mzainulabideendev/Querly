"""Documentation and project-tracking invariants.

Phase 0 is a specification phase, so the specification set is part of the
product. These tests prevent required documents from silently disappearing and
prevent the compatibility matrix from advertising unimplemented databases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ROOT_DOCUMENTS = [
    "README.md",
    "ARCHITECTURE.md",
    "PROJECT_STATUS.md",
    "IMPLEMENTATION_ROADMAP.md",
    "CHANGELOG.md",
    "TEST_REPORT.md",
    "KNOWN_LIMITATIONS.md",
    "DATABASE_COMPATIBILITY.md",
    "DECISIONS.md",
    "CONTRIBUTING.md",
    "LICENSE",
]

REQUIRED_GUIDES = [
    "docs/language/SPECIFICATION.md",
    "docs/language/grammar.ebnf",
    "docs/types/TYPE_SYSTEM.md",
    "docs/ir/IR_SPECIFICATION.md",
    "docs/adapters/ADAPTER_GUIDE.md",
    "docs/cli/CLI_REFERENCE.md",
    "docs/security/SECURITY.md",
    "docs/conversion/CONVERSION_GUIDE.md",
    "docs/migrations/MIGRATION_GUIDE.md",
    "docs/sdk/python.md",
    "docs/sdk/javascript.md",
]

TARGET_DATABASES = [
    "PostgreSQL",
    "MySQL",
    "MariaDB",
    "SQLite",
    "SQL Server",
    "Oracle",
    "IBM Db2",
    "CockroachDB",
    "MongoDB",
    "CouchDB",
    "Amazon DocumentDB",
    "Redis",
    "DynamoDB",
    "Aerospike",
    "Cassandra",
    "ScyllaDB",
    "HBase",
    "Neo4j",
    "Amazon Neptune",
    "Elasticsearch",
    "OpenSearch",
    "ClickHouse",
    "pgvector",
    "Qdrant",
    "Milvus",
    "Weaviate",
]


@pytest.mark.parametrize("relative", REQUIRED_ROOT_DOCUMENTS + REQUIRED_GUIDES)
def test_required_document_exists_and_is_non_trivial(relative: str) -> None:
    path = ROOT / relative
    assert path.is_file(), f"missing required document: {relative}"
    assert path.stat().st_size > 200, f"document is suspiciously empty: {relative}"


def test_specification_has_required_sections() -> None:
    spec = (ROOT / "docs" / "language" / "SPECIFICATION.md").read_text(encoding="utf-8")
    for heading in [
        "# Querly Language Specification",
        "## Design principles",
        "## Lexical structure",
        "## Declarations",
        "## Query language",
        "## Type system",
        "## Transactions",
        "## Migrations",
        "## Database conversion",
        "## Diagnostics",
    ]:
        assert heading in spec, f"missing specification heading: {heading}"


def test_specification_defines_both_syntax_modes() -> None:
    spec = (ROOT / "docs" / "language" / "SPECIFICATION.md").read_text(encoding="utf-8")
    assert "English-like" in spec
    assert "Structured" in spec
    assert "create table users" in spec
    assert "table users" in spec


def test_compatibility_matrix_lists_all_target_databases() -> None:
    matrix = (ROOT / "DATABASE_COMPATIBILITY.md").read_text(encoding="utf-8")
    missing = [name for name in TARGET_DATABASES if name not in matrix]
    assert not missing, f"databases missing from compatibility matrix: {missing}"


def test_compatibility_matrix_claims_nothing_implemented_in_phase_zero() -> None:
    matrix = (ROOT / "DATABASE_COMPATIBILITY.md").read_text(encoding="utf-8")
    rows = [line for line in matrix.splitlines() if line.startswith("|") and "---" not in line]
    data_rows = [line for line in rows if "Not started" in line or "Status" in line]
    assert len(data_rows) >= len(TARGET_DATABASES)
    for line in rows:
        if "|" not in line:
            continue
        assert "Implemented" not in line
        assert "Production ready" not in line


def test_project_status_reports_phase_boundary() -> None:
    status = (ROOT / "PROJECT_STATUS.md").read_text(encoding="utf-8")
    assert "Phase 0" in status
    assert "awaiting approval" in status.lower()


def test_known_limitations_is_explicit_about_missing_components() -> None:
    limitations = (ROOT / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    for component in ["Language", "Conversion", "Security", "Platform"]:
        assert component in limitations


def test_decisions_record_phase_zero_choices() -> None:
    decisions = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    assert decisions.count("## ADR-") >= 8


def test_roadmap_contains_all_ten_phases() -> None:
    roadmap = (ROOT / "IMPLEMENTATION_ROADMAP.md").read_text(encoding="utf-8")
    for phase in range(10):
        assert f"Phase {phase}" in roadmap
    assert "Acceptance gate" in roadmap


def test_readme_states_current_phase_honestly() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Phase 0" in readme
    assert "not" in readme.lower()
    assert "MIT" in readme
