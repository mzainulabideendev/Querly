"""Compatibility statuses, profiles, and conversion status enums.

Implements ADR-0010: compatibility statuses are `exact`, `compatible`,
`approximate`, `unsupported`, `requires_decision`. Profiles (`strict`,
`compatible`, `best-effort`) only choose which statuses abort; they never
rewrite the reported status.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class CompatibilityStatus(str, Enum):
    """Compatibility status for a single construct.

    Ordered from least to most severe. The overall report status is the
    most severe finding status.
    """

    EXACT = "exact"
    """Target representation is provably equivalent for every value."""

    COMPATIBLE = "compatible"
    """Values preserved, documented non-semantic differences."""

    APPROXIMATE = "approximate"
    """Some values, constraints, or guarantees are lost or weakened."""

    UNSUPPORTED = "unsupported"
    """No representation exists in the target."""

    REQUIRES_DECISION = "requires_decision"
    """Safe conversion depends on application intent."""

    # Severity ordering, lowest first
    SEVERITY_ORDER: dict[str, int] = {
        "exact": 0,
        "compatible": 1,
        "approximate": 2,
        "unsupported": 3,
        "requires_decision": 4,
    }

    @classmethod
    def most_severe(cls, statuses: list[CompatibilityStatus]) -> CompatibilityStatus:
        """Return the most severe status from a list."""
        if not statuses:
            return cls.EXACT
        return max(statuses, key=lambda s: cls.SEVERITY_ORDER[s.value])

    @classmethod
    def from_level(cls, level: str) -> CompatibilityStatus:
        """Create a status from a string level."""
        return cls(level)


class ConversionProfile(str, Enum):
    """Conversion profile controlling which statuses abort.

    Profiles control refusal, never reporting. They never rewrite a reported
    status.
    """

    STRICT = "strict"
    """Allows exact only; aborts on any other status."""

    COMPATIBLE = "compatible"
    """Default. Allows exact and compatible; aborts on approximate,
    unsupported, or requires_decision."""

    BEST_EFFORT = "best_effort"
    """Allows exact, compatible, approximate, resolved requires_decision.
    Aborts on unsupported, unresolved decisions. Must print every
    approximation before generating output."""

    @classmethod
    def allowed_statuses(cls, profile: ConversionProfile) -> set[CompatibilityStatus]:
        """Return the set of statuses allowed under the given profile."""
        if profile == cls.STRICT:
            return {CompatibilityStatus.EXACT}
        if profile == cls.COMPATIBLE:
            return {CompatibilityStatus.EXACT, CompatibilityStatus.COMPATIBLE}
        if profile == cls.BEST_EFFORT:
            return {
                CompatibilityStatus.EXACT,
                CompatibilityStatus.COMPATIBLE,
                CompatibilityStatus.APPROXIMATE,
                CompatibilityStatus.REQUIRES_DECISION,
            }
        return set()

    @classmethod
    def aborts_on(cls, profile: ConversionProfile) -> set[CompatibilityStatus]:
        """Return the set of statuses that abort under the given profile."""
        all_statuses = set(CompatibilityStatus)
        allowed = cls.allowed_statuses(profile)
        return all_statuses - allowed


class ConversionStatus(str, Enum):
    """Overall conversion outcome status."""

    SUCCESS = "success"
    """Conversion completed without aborting conditions."""

    BLOCKED = "blocked"
    """Conversion blocked by an aborting status (unsupported or
    requires_decision in strict/compatible profiles)."""

    ABORTED = "aborted"
    """Conversion aborted during execution."""


class CompatibilityAnalyzer:
    """Analyzes compatibility between source and target schemas.

    Takes a source schema document and target engine capabilities,
    and produces per-construct compatibility findings.
    """

    def __init__(
        self,
        profile: ConversionProfile = ConversionProfile.COMPATIBLE,
        source_adapter: str | None = None,
        target_adapter: str | None = None,
    ) -> None:
        self.profile = profile
        self.source_adapter = source_adapter
        self.target_adapter = target_adapter

    def analyze_schema(
        self,
        source_schema: Any,
        target_capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        """Analyze compatibility of a source schema against target capabilities.

        Args:
            source_schema: The source schema document (SchemaDocument).
            target_capabilities: The target adapter's type support map.

        Returns:
            A dict containing per-construct findings and overall status.
        """
        findings: list[dict[str, Any]] = []

        for entity in source_schema.entities:
            entity_findings: list[dict[str, Any]] = []

            for attr in entity.attributes:
                finding = self._analyze_attribute(attr, target_capabilities)
                entity_findings.append(finding)

            for idx in entity.indexes:
                finding = self._analyze_index(idx, target_capabilities)
                entity_findings.append(finding)

            for uc in entity.unique_constraints:
                finding = self._analyze_constraint(uc, target_capabilities)
                entity_findings.append(finding)

            findings.extend(entity_findings)

        # Determine overall status
        from .compatibility import CompatibilityStatus

        statuses = [
            CompatibilityStatus(f.get("status", "unsupported"))
            for f in findings
        ]
        overall = CompatibilityStatus.most_severe(statuses)

        # Profile determines if we abort
        from .compatibility import ConversionProfile, ProfileSelector

        aborts = ProfileSelector.aborts_on(self.profile)
        if overall in aborts:
            overall_status = ConversionStatus.BLOCKED
        elif overall == CompatibilityStatus.UNSUPPORTED:
            overall_status = ConversionStatus.BLOCKED
        else:
            overall_status = ConversionStatus.SUCCESS

        return {
            "source_adapter": self.source_adapter,
            "target_adapter": self.target_adapter,
            "profile": self.profile.value,
            "findings": findings,
            "overall_status": overall.value,
            "overall_conversion_status": overall_status.value,
            "most_severe_status": overall.value,
        }

    def _analyze_attribute(
        self,
        attr: Any,
        target_capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        """Analyze compatibility of a single attribute."""
        # Default finding - type compatibility depends on target
        finding: dict[str, Any] = {
            "construct": f"attribute.{getattr(attr, 'name', 'unknown')}",
            "location": {"entity": getattr(attr, 'name', 'unknown')},
            "source": {
                "name": getattr(attr, 'name', 'unknown'),
                "type": getattr(attr.type, 'kind', str(getattr(attr.type, 'name', 'unknown'))),
                "nullable": getattr(attr, 'nullable', True),
            },
            "target": {
                "type": "unknown",
                "support": "unsupported",
            },
            "status": "unsupported",
            "reason": "No target mapping available",
        }

        # Try to find type mapping
        kind = getattr(attr.type, 'kind', None) or getattr(attr.type, 'name', None)
        if kind:
            kind_str = kind.lower() if hasattr(kind, 'lower') else str(kind)
            if kind_str in target_capabilities:
                tc = target_capabilities[kind_str]
                finding["target"]["type"] = tc.native or kind_str
                finding["target"]["support"] = tc.support.value
                finding["status"] = tc.support.value

        # Adjust based on profile
        if finding["status"] == "exact" and self.profile != ConversionProfile.STRICT:
            finding["status"] = "compatible"  # downgrade in non-strict profiles

        return finding

    def _analyze_index(
        self,
        idx: Any,
        target_capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        """Analyze compatibility of a single index."""
        finding: dict[str, Any] = {
            "construct": f"index.{getattr(idx, 'name', 'unknown')}",
            "location": {"entity": getattr(idx, 'name', 'unknown')},
            "source": {
                "name": getattr(idx, 'name', 'unknown'),
                "columns": getattr(idx, 'columns', []),
                "unique": getattr(idx, 'unique', False),
            },
            "target": {
                "name": None,
                "support": "unsupported",
            },
            "status": "unsupported",
            "reason": "Index analysis not yet implemented",
        }

        return finding

    def _analyze_constraint(
        self,
        uc: Any,
        target_capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        """Analyze compatibility of a single unique constraint."""
        finding: dict[str, Any] = {
            "construct": f"constraint.{getattr(uc, 'name', 'unknown')}",
            "location": {"entity": getattr(uc, 'name', 'unknown')},
            "source": {
                "name": getattr(uc, 'name', 'unknown'),
                "columns": getattr(uc, 'columns', []),
            },
            "target": {
                "name": None,
                "support": "unsupported",
            },
            "status": "unsupported",
            "reason": "Constraint analysis not yet implemented",
        }

        return finding


class ProfileSelector:
    """Selector for profile-based abort behavior.

    Profiles control refusal, never reporting. They never rewrite a reported
    status.
    """

    @staticmethod
    def allowed_statuses(profile: ConversionProfile) -> set[CompatibilityStatus]:
        """Return the set of statuses allowed under the given profile."""
        if profile == ConversionProfile.STRICT:
            return {CompatibilityStatus.EXACT}
        if profile == ConversionProfile.COMPATIBLE:
            return {CompatibilityStatus.EXACT, CompatibilityStatus.COMPATIBLE}
        if profile == ConversionProfile.BEST_EFFORT:
            return {
                CompatibilityStatus.EXACT,
                CompatibilityStatus.COMPATIBLE,
                CompatibilityStatus.APPROXIMATE,
                CompatibilityStatus.REQUIRES_DECISION,
            }
        return set()

    @staticmethod
    def aborts_on(profile: ConversionProfile) -> set[CompatibilityStatus]:
        """Return the set of statuses that abort under the given profile."""
        all_statuses = set(CompatibilityStatus)
        allowed = ProfileSelector.allowed_statuses(profile)
        return all_statuses - allowed


class CompatibilityReport:
    """A structured compatibility report generated after schema analysis.

    Contains per-construct findings and an overall compatibility status.
    """

    def __init__(
        self,
        source_adapter: str | None = None,
        target_adapter: str | None = None,
        profile: ConversionProfile = ConversionProfile.COMPATIBLE,
        findings: list[dict[str, Any]] | None = None,
        overall_status: CompatibilityStatus = CompatibilityStatus.EXACT,
    ) -> None:
        self.source_adapter = source_adapter
        self.target_adapter = target_adapter
        self.profile = profile
        self.findings = findings or []
        self.overall_status = overall_status

    def add_finding(self, finding: dict[str, Any]) -> None:
        """Add a single finding to the report."""
        self.findings.append(finding)
        # Recalculate overall status
        from .compatibility import CompatibilityStatus

        if self.findings:
            statuses = [
                CompatibilityStatus(f.get("status", "unsupported"))
                for f in self.findings
            ]
            self.overall_status = CompatibilityStatus.most_severe(statuses)
        else:
            self.overall_status = CompatibilityStatus.EXACT

    def to_dict(self) -> dict[str, Any]:
        """Convert report to dictionary."""
        return {
            "source_adapter": self.source_adapter,
            "target_adapter": self.target_adapter,
            "profile": self.profile.value,
            "findings": self.findings,
            "overall_status": self.overall_status.value,
        }

    def to_json(self) -> str:
        """Convert report to JSON string."""
        import json

        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def create_compatibility_report(
    source_adapter: str | None = None,
    target_adapter: str | None = None,
    profile: ConversionProfile = ConversionProfile.COMPATIBLE,
    findings: list[dict[str, Any]] | None = None,
) -> CompatibilityReport:
    """Factory function to create a CompatibilityReport instance.

    Args:
        source_adapter: Name of the source adapter.
        target_adapter: Name of the target adapter.
        profile: The conversion profile used.
        findings: List of per-construct findings.

    Returns:
        A CompatibilityReport instance.
    """
    overall_status = CompatibilityStatus.EXACT
    if findings:
        statuses = [
            CompatibilityStatus(f.get("status", "unsupported"))
            for f in findings
        ]
        overall_status = CompatibilityStatus.most_severe(statuses)

    return CompatibilityReport(
        source_adapter=source_adapter,
        target_adapter=target_adapter,
        profile=profile,
        findings=findings,
        overall_status=overall_status,
    )
