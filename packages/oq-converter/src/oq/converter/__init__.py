"""Querly conversion engine: schema conversion, query conversion, compatibility.

Phase 6 implementation.

This package contains the core conversion logic that transforms schemas and queries between different database systems. It implements:
- Schema conversion workflows
- Query conversion workflows
- Compatibility analysis and reporting
- Migration planning and execution
- Profile management (strict, compatible, best-effort)

The package follows the architecture defined in docs/conversion/CONVERSION_GUIDE.md.
"""
from __future__ import annotations

from .schema_conversion import (
    SchemaConverter,
    SchemaConversionResult,
    TypeMappingEntry,
)
from .query_conversion import (
    QueryConverter,
    QueryConversionResult,
)
from .migration import (
    MigrationEngine,
    MigrationPlan,
    MigrationStatus,
    MigrationPlanResult,
)
from .compatibility import (
    CompatibilityStatus,
    ConversionProfile,
    ConversionStatus,
    CompatibilityAnalyzer,
    CompatibilityReport,
    ProfileSelector,
)

__all__ = [
    "SchemaConverter",
    "SchemaConversionResult",
    "TypeMappingEntry",
    "QueryConverter",
    "QueryConversionResult",
    "MigrationEngine",
    "MigrationPlan",
    "MigrationStatus",
    "MigrationPlanResult",
    "CompatibilityStatus",
    "ConversionProfile",
    "ConversionStatus",
    "CompatibilityAnalyzer",
    "CompatibilityReport",
    "ProfileSelector",
]