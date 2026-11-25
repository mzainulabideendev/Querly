"""PostgreSQL adapter with a public conformance implementation.

The first release adapter is currently compiled through the shared SQL subset.
Real engine execution remains integration-gated and is not claimed as supported
by the compatibility report.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from oq.ir import (
    AdapterDiagnostic,
    Artifact,
    PlanDocument,
    QueryDocument,
    SchemaDocument,
)
from oq.runtime.contract import (
    Adapter,
    AdapterError,
    AdapterErrorCode,
    Capability,
    CapabilityReport,
    CompileOptions,
    Connection,
    ConnectionConfig,
    DDLArtifact,
    EngineVersion,
    ResultSet,
    SecretProvider,
    Transaction,
    TransactionOptions,
    TypeSupport,
    TypeSupportLevel,
)
from oq.runtime.plugin import CANONICAL_TYPE_KINDS, STANDARD_CAPABILITY_KEYS


class PostgresAdapter(Adapter):
    id = "postgres"
    display_name = "PostgreSQL"
    package_version = "0.1.0.dev0"

    def capabilities(self) -> CapabilityReport:
        types = {
            kind: TypeSupport(
                TypeSupportLevel.EXACT,
                "PostgreSQL native type",
            )
            if kind
            in (
                "integer",
                "text",
                "float",
                "decimal",
                "boolean",
                "date",
                "timestamp",
                "json",
                "binary",
            )
            else TypeSupport(
                TypeSupportLevel.UNSUPPORTED,
                None,
                notes=f"No verified implementation for canonical type {kind!r}.",
            )
            for kind in CANONICAL_TYPE_KINDS
        }
        return CapabilityReport(
            adapter=self.id,
            engine=self.display_name,
            adapter_version=self.package_version,
            tested_versions=[],
            types=types,
            capabilities={
                key: Capability(False, "Not verified in an integration release.")
                for key in STANDARD_CAPABILITY_KEYS
            },
            limitations=["No live PostgreSQL integration run has verified this release."],
            generated_at=datetime.now(UTC),
        )

    def connect(self, config: ConnectionConfig, *, secrets: SecretProvider) -> Connection:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, "PostgreSQL execution is not supported in this release."
        )

    def inspect_schema(self, connection: Connection) -> SchemaDocument:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, "PostgreSQL inspection is not supported in this release."
        )

    def compile_schema(self, schema: SchemaDocument, options: CompileOptions) -> list[DDLArtifact]:
        return []

    def compile_query(self, query: QueryDocument, schema: SchemaDocument) -> PlanDocument:
        if query.operation != "select":
            diagnostic = AdapterDiagnostic(
                code=AdapterErrorCode.UNSUPPORTED.value,
                message="PostgreSQL mutation compilation is not implemented in this release.",
                operation=query.operation,
                feature=query.operation,
            )
            return PlanDocument(
                target_adapter=self.id,
                operation=query.operation,
                artifact=Artifact(text="", dialect="postgres"),
                supported=False,
                diagnostics=[diagnostic],
            )
        return PlanDocument(
            target_adapter=self.id,
            operation="select",
            artifact=Artifact(
                text="SELECT * FROM " + query.source.entity,  # noqa: S608
                dialect="postgres",
            ),
        )

    def execute(
        self, connection: Connection, plan: PlanDocument, params: Mapping[str, object]
    ) -> ResultSet:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED, "PostgreSQL execution is not supported in this release."
        )

    def begin(self, connection: Connection, options: TransactionOptions) -> Transaction:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED,
            "PostgreSQL transactions are not supported in this release.",
        )

    def commit(self, transaction: Transaction) -> None:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED,
            "PostgreSQL transactions are not supported in this release.",
        )

    def rollback(self, transaction: Transaction) -> None:
        return

    def detect_version(self, connection: Connection) -> EngineVersion:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED,
            "PostgreSQL version detection is not supported in this release.",
        )

    def close(self, connection: Connection) -> None:
        return
