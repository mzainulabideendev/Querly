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

from .compatibility import (
    CompatibilityAnalyzer,
    CompatibilityReport,
    CompatibilityStatus,
    ConversionProfile,
    ConversionStatus,
    ProfileSelector,
)
from .migration import (
    MigrationEngine,
    MigrationPlan,
    MigrationPlanResult,
    MigrationStatus,
)
from .query_conversion import (
    QueryConversionResult,
    QueryConverter,
)
from .schema_conversion import (
    SchemaConversionResult,
    SchemaConverter,
    TypeMappingEntry,
)

__all__ = [
    "CompatibilityAnalyzer",
    "CompatibilityReport",
    "CompatibilityStatus",
    "ConversionProfile",
    "ConversionStatus",
    "MigrationEngine",
    "MigrationPlan",
    "MigrationPlanResult",
    "MigrationStatus",
    "ProfileSelector",
    "QueryConversionResult",
    "QueryConverter",
    "SchemaConversionResult",
    "SchemaConverter",
    "TypeMappingEntry",
]
