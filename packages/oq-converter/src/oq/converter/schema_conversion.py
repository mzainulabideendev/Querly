"""Schema conversion: source schema → universal IR → target schema.

Implements the schema conversion workflow defined in
docs/conversion/CONVERSION_GUIDE.md §5:

  source database
    -> adapter inspection
    -> universal schema IR (with inspection findings)
    -> compatibility analysis against target capabilities
    -> target schema IR
    -> target DDL + migration plan + compatibility report
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oq.ir import Attribute, Entity, Index, SchemaDocument, Type
from oq.runtime.contract import DDLArtifact, TypeSupportLevel

from .compatibility import (
    CompatibilityAnalyzer,
    CompatibilityStatus,
    ConversionProfile,
    ConversionStatus,
    ProfileSelector,
)


@dataclass
class TypeMappingEntry:
    """A single type mapping with support level."""

    source_type: str
    """Source engine's type name (e.g., 'integer', 'bigint', 'varchar(255)')."""
    target_type: str
    """Target engine's type name."""
    support: TypeSupportLevel
    """Level of type support."""
    notes: str | None = None
    """Human-readable notes on the mapping."""
    dimension: int | None = None
    """For vector types."""
    max_precision: int | None = None
    """For decimal types."""
    max_length: int | None = None
    """For text/binary types."""


@dataclass
class SchemaConversionResult:
    """Result of converting a source schema to a target schema."""

    source_schema: SchemaDocument
    """The original source schema."""
    target_schema: SchemaDocument
    """The generated target schema in universal IR."""
    compatibility_report: dict[str, Any]
    """Structured compatibility analysis."""
    migration_plan: list[DDLArtifact] | None
    """Generated target DDL artifacts, or None if conversion was blocked."""
    conversion_status: ConversionStatus
    """Overall conversion outcome."""
    profile: ConversionProfile
    """The conversion profile used."""
    findings: list[dict[str, Any]] | None = None
    """Individual conversion findings (per-attribute, per-index, etc.)."""
    warnings: list[str] = field(default_factory=list)
    """Conversion warnings."""


class SchemaConverter:
    """Converts schemas between database engines."""

    def __init__(
        self,
        profile: ConversionProfile = ConversionProfile.COMPATIBLE,
        source_adapter: str | None = None,
        target_adapter: str | None = None,
    ) -> None:
        self.profile = profile
        self.source_adapter = source_adapter
        self.target_adapter = target_adapter

        self._type_mappings: dict[str, TypeMappingEntry] = {}
        self._init_type_mappings()

        self._analyzer = CompatibilityAnalyzer(
            profile=self.profile,
            source_adapter=self.source_adapter,
            target_adapter=self.target_adapter,
        )

    def _init_type_mappings(self) -> None:
        """Initialize type mapping tables for common engine pairs."""

        self._type_mappings = {
            # Integer types
            "smallint": TypeMappingEntry(
                source_type="smallint",
                target_type="smallint",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Range compatible for most use cases",
            ),
            "integer": TypeMappingEntry(
                source_type="integer",
                target_type="int",
                support=TypeSupportLevel.EXACT,
                notes="Both are 32-bit signed integers",
            ),
            "bigint": TypeMappingEntry(
                source_type="bigint",
                target_type="bigint",
                support=TypeSupportLevel.EXACT,
                notes="Both are 64-bit signed integers",
            ),
            # Decimal/fixed-point
            "decimal": TypeMappingEntry(
                source_type="decimal",
                target_type="decimal",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Precision/scale may differ; report loss if narrowed",
                max_precision=38,
            ),
            "numeric": TypeMappingEntry(
                source_type="numeric",
                target_type="numeric",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Same as decimal",
                max_precision=38,
            ),
            # Text types
            "varchar": TypeMappingEntry(
                source_type="varchar",
                target_type="varchar",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Length may be lost; report if precision differs",
                max_length=65535,
            ),
            "text": TypeMappingEntry(
                source_type="text",
                target_type="text",
                support=TypeSupportLevel.EXACT,
                notes="Unlimited text in both engines",
            ),
            "character varying": TypeMappingEntry(
                source_type="character varying",
                target_type="varchar",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Same as varchar",
            ),
            # Boolean
            "boolean": TypeMappingEntry(
                source_type="boolean",
                target_type="tinyint(1)",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Stored as 0/1; no native boolean in MySQL",
            ),
            # Binary
            "bytea": TypeMappingEntry(
                source_type="bytea",
                target_type="longblob",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Binary data; length limits may differ",
            ),
            # UUID
            "uuid": TypeMappingEntry(
                source_type="uuid",
                target_type="char(36)",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Stored as char(36) with UUID format",
            ),
            # JSON
            "json": TypeMappingEntry(
                source_type="json",
                target_type="json",
                support=TypeSupportLevel.EXACT,
                notes="Both support JSON natively",
            ),
            # Date/time
            "date": TypeMappingEntry(
                source_type="date",
                target_type="date",
                support=TypeSupportLevel.EXACT,
                notes="Both support date natively",
            ),
            "timestamp": TypeMappingEntry(
                source_type="timestamp",
                target_type="datetime",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Timezone handling may differ",
            ),
            "timestamptz": TypeMappingEntry(
                source_type="timestamptz",
                target_type="datetime",
                support=TypeSupportLevel.COMPATIBLE,
                notes="Postgres timezone vs MySQL datetime",
            ),
        }

    def convert(
        self,
        source_schema: SchemaDocument,
        target_capabilities: dict[str, Any],
        target_dialect: str = "postgres",
    ) -> SchemaConversionResult:
        """Convert a source schema to a target schema.

        Args:
            source_schema: The source schema document.
            target_capabilities: The target adapter's type support map.
            target_dialect: The target engine dialect ('postgres', 'mysql', 'sqlite').

        Returns:
            SchemaConversionResult with the converted schema and compatibility report.
        """
        findings: list[dict[str, Any]] = []

        # Build target schema entities
        target_entities: list[dict[str, Any]] = []

        for entity in source_schema.entities:
            target_attrs: list[dict[str, Any]] = []

            for attr in entity.attributes:
                finding = self._convert_attribute(attr, target_capabilities, entity_findings=[])
                findings.append(finding)

                if finding["status"] not in ProfileSelector.aborts_on(self.profile):
                    target_attrs.append(self._build_target_attribute(attr, finding))

            # Convert indexes
            target_indexes: list[dict[str, Any]] = []
            for idx in entity.indexes:
                finding = self._convert_index(idx, target_capabilities, entity_findings=[])
                findings.append(finding)
                if finding["status"] not in ProfileSelector.aborts_on(self.profile):
                    target_indexes.append(self._build_target_index(idx, finding))

            # Convert unique constraints
            target_constraints: list[dict[str, Any]] = []
            for uc in entity.unique_constraints:
                finding = self._convert_constraint(uc, target_capabilities, entity_findings=[])
                findings.append(finding)
                if finding["status"] not in ProfileSelector.aborts_on(self.profile):
                    target_constraints.append(self._build_target_constraint(uc, finding))

            target_entities.append(
                {
                    "name": entity.name,
                    "attributes": target_attrs,
                    "indexes": target_indexes,
                    "unique_constraints": target_constraints,
                }
            )

        # Determine overall compatibility status
        overall_status = self._determine_overall_status(findings)

        # Generate migration plan if not blocked
        migration_plan: list[DDLArtifact] | None = None
        if overall_status != ConversionStatus.BLOCKED:
            migration_plan = self._generate_migration_plan(target_entities)

        # Build the target SchemaDocument
        target_schema_entities = []
        for entity_data in target_entities:
            target_schema_entities.append(
                Entity(
                    name=entity_data["name"],
                    attributes=[
                        Attribute(
                            name=attr["name"],
                            type=attr.get("_type"),
                            nullable=attr.get("_nullable", True),
                            primary=attr.get("_primary", False),
                            unique=attr.get("_unique", False),
                            indexed=attr.get("_indexed", False),
                            auto=attr.get("_auto", False),
                            default=attr.get("_default"),
                            check=attr.get("_check"),
                            references=attr.get("_references"),
                            span=attr.get("_span"),
                        )
                        for attr in entity_data["attributes"]
                    ],
                    primary_key=entity_data.get("primary_key", []),
                    indexes=[
                        Index(
                            name=idx["name"],
                            columns=idx["columns"],
                            unique=idx.get("unique", False),
                            kind=idx.get("kind", "btree"),
                            keys=idx.get("keys", []),
                            sparse=idx.get("sparse", False),
                            expire_after_seconds=idx.get("expire_after_seconds"),
                            partial_filter_expression=idx.get("partial_filter_expression"),
                            options=idx.get("options", {}),
                        )
                        for idx in entity_data.get("indexes", [])
                    ],
                    relationships=entity_data.get("relationships", []),
                    constraints=entity_data.get("constraints", []),
                    unique_constraints=entity_data.get("unique_constraints", []),
                    unsupported_objects=entity_data.get("unsupported_objects", []),
                )
            )

        target_schema = SchemaDocument(
            entities=target_schema_entities,
            enums=source_schema.enums,
            extensions=source_schema.extensions,
        )

        return SchemaConversionResult(
            source_schema=source_schema,
            target_schema=target_schema,
            compatibility_report={
                "overall_status": overall_status.value,
                "profile": self.profile.value,
                "findings": findings,
            },
            migration_plan=migration_plan,
            conversion_status=overall_status,
            profile=self.profile,
            findings=findings if findings else None,
        )

    def _convert_attribute(
        self,
        attr: Attribute,
        target_capabilities: dict[str, Any],
        entity_findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Convert a single attribute and return a finding."""
        kind = (
            attr.type.kind
            if hasattr(attr.type, "kind")
            else attr.type.get("kind", "text")
            if isinstance(attr.type, dict)
            else "text"
        )

        # Look up type mapping
        type_mapping = self._lookup_type_mapping(kind)

        target_type = type_mapping.target_type if type_mapping else kind
        support = type_mapping.support if type_mapping else TypeSupportLevel.UNSUPPORTED
        notes = type_mapping.notes if type_mapping else f"No type mapping for {kind}"

        finding: dict[str, Any] = {
            "construct": f"attribute.{attr.name}",
            "location": {"entity": attr.name},
            "source": {
                "name": attr.name,
                "type": kind,
                "nullable": attr.nullable,
                "primary": attr.primary,
                "unique": attr.unique,
            },
            "target": {
                "name": attr.name,
                "type": target_type,
                "support": support.value,
            },
            "status": support.value,
            "reason": notes,
        }

        if type_mapping:
            if type_mapping.dimension is not None:
                finding["target"]["dimension"] = type_mapping.dimension
            if type_mapping.max_precision is not None:
                finding["target"]["max_precision"] = type_mapping.max_precision
            if type_mapping.max_length is not None:
                finding["target"]["max_length"] = type_mapping.max_length

        entity_findings.append(finding)
        return finding

    def _lookup_type_mapping(
        self,
        kind: str,
    ) -> TypeMappingEntry | None:
        """Look up type mapping from universal type name."""
        type_key = kind.lower() if kind else ""
        return self._type_mappings.get(type_key)

    def _determine_overall_status(self, findings: list[dict[str, Any]]) -> ConversionStatus:
        """Determine the overall conversion status from findings."""
        if not findings:
            return ConversionStatus.SUCCESS

        statuses = [CompatibilityStatus(f.get("status", "unsupported")) for f in findings]
        most_severe = CompatibilityStatus.most_severe(statuses)

        aborts = ProfileSelector.aborts_on(self.profile)
        if most_severe in aborts:
            return ConversionStatus.BLOCKED

        if most_severe == CompatibilityStatus.UNSUPPORTED:
            return ConversionStatus.BLOCKED

        return ConversionStatus.SUCCESS

    def _generate_migration_plan(
        self,
        target_entities: list[dict[str, Any]],
    ) -> list[DDLArtifact]:
        """Generate target DDL artifacts from the converted schema."""
        artifacts: list[DDLArtifact] = []

        for entity_data in target_entities:
            entity_name = entity_data["name"]

            columns_sql: list[str] = []
            primary_keys: list[str] = []
            unique_constraints: list[str] = []

            for attr in entity_data.get("attributes", []):
                col_def = f'"{attr["name"]}" {attr.get("_type_sql", "TEXT")}'
                columns_sql.append(col_def)

                if attr.get("_primary", False):
                    primary_keys.append(attr["name"])
                if attr.get("_unique", False) and not attr.get("_primary", False):
                    unique_constraints.append(attr["name"])

            if primary_keys:
                columns_sql.append(f'PRIMARY KEY ("{", ".join(primary_keys)}")')

            for uc_name in unique_constraints:
                columns_sql.append(f'UNIQUE ("{uc_name}")')

            table_sql = f'CREATE TABLE "{entity_name}" (\n  ' + ",\n  ".join(columns_sql) + "\n)"
            artifacts.append(
                DDLArtifact(
                    sql=table_sql,
                    statement_type="create_table",
                    entity=entity_name,
                )
            )

            # Add indexes
            for idx in entity_data.get("indexes", []):
                idx_sql = (
                    f"CREATE {'UNIQUE ' if idx.get('unique', False) else ''}"
                    f'INDEX "{idx["name"]}" ON "{entity_name}" '
                    f"({', '.join(idx['columns'])})"
                )
                artifacts.append(
                    DDLArtifact(
                        sql=idx_sql,
                        statement_type="create_index",
                        entity=entity_name,
                        depends_on=[entity_name],
                    )
                )

        return artifacts

    def _build_target_attribute(self, attr: Attribute, finding: dict[str, Any]) -> dict[str, Any]:
        """Build a target attribute dict from a source attribute and its finding."""
        kind = (
            attr.type.kind
            if hasattr(attr.type, "kind")
            else attr.type.get("kind", "text")
            if isinstance(attr.type, dict)
            else "text"
        )

        type_mapping = self._lookup_type_mapping(kind)
        target_kind = type_mapping.target_type if type_mapping else kind

        return {
            "name": attr.name,
            "_type": Type(kind=target_kind),
            "_type_sql": target_kind,
            "_nullable": attr.nullable,
            "_primary": attr.primary,
            "_unique": attr.unique,
            "_indexed": attr.indexed,
            "_auto": attr.auto,
            "_default": attr.default,
            "_check": attr.check,
            "_references": attr.references,
        }

    def _build_target_index(self, idx: Index, finding: dict[str, Any]) -> dict[str, Any]:
        """Build a target index dict from a source index and its finding."""
        columns = list(idx.columns) if hasattr(idx, "columns") else []
        unique = idx.unique if hasattr(idx, "unique") else False
        kind = idx.kind if hasattr(idx, "kind") else "btree"

        return {
            "name": idx.name,
            "columns": columns,
            "unique": unique,
            "kind": kind,
        }

    def _convert_index(
        self,
        idx: Index,
        target_capabilities: dict[str, Any],
        entity_findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Convert a single index and return a finding."""
        columns = list(idx.columns) if hasattr(idx, "columns") else []
        unique = idx.unique if hasattr(idx, "unique") else False
        kind = idx.kind if hasattr(idx, "kind") else "btree"

        finding: dict[str, Any] = {
            "construct": f"index.{idx.name}",
            "location": {"entity": idx.name},
            "source": {
                "name": idx.name,
                "columns": columns,
                "unique": unique,
                "kind": kind,
            },
            "target": {
                "name": idx.name,
                "support": "compatible",
            },
            "status": "compatible",
            "reason": "Index preserved with same kind",
        }

        return finding

    def _convert_constraint(
        self,
        uc: Any,
        target_capabilities: dict[str, Any],
        entity_findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Convert a single unique constraint and return a finding."""
        columns = list(uc.columns) if hasattr(uc, "columns") else []
        name = getattr(uc, "name", "unnamed_constraint")

        finding: dict[str, Any] = {
            "construct": f"constraint.{name}",
            "location": {"entity": name},
            "source": {
                "name": name,
                "columns": columns,
            },
            "target": {
                "name": name,
                "support": "compatible",
            },
            "status": "compatible",
            "reason": "Unique constraint preserved",
        }

        return finding


# Convenience functions for common conversions


def convert_postgres_to_mysql(
    source_schema: SchemaDocument,
    target_capabilities: dict[str, Any],
    profile: ConversionProfile = ConversionProfile.COMPATIBLE,
) -> SchemaConversionResult:
    """Convert from PostgreSQL to MySQL schema."""
    converter = SchemaConverter(
        profile=profile,
        source_adapter="postgres",
        target_adapter="mysql",
    )
    return converter.convert(source_schema, target_capabilities, target_dialect="mysql")


def convert_mysql_to_postgres(
    source_schema: SchemaDocument,
    target_capabilities: dict[str, Any],
    profile: ConversionProfile = ConversionProfile.COMPATIBLE,
) -> SchemaConversionResult:
    """Convert from MySQL to PostgreSQL schema."""
    converter = SchemaConverter(
        profile=profile,
        source_adapter="mysql",
        target_adapter="postgres",
    )
    return converter.convert(source_schema, target_capabilities, target_dialect="postgres")


def convert_sqlite_to_postgres(
    source_schema: SchemaDocument,
    target_capabilities: dict[str, Any],
    profile: ConversionProfile = ConversionProfile.COMPATIBLE,
) -> SchemaConversionResult:
    """Convert from SQLite to PostgreSQL schema."""
    converter = SchemaConverter(
        profile=profile,
        source_adapter="sqlite",
        target_adapter="postgres",
    )
    return converter.convert(source_schema, target_capabilities, target_dialect="postgres")
