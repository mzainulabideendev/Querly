"""Querly universal intermediate representation (IR).

This package provides versioned, validated Pydantic models for:
- Schema documents (entities, attributes, constraints, indexes, relationships)
- Query documents (select, insert, update, delete operations)
- Plan documents (compiled artifacts with binding plans)
- Compatibility documents (conversion findings)

All IR documents share a common envelope with format version, language
version, and generator metadata. The models are immutable (frozen) and
serialize to deterministic JSON.

Status: Phase 2 foundation for Phase 4 adapters. Schema and query documents
are available; migration/plan/compatibility models arrive in later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from oq.core.version import VERSION as OQ_VERSION  # type: ignore

__all__ = [
    "IR_FORMAT_VERSION",
    # Envelope
    "IREnvelope",
    "Generator",
    # Schema
    "SchemaDocument",
    "Entity",
    "Attribute",
    "Constraint",
    "Index",
    "Relationship",
    "Extension",
    # Query
    "QueryDocument",
    "Parameter",
    "TableRef",
    "Join",
    "Projection",
    "Expression",
    "OrderItem",
    "Returning",
    "Safety",
    # Document-store IR (Phase 5)
    "DocumentStoreSchema",
    "CollectionSchema",
    "DocumentField",
    "DocumentQuery",
    "AggregationStage",
    # Key-value IR (Phase 5)
    "KeySpaceSchema",
    "KeySpec",
    "KeyValueQuery",
    "KeyValueOperation",
    # Plan
    "PlanDocument",
    "Artifact",
    # Compatibility
    "CompatibilityDocument",
    "Finding",
    # Utilities
    "to_json",
    "from_json",
]

IR_FORMAT_VERSION: Final = "0.1"
"""Document format version written into serialized Querly IR documents."""


class Generator(BaseModel):
    """Metadata about the tool that produced a document."""

    name: str
    version: str

    model_config = ConfigDict(frozen=True, extra="allow")


class IREnvelope(BaseModel):
    """Common envelope for all IR documents."""

    format: str = Field(..., description="Document kind, e.g., 'querly.ir.schema'")
    format_version: str = Field(default=IR_FORMAT_VERSION, description="IR format version")
    language_version: str = Field(default="0.1", description="Language version")
    generator: Generator = Field(default_factory=lambda: Generator(name="oq-compiler", version=OQ_VERSION))
    document: dict[str, Any] = Field(default_factory=dict, description="Kind-specific payload")


# Type models (simplified for Phase 2 foundation)


class Type(BaseModel):
    """A canonical type descriptor."""

    kind: str
    """Canonical type kind, e.g., 'integer', 'decimal', 'text'."""

    precision: int | None = None
    scale: int | None = None
    max_length: int | None = None
    dimension: int | None = None
    element: "Type | None" = None
    key: "Type | None" = None
    value: "Type | None" = None
    entity: str | None = None
    cardinality: str | None = None
    inverse: str | None = None
    has_timezone: bool = False

    model_config = ConfigDict(frozen=True, extra="allow")


class Attribute(BaseModel):
    """One entity attribute."""

    name: str
    type: Type
    nullable: bool = False
    primary: bool = False
    unique: bool = False
    auto: bool = False
    indexed: bool = False
    default: Any | None = None
    check: Any | None = None
    references: str | None = None
    span: dict[str, int] | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class Constraint(BaseModel):
    """A constraint descriptor."""

    kind: str
    """One of: 'primary_key', 'unique', 'foreign_key', 'check', 'not_null', 'default', 'index'."""

    columns: list[str] = Field(default_factory=list)
    reference: str | None = None
    referenced_columns: list[str] = Field(default_factory=list)
    expression: Any | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class Index(BaseModel):
    """One named index."""

    name: str
    columns: list[str]
    unique: bool = False
    kind: str = "btree"

    model_config = ConfigDict(frozen=True, extra="allow")


class Relationship(BaseModel):
    """One relationship between entities."""

    name: str
    kind: str
    source_entity: str
    target_entity: str
    on_delete: str | None = None
    on_update: str | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class Extension(BaseModel):
    """Opt-in engine extension."""

    name: str
    enabled: bool = False

    model_config = ConfigDict(frozen=True, extra="allow")


class Entity(BaseModel):
    """One entity in the schema document."""

    name: str
    attributes: list[Attribute] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    unique_constraints: list[Constraint] = Field(default_factory=list)
    unsupported_objects: list[dict[str, str]] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


class SchemaDocument(BaseModel):
    """Universal schema document (querly.ir.schema)."""

    entities: list[Entity] = Field(default_factory=list)
    enums: list[dict[str, Any]] = Field(default_factory=list)
    extensions: list[Extension] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")

    @property
    def entity_map(self) -> dict[str, Entity]:
        return {e.name: e for e in self.entities}


# Query document models


class Parameter(BaseModel):
    """A named query parameter."""

    name: str
    type: Type
    description: str | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class Projection(BaseModel):
    """One projected expression with optional alias."""

    expression: dict[str, Any]
    alias: str | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class Join(BaseModel):
    """One join."""

    kind: str  # inner, left, right, full
    entity: str
    alias: str | None = None
    condition: dict[str, Any]

    model_config = ConfigDict(frozen=True, extra="allow")


class TableRef(BaseModel):
    """Table reference."""

    entity: str
    alias: str | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class Expression(BaseModel):
    """A typed expression (simplified for Phase 2)."""

    kind: str = "column"
    entity: str | None = None
    name: str | None = None
    operator: str | None = None
    left: "Expression | None" = None
    right: "Expression | None" = None
    function: str | None = None
    arguments: list["Expression"] = Field(default_factory=list)
    literal_type: dict[str, Any] | None = None
    literal_value: Any = None
    cast_type: dict[str, Any] | None = None
    whens: list[dict[str, Any]] = Field(default_factory=list)
    otherwise: dict[str, Any] | None = None
    items: list["Expression"] = Field(default_factory=list)
    negated: bool = False
    low: "Expression | None" = None
    high: "Expression | None" = None
    test: str | None = None
    field: str | None = None
    index: int | None = None
    name2: str | None = None  # For function names like "count"

    model_config = ConfigDict(frozen=True, extra="allow")


class OrderItem(BaseModel):
    """One ordering term."""

    expression: dict[str, Any]
    direction: str = "ascending"  # ascending, descending
    nulls: str = "last"  # first, last

    model_config = ConfigDict(frozen=True, extra="allow")


class Returning(BaseModel):
    """A returning clause."""

    projections: list[Projection] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


class Safety(BaseModel):
    """Safety classification for a mutation."""

    classification: str = "read"  # read, write, destructive_write, ddl
    unfiltered: bool = False
    destructive: bool = False

    model_config = ConfigDict(frozen=True, extra="allow")


class QueryDocument(BaseModel):
    """Universal query document (querly.ir.query)."""

    name: str | None = None
    operation: str = Field(..., description="select, insert, update, or delete")
    parameters: list[Parameter] = Field(default_factory=list)
    source: TableRef
    joins: list[Join] = Field(default_factory=list)
    projection: list[Projection] = Field(default_factory=list)
    filter: dict[str, Any] | None = None
    group_by: list[dict[str, Any]] = Field(default_factory=list)
    having: dict[str, Any] | None = None
    order_by: list[OrderItem] = Field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False
    returning: Returning | None = None
    safety: Safety = Field(default_factory=Safety)

    model_config = ConfigDict(frozen=True, extra="allow")

    @property
    def is_mutation(self) -> bool:
        return self.operation in ("insert", "update", "delete")


class Artifact(BaseModel):
    """Engine-specific compilation artifact."""

    kind: str = "sql"
    text: str
    dialect: str = "postgres"
    placeholders: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


class PlanDocument(BaseModel):
    """Compiled plan (querly.ir.plan)."""

    target_adapter: str
    operation: str
    artifact: Artifact
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    safety: Safety = Field(default_factory=Safety)
    limits: dict[str, int] = Field(default_factory=lambda: {"max_rows": 10000, "timeout_ms": 30000})
    result_columns: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


# Document-store IR models (Phase 5)

class DocumentField(BaseModel):
    """A field descriptor for schema-on-read inspection."""

    path: str
    """Dot-notation field path."""

    type: Type
    """Universal type inferred from values."""

    nullable: bool = True
    frequency: float = 1.0
    """Fraction of documents containing this field (0.0-1.0)."""

    model_config = ConfigDict(frozen=True, extra="allow")


class CollectionSchema(BaseModel):
    """Schema-on-read representation of a document collection."""

    name: str
    fields: list[DocumentField] = Field(default_factory=list)
    document_count: int | None = None
    indexes: list[Index] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


class DocumentStoreSchema(BaseModel):
    """Universal schema document for document stores."""

    collections: list[CollectionSchema] = Field(default_factory=list)
    extensions: list[Extension] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


class AggregationStage(BaseModel):
    """One stage in an aggregation pipeline."""

    operator: str
    """Stage operator: '$match', '$group', '$sort', '$limit', etc."""

    value: dict[str, Any]

    model_config = ConfigDict(frozen=True, extra="allow")


class DocumentQuery(BaseModel):
    """Universal query document for document stores."""

    operation: str = Field(..., description="find, insert_one, insert_many, update_one, update_many, delete_one, delete_many, aggregate, count")
    collection: str
    filter: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None
    sort: list[dict[str, Any]] = Field(default_factory=list)
    skip: int | None = None
    limit: int | None = None
    documents: list[dict[str, Any]] = Field(default_factory=list)
    updates: dict[str, Any] | None = None
    pipeline: list[AggregationStage] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    safety: Safety = Field(default_factory=Safety)

    model_config = ConfigDict(frozen=True, extra="allow")


# Key-value store IR models (Phase 5)

class KeySpec(BaseModel):
    """Key-space specification."""

    pattern: str
    """Glob-style key pattern (e.g., 'user:*')."""

    data_type: str
    """Redis type: string, hash, list, set, zset, stream."""

    ttl_seconds: int | None = None
    """Default TTL in seconds if applicable."""

    model_config = ConfigDict(frozen=True, extra="allow")


class KeySpaceSchema(BaseModel):
    """Schema document for key-value stores."""

    key_specs: list[KeySpec] = Field(default_factory=list)
    extensions: list[Extension] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


class KeyValueOperation(BaseModel):
    """Operation on a key-value store."""

    command: str
    """Redis command: SET, GET, HSET, LPUSH, SADD, ZADD, etc."""

    key: str
    args: list[Any] = Field(default_factory=list)
    ttl_seconds: int | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class KeyValueQuery(BaseModel):
    """Universal query document for key-value stores."""

    operations: list[KeyValueOperation] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    safety: Safety = Field(default_factory=Safety)

    model_config = ConfigDict(frozen=True, extra="allow")


class Finding(BaseModel):
    """A single conversion compatibility finding."""

    construct: str
    location: dict[str, str]
    status: str
    source: dict[str, Any]
    target: dict[str, Any]
    reason: str | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class CompatibilityDocument(BaseModel):
    """Compatibility report (querly.ir.compatibility)."""

    source: dict[str, Any]
    target: dict[str, Any]
    profile: str
    status: str
    findings: list[Finding] = Field(default_factory=list)
    loss_of_constraints: list[str] = Field(default_factory=list)
    requires_decision: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="allow")


# Serialization utilities


def to_json(document: IREnvelope, *, indent: int = 2) -> str:
    """Serialize an IR envelope to deterministic JSON."""
    import json

    return json.dumps(
        document.model_dump(mode="json", exclude_none=True),
        indent=indent,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"


def from_json(text: str, *, envelope: IREnvelope | None = None) -> IREnvelope:
    """Parse an IR envelope from JSON text."""
    import json

    data = json.loads(text)
    if envelope is None:
        envelope = IREnvelope.model_validate(data)
    return envelope
