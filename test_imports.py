"""Test that imports work correctly after Phase 6 setup."""
from oq.converter.schema_conversion import SchemaConverter, SchemaConversionResult, TypeMappingTable
from oq.converter.query_conversion import QueryConverter, QueryConversionResult
from oq.converter.migration import MigrationEngine, MigrationPlan, MigrationStatus, MigrationPlanResult
from oq.converter.compatibility import (
    CompatibilityAnalyzer,
    CompatibilityReport,
    ProfileSelector,
    CompatibilityStatus,
    ConversionProfile,
    ConversionStatus,
)

print("All imports successful!")
print(f"CompatibilityStatus: {CompatibilityStatus}")
print(f"ConversionProfile: {ConversionProfile}")