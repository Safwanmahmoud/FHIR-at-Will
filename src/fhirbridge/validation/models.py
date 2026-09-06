"""The validation report (AGENTS.md 10).

These are API boundary types, so they are Pydantic models with a stable field
layout. The layer list always contains an entry for every layer, including the
ones that did not run: a report where L6 is absent is indistinguishable from one
where L6 passed, and that ambiguity is exactly the kind of quiet
overclaiming principle 2.8 exists to prevent.

Issue ``message`` text may quote element values from the submitted resource, so
it is safe in a response body but must never be logged or used as a metric label
(principle 2.6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationLayer(StrEnum):
    """The eight cascade layers, in execution order."""

    STRUCTURAL = "structural"
    PROFILE = "profile"
    TERMINOLOGY = "terminology"
    INVARIANTS = "invariants"
    PLAUSIBILITY = "plausibility"
    FIDELITY = "fidelity"
    COVERAGE = "coverage"
    ROUTING = "routing"

    @property
    def number(self) -> int:
        return _LAYER_NUMBER[self]


_LAYER_NUMBER: dict[ValidationLayer, int] = {
    ValidationLayer.STRUCTURAL: 1,
    ValidationLayer.PROFILE: 2,
    ValidationLayer.TERMINOLOGY: 3,
    ValidationLayer.INVARIANTS: 4,
    ValidationLayer.PLAUSIBILITY: 5,
    ValidationLayer.FIDELITY: 6,
    ValidationLayer.COVERAGE: 7,
    ValidationLayer.ROUTING: 8,
}

CASCADE_ORDER: tuple[ValidationLayer, ...] = tuple(
    sorted(ValidationLayer, key=lambda layer: layer.number)
)


class IssueSeverity(StrEnum):
    """Aligned with FHIR ``issue-severity``."""

    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"

    @property
    def is_blocking(self) -> bool:
        return self in (IssueSeverity.FATAL, IssueSeverity.ERROR)


class LayerStatus(StrEnum):
    PASSED = "passed"
    """Ran; produced no blocking issues."""

    FAILED = "failed"
    """Ran; produced at least one blocking issue."""

    SKIPPED = "skipped"
    """Did not run. ``skipped_reason`` says why, and the report cannot claim it passed."""

    NOT_APPLICABLE = "not_applicable"
    """Cannot apply to this input, e.g. L6 fidelity with no source spans."""


class RoutingDecision(StrEnum):
    """L8 routing outcomes (AGENTS.md 10)."""

    AUTO = "auto"
    """No blocking issues. For a standalone validate call this means "conformant"."""

    NEEDS_REVIEW = "needs_review"
    REJECT = "reject"


class ValidationIssue(BaseModel):
    """One issue from one layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer: ValidationLayer
    severity: IssueSeverity
    code: str = Field(description="FHIR issue-type code, or a rule id for L5.")
    message: str = Field(description="Human-readable detail. May quote element values.")
    expression: str | None = Field(
        default=None, description="FHIRPath location, e.g. Bundle.entry[4].resource.category."
    )
    rule_id: str | None = Field(
        default=None, description="Identifier of the L5 plausibility rule that fired."
    )
    machine_code: str | None = Field(
        default=None, description="A fhirbridge error code, when the issue maps to one."
    )
    line: int | None = None
    column: int | None = None


class LayerResult(BaseModel):
    """The outcome of one layer."""

    model_config = ConfigDict(extra="forbid")

    layer: ValidationLayer
    layer_number: int
    status: LayerStatus
    blocking: bool = Field(description="Whether errors in this layer block the overall result.")
    errors: int = 0
    warnings: int = 0
    informational: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)
    duration_ms: int = 0
    skipped_reason: str | None = None
    notes: list[str] = Field(
        default_factory=list,
        description="Coverage caveats, e.g. which bindings could not be checked here.",
    )

    @model_validator(mode="after")
    def _counts_match_issues(self) -> Self:
        if self.issues:
            self.errors = sum(1 for i in self.issues if i.severity.is_blocking)
            self.warnings = sum(1 for i in self.issues if i.severity is IssueSeverity.WARNING)
            self.informational = sum(
                1 for i in self.issues if i.severity is IssueSeverity.INFORMATION
            )
        return self


class ValidationScores(BaseModel):
    """Scores. ``None`` means "not measured", never "perfect"."""

    model_config = ConfigDict(extra="forbid")

    conformance: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description=(
            "Fraction of submitted resources with zero blocking issues in L1-L4. "
            "Defined in docs/evaluation.md."
        ),
    )
    fidelity: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None, description="L6 semantic fidelity. Requires source spans and an LLM."
    )
    coverage: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None, description="L7 coverage, i.e. 1 - omission_rate."
    )
    mean_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        default=None, description="Mean calibrated fact confidence. Requires a conversion."
    )


class CriticalFlag(BaseModel):
    """A critical-domain concern that always forces review (AGENTS.md 10)."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    reason: str
    fact_id: str | None = None
    expression: str | None = None


class TerminologyVersions(BaseModel):
    model_config = ConfigDict(extra="allow")

    server: str | None = None


class ReportVersions(BaseModel):
    """The full version set required by principle 2.8."""

    model_config = ConfigDict(extra="forbid")

    code: str
    report_schema: str
    fhir: str
    typed_models: str = Field(
        description=(
            "FHIR version of the fhir.resources models used for L1 "
            "(see docs/adr/0004-r4-typed-models.md)."
        )
    )
    prompt_set: str | None = None
    binding_table: str
    target_descriptor: str
    delivery_plan_schema: str
    ig: list[str] = Field(default_factory=list)
    validator: str | None = None
    terminology: dict[str, str | None] = Field(default_factory=dict)
    model: dict[str, str] = Field(
        default_factory=dict, description="Per-stage LLM model ids. Empty for validate-only calls."
    )


class ValidationReport(BaseModel):
    """The response body of ``POST /v1/validate``."""

    model_config = ConfigDict(extra="forbid")

    status: RoutingDecision
    conformant: bool = Field(
        description="True when no blocking issue was raised by any layer that ran."
    )
    resource_type: str
    resource_count: int = Field(description="1, or the number of Bundle entries.")
    layers: list[LayerResult]
    scores: ValidationScores = Field(default_factory=ValidationScores)
    critical_flags: list[CriticalFlag] = Field(default_factory=list)
    omissions: list[dict[str, object]] = Field(default_factory=list)
    versions: ReportVersions
    nondeterminism_risk: bool = False
    nondeterminism_reasons: list[str] = Field(default_factory=list)
    conversion_id: str | None = None
    validated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profiles: list[str] = Field(default_factory=list)
    duration_ms: int = 0

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [
            issue
            for layer in self.layers
            if layer.blocking
            for issue in layer.issues
            if issue.severity.is_blocking
        ]

    def layer(self, layer: ValidationLayer) -> LayerResult | None:
        return next((item for item in self.layers if item.layer is layer), None)


__all__ = [
    "CASCADE_ORDER",
    "CriticalFlag",
    "IssueSeverity",
    "LayerResult",
    "LayerStatus",
    "ReportVersions",
    "RoutingDecision",
    "TerminologyVersions",
    "ValidationIssue",
    "ValidationLayer",
    "ValidationReport",
    "ValidationScores",
]
