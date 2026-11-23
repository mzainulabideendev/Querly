"""Tests for the Querly conversion and migration engine (Phase 6)."""

from __future__ import annotations

import pytest
from oq.ir import Attribute, Entity, QueryDocument, SchemaDocument, TableRef, Type
from oq.runtime.contract import TypeSupport, TypeSupportLevel

from oq.converter import (
    CompatibilityAnalyzer,
    CompatibilityReport,
    CompatibilityStatus,
    ConversionProfile,
    ConversionStatus,
    MigrationEngine,
    QueryConverter,
    SchemaConverter,
)
from oq.converter.schema_conversion import (
    convert_mysql_to_postgres,
    convert_postgres_to_mysql,
    convert_sqlite_to_postgres,
)


def test_conversion_profiles() -> None:
    """Test conversion profiles and allowed statuses."""
    assert ConversionProfile.STRICT.value == "strict"
    assert ConversionProfile.COMPATIBLE.value == "compatible"
    assert ConversionProfile.BEST_EFFORT.value == "best_effort"

    # Test allowed statuses
    strict_allowed = ConversionProfile.allowed_statuses(ConversionProfile.STRICT)
    assert CompatibilityStatus.EXACT in strict_allowed
    assert CompatibilityStatus.COMPATIBLE not in strict_allowed

    compatible_allowed = ConversionProfile.allowed_statuses(ConversionProfile.COMPATIBLE)
    assert CompatibilityStatus.EXACT in compatible_allowed
    assert CompatibilityStatus.COMPATIBLE in compatible_allowed
    assert CompatibilityStatus.APPROXIMATE not in compatible_allowed


def test_schema_converter_basic() -> None:
    """Test schema converter with a simple schema."""
    schema = SchemaDocument(
        entities=[
            Entity(
                name="users",
                attributes=[
                    Attribute(name="id", type=Type(kind="integer"), primary=True),
                    Attribute(name="email", type=Type(kind="text"), nullable=False),
                ],
            )
        ]
    )

    capabilities = {
        "integer": TypeSupport(support=TypeSupportLevel.EXACT, native="INTEGER"),
        "text": TypeSupport(support=TypeSupportLevel.EXACT, native="TEXT"),
    }

    converter = SchemaConverter(
        profile=ConversionProfile.COMPATIBLE,
        source_adapter="sqlite",
        target_adapter="postgres",
    )

    result = converter.convert(schema, capabilities, target_dialect="postgres")
    assert result.conversion_status == ConversionStatus.SUCCESS
    assert result.target_schema is not None
    assert len(result.target_schema.entities) == 1
    assert result.migration_plan is not None
    assert len(result.migration_plan) > 0


def test_schema_conversion_helpers() -> None:
    """Test helper functions for PostgreSQL/MySQL/SQLite schema conversions."""
    schema = SchemaDocument(
        entities=[
            Entity(
                name="orders",
                attributes=[
                    Attribute(name="id", type=Type(kind="integer"), primary=True),
                    Attribute(name="amount", type=Type(kind="decimal")),
                    Attribute(name="created_at", type=Type(kind="timestamp")),
                ],
            )
        ]
    )

    capabilities = {
        "integer": TypeSupport(support=TypeSupportLevel.EXACT, native="INT"),
        "decimal": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="DECIMAL(10,2)"),
        "timestamp": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="DATETIME"),
    }

    # Postgres -> MySQL
    res_pg_my = convert_postgres_to_mysql(schema, capabilities, ConversionProfile.COMPATIBLE)
    assert res_pg_my.conversion_status == ConversionStatus.SUCCESS
    assert len(res_pg_my.target_schema.entities) == 1

    # MySQL -> Postgres
    res_my_pg = convert_mysql_to_postgres(schema, capabilities, ConversionProfile.COMPATIBLE)
    assert res_my_pg.conversion_status == ConversionStatus.SUCCESS

    # SQLite -> Postgres
    res_sq_pg = convert_sqlite_to_postgres(schema, capabilities, ConversionProfile.COMPATIBLE)
    assert res_sq_pg.conversion_status == ConversionStatus.SUCCESS


def test_query_converter() -> None:
    """Test QueryConverter with a QueryDocument."""
    query_ir = QueryDocument(
        operation="select",
        source=TableRef(entity="users"),
    )

    query_converter = QueryConverter(
        profile=ConversionProfile.COMPATIBLE,
        source_adapter="postgres",
        target_adapter="sqlite",
    )

    capabilities = {}
    res = query_converter.convert_query(query_ir, capabilities, target_dialect="sqlite")

    assert res.conversion_status == ConversionStatus.SUCCESS
    assert res.target_artifact is not None
    assert "users" in (res.target_sql or "")


def test_compatibility_analyzer_and_report() -> None:
    """Test CompatibilityAnalyzer and CompatibilityReport."""
    analyzer = CompatibilityAnalyzer(
        profile=ConversionProfile.COMPATIBLE,
        source_adapter="postgres",
        target_adapter="mysql",
    )

    schema = SchemaDocument(
        entities=[
            Entity(
                name="accounts",
                attributes=[
                    Attribute(name="id", type=Type(kind="integer"), primary=True),
                ],
            )
        ]
    )

    report_dict = analyzer.analyze_schema(schema, {"integer": TypeSupport(support=TypeSupportLevel.EXACT, native="INT")})
    assert report_dict["source_adapter"] == "postgres"
    assert report_dict["target_adapter"] == "mysql"
    assert "findings" in report_dict

    report = CompatibilityReport(
        source_adapter="postgres",
        target_adapter="mysql",
        profile=ConversionProfile.COMPATIBLE,
        findings=report_dict["findings"],
    )

    report_json = report.to_json()
    assert "postgres" in report_json
    assert "mysql" in report_json


def test_migration_engine() -> None:
    """Test migration engine plan generation and dry run."""
    engine = MigrationEngine(profile=ConversionProfile.COMPATIBLE)

    schema = SchemaDocument(
        entities=[
            Entity(
                name="products",
                attributes=[
                    Attribute(name="sku", type=Type(kind="text"), primary=True),
                ],
            )
        ]
    )

    converter = SchemaConverter(profile=ConversionProfile.COMPATIBLE)
    result = converter.convert(schema, {}, target_dialect="postgres")

    plan = engine.create_plan("sqlite", "postgres", result.migration_plan or [])
    assert plan.source_adapter == "sqlite"
    assert plan.target_adapter == "postgres"
    assert len(plan.steps) > 0

    dry_run_ok = engine.dry_run(plan)
    assert dry_run_ok is True

    exec_result = engine.execute_plan(plan, dry_run_first=True)
    assert exec_result.status.status == "applied"
    assert exec_result.verification_passed is True
