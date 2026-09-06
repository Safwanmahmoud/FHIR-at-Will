"""The validation cascade orchestrator (AGENTS.md 10).

Invariants this module is responsible for:

* **A layer that did not run never counts as a pass.** It appears in the report
  with ``status=skipped`` and a reason, and it prevents the ``auto`` routing
  decision. A report that looks clean because half the cascade was silently
  skipped is worse than a report that fails.
* **Dependency outages propagate.** ``ValidatorUnavailableError`` and
  ``TerminologyUnavailableError`` are not caught here. They surface as ``503``
  (principle 2.4).
* **Layers 6 and 7 are honest about needing a source document.** For a
  standalone ``POST /v1/validate`` there are no spans to check fidelity or
  omissions against, so they are reported ``not_applicable``, not ``passed``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from fhirbridge.config import Settings
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.observability.metrics import (
    VALIDATION_ISSUES,
    VALIDATION_LAYER_DURATION,
    VALIDATION_LAYER_SKIPPED,
    VALIDATION_RUNS,
)
from fhirbridge.terminology.interface import TerminologyClient
from fhirbridge.validation import invariants as l4
from fhirbridge.validation import plausibility as l5
from fhirbridge.validation import profile as l2
from fhirbridge.validation import structural as l1
from fhirbridge.validation import terminology as l3
from fhirbridge.validation.models import (
    CASCADE_ORDER,
    CriticalFlag,
    IssueSeverity,
    LayerResult,
    LayerStatus,
    ReportVersions,
    RoutingDecision,
    ValidationIssue,
    ValidationLayer,
    ValidationReport,
    ValidationScores,
)
from fhirbridge.version import (
    BINDING_TABLE_VERSION,
    CODE_VERSION,
    DELIVERY_PLAN_SCHEMA_VERSION,
    TARGET_DESCRIPTOR_VERSION,
    TYPED_MODEL_FHIR_VERSION,
    VALIDATION_REPORT_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

_ENTRY_INDEX: Final = re.compile(r"^Bundle\.entry\[(\d+)\]")

CRITICAL_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    "allergy": ("AllergyIntolerance",),
    "medication_dose": ("dosage", "doseQuantity", "doseRange", "rateQuantity"),
    "laterality": ("bodySite", "laterality", "site"),
}
"""Critical domains (AGENTS.md 10). ``negation`` and ``experiencer`` need facts,
so they are only assessed from M3 onward."""

_LAYERS_REQUIRING_LLM: Final[frozenset[ValidationLayer]] = frozenset(
    {ValidationLayer.FIDELITY, ValidationLayer.COVERAGE}
)


@dataclass(slots=True)
class ValidationSpec:
    """What the caller asked for."""

    profiles: tuple[str, ...] = ()
    layers: frozenset[ValidationLayer] | None = None
    """``None`` means every layer that can run. An explicit set opts out."""

    severity_overrides: Mapping[str, str] = field(default_factory=dict)
    max_terminology_checks: int = l3.DEFAULT_MAX_CHECKS
    conversion_id: str | None = None
    ig_packages: tuple[str, ...] = ()

    def wants(self, layer: ValidationLayer) -> bool:
        return self.layers is None or layer in self.layers


class ValidationCascade:
    """Runs L1 through L8 over a single FHIR payload."""

    def __init__(
        self,
        *,
        validator: ValidatorClient,
        terminology: TerminologyClient,
        settings: Settings,
        terminology_versions: Mapping[str, str | None] | None = None,
    ) -> None:
        self._validator = validator
        self._terminology = terminology
        self._settings = settings
        self._terminology_versions = dict(terminology_versions or {})

    async def run(self, payload: object, spec: ValidationSpec | None = None) -> ValidationReport:
        """Validate ``payload`` and return the report."""
        request = spec or ValidationSpec()
        started = time.perf_counter()
        layers: dict[ValidationLayer, LayerResult] = {}

        # --- L1 structural (always runs; nothing downstream works without it)
        structural = l1.validate_structure(payload)
        layers[ValidationLayer.STRUCTURAL] = structural.result
        _observe(structural.result)

        parseable = isinstance(payload, dict) and structural.resource_type is not None
        fatal = any(issue.severity is IssueSeverity.FATAL for issue in structural.result.issues)
        resource_type = structural.resource_type or "Unknown"
        body: dict[str, Any] = payload if isinstance(payload, dict) else {}

        if fatal or not parseable:
            reason = (
                "L1 could not parse the payload as a FHIR R4 resource, so no further "
                "layer can be applied."
            )
            for layer in CASCADE_ORDER:
                if layer is ValidationLayer.STRUCTURAL or layer is ValidationLayer.ROUTING:
                    continue
                layers[layer] = _skip(layer, reason)
            return self._assemble(
                layers=layers,
                resource_type=resource_type,
                resource_count=structural.resource_count,
                request=request,
                started=started,
            )

        # --- L2 profile
        if request.wants(ValidationLayer.PROFILE):
            layers[ValidationLayer.PROFILE] = await l2.validate_profile(
                body,
                client=self._validator,
                profiles=request.profiles,
                resource_type=resource_type,
            )
        else:
            layers[ValidationLayer.PROFILE] = _skip(
                ValidationLayer.PROFILE, "not requested by the caller"
            )
        _observe(layers[ValidationLayer.PROFILE])

        # --- L3 terminology
        if not request.wants(ValidationLayer.TERMINOLOGY):
            layers[ValidationLayer.TERMINOLOGY] = _skip(
                ValidationLayer.TERMINOLOGY, "not requested by the caller"
            )
        elif structural.typed is None:
            layers[ValidationLayer.TERMINOLOGY] = _skip(
                ValidationLayer.TERMINOLOGY,
                "L1 produced no typed model for this resource type, so coded elements "
                "could not be located reliably. Terminology conformance rests on L2.",
            )
        else:
            layers[ValidationLayer.TERMINOLOGY] = await l3.validate_terminology(
                structural.typed,
                client=self._terminology,
                max_checks=request.max_terminology_checks,
            )
        _observe(layers[ValidationLayer.TERMINOLOGY])

        # --- L4 invariants
        if request.wants(ValidationLayer.INVARIANTS):
            layers[ValidationLayer.INVARIANTS] = await l4.validate_invariants(
                body, client=self._validator, resource_type=resource_type
            )
        else:
            layers[ValidationLayer.INVARIANTS] = _skip(
                ValidationLayer.INVARIANTS, "not requested by the caller"
            )
        _observe(layers[ValidationLayer.INVARIANTS])

        # --- L5 plausibility
        if request.wants(ValidationLayer.PLAUSIBILITY):
            layers[ValidationLayer.PLAUSIBILITY] = l5.validate_plausibility(
                body,
                resource_type=resource_type,
                severity_overrides=request.severity_overrides,
            )
        else:
            layers[ValidationLayer.PLAUSIBILITY] = _skip(
                ValidationLayer.PLAUSIBILITY, "not requested by the caller"
            )
        _observe(layers[ValidationLayer.PLAUSIBILITY])

        # --- L6/L7 need source spans and a second model; not available here.
        for layer in _LAYERS_REQUIRING_LLM:
            layers[layer] = LayerResult(
                layer=layer,
                layer_number=layer.number,
                status=LayerStatus.NOT_APPLICABLE,
                blocking=False,
                skipped_reason=(
                    "This layer compares a bundle against the source document it came "
                    "from. A standalone validation has no source spans, so there is "
                    "nothing to compare. Use POST /v1/verify with a document to run it."
                ),
            )

        return self._assemble(
            layers=layers,
            resource_type=resource_type,
            resource_count=structural.resource_count,
            request=request,
            started=started,
        )

    # --- Assembly ---------------------------------------------------------

    def _assemble(
        self,
        *,
        layers: Mapping[ValidationLayer, LayerResult],
        resource_type: str,
        resource_count: int,
        request: ValidationSpec,
        started: float,
    ) -> ValidationReport:
        ordered = [layers[layer] for layer in CASCADE_ORDER if layer in layers]
        blocking_issues = [
            issue
            for result in ordered
            if result.blocking
            for issue in result.issues
            if issue.severity.is_blocking
        ]
        warnings = [
            issue
            for result in ordered
            for issue in result.issues
            if issue.severity is IssueSeverity.WARNING
        ]
        skipped_blocking = [
            result for result in ordered if result.blocking and result.status is LayerStatus.SKIPPED
        ]
        critical_flags = _critical_flags(ordered)

        conformant = not blocking_issues
        if blocking_issues:
            decision = RoutingDecision.REJECT
        elif skipped_blocking or warnings or critical_flags:
            decision = RoutingDecision.NEEDS_REVIEW
        else:
            decision = RoutingDecision.AUTO

        routing = LayerResult(
            layer=ValidationLayer.ROUTING,
            layer_number=ValidationLayer.ROUTING.number,
            status=LayerStatus.PASSED,
            blocking=False,
            issues=[
                ValidationIssue(
                    layer=ValidationLayer.ROUTING,
                    severity=IssueSeverity.INFORMATION,
                    code="informational",
                    message=_routing_rationale(
                        decision, blocking_issues, warnings, skipped_blocking, critical_flags
                    ),
                )
            ],
            notes=[
                "Routing for a standalone validation is driven by conformance only. "
                "Calibrated fact confidence is added from M3."
            ],
        )
        report_layers = [*ordered, routing]

        VALIDATION_RUNS.labels(outcome=str(decision)).inc()

        return ValidationReport(
            status=decision,
            conformant=conformant,
            resource_type=resource_type,
            resource_count=resource_count,
            layers=report_layers,
            scores=ValidationScores(
                conformance=_conformance_score(ordered, resource_count, resource_type)
            ),
            critical_flags=critical_flags,
            versions=self._versions(request),
            nondeterminism_risk=False,
            conversion_id=request.conversion_id,
            profiles=list(request.profiles),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _versions(self, request: ValidationSpec) -> ReportVersions:
        return ReportVersions(
            code=CODE_VERSION,
            report_schema=VALIDATION_REPORT_SCHEMA_VERSION,
            fhir=self._settings.default_fhir_version,
            typed_models=TYPED_MODEL_FHIR_VERSION,
            binding_table=BINDING_TABLE_VERSION,
            target_descriptor=TARGET_DESCRIPTOR_VERSION,
            delivery_plan_schema=DELIVERY_PLAN_SCHEMA_VERSION,
            ig=list(request.ig_packages or self._settings.ig_coordinates),
            validator=self._settings.validator_version,
            terminology=dict(self._terminology_versions),
        )


def _skip(layer: ValidationLayer, reason: str) -> LayerResult:
    VALIDATION_LAYER_SKIPPED.labels(layer=str(layer), reason=_reason_label(reason)).inc()
    return LayerResult(
        layer=layer,
        layer_number=layer.number,
        status=LayerStatus.SKIPPED,
        blocking=layer not in _LAYERS_REQUIRING_LLM and layer is not ValidationLayer.ROUTING,
        skipped_reason=reason,
    )


def _reason_label(reason: str) -> str:
    """Collapse a prose reason into a bounded metric label."""
    lowered = reason.lower()
    if "not requested" in lowered:
        return "not_requested"
    if "could not parse" in lowered:
        return "unparseable_input"
    if "typed model" in lowered:
        return "no_typed_model"
    return "other"


def _observe(result: LayerResult) -> None:
    VALIDATION_LAYER_DURATION.labels(layer=str(result.layer)).observe(result.duration_ms / 1000)
    for issue in result.issues:
        VALIDATION_ISSUES.labels(layer=str(result.layer), severity=str(issue.severity)).inc()


def _conformance_score(
    results: Sequence[LayerResult], resource_count: int, resource_type: str
) -> float | None:
    """Fraction of submitted resources with no blocking issue in L1-L4.

    Defined in docs/evaluation.md. Returns None when there is nothing to score.
    """
    conformance_layers = {
        ValidationLayer.STRUCTURAL,
        ValidationLayer.PROFILE,
        ValidationLayer.TERMINOLOGY,
        ValidationLayer.INVARIANTS,
    }
    if resource_count <= 0:
        return None

    if resource_type != "Bundle":
        blocking = any(
            issue.severity.is_blocking
            for result in results
            if result.layer in conformance_layers
            for issue in result.issues
        )
        return 0.0 if blocking else 1.0

    failed_entries: set[int] = set()
    bundle_level_failure = False
    for result in results:
        if result.layer not in conformance_layers:
            continue
        for issue in result.issues:
            if not issue.severity.is_blocking:
                continue
            match = _ENTRY_INDEX.match(issue.expression or "")
            if match:
                failed_entries.add(int(match.group(1)))
            else:
                bundle_level_failure = True

    if bundle_level_failure:
        return 0.0
    return max(0.0, (resource_count - len(failed_entries)) / resource_count)


def _critical_flags(results: Sequence[LayerResult]) -> list[CriticalFlag]:
    """Flag issues that touch a critical domain (AGENTS.md 10).

    Critical domains are reported separately and are never averaged into the
    overall conformance score.
    """
    flags: list[CriticalFlag] = []
    seen: set[tuple[str, str | None]] = set()
    for result in results:
        for issue in result.issues:
            if issue.severity is IssueSeverity.INFORMATION:
                continue
            expression = issue.expression or ""
            for domain, markers in CRITICAL_DOMAINS.items():
                if not any(marker in expression for marker in markers):
                    continue
                key = (domain, issue.expression)
                if key in seen:
                    continue
                seen.add(key)
                flags.append(
                    CriticalFlag(
                        domain=domain,
                        reason=f"{result.layer} reported a {issue.severity} issue in a "
                        f"critical domain",
                        expression=issue.expression,
                    )
                )
    return flags


def _routing_rationale(
    decision: RoutingDecision,
    blocking: Sequence[ValidationIssue],
    warnings: Sequence[ValidationIssue],
    skipped_blocking: Sequence[LayerResult],
    critical_flags: Sequence[CriticalFlag],
) -> str:
    match decision:
        case RoutingDecision.REJECT:
            layers = sorted({str(issue.layer) for issue in blocking})
            return f"Rejected: {len(blocking)} blocking issue(s) in {', '.join(layers)}."
        case RoutingDecision.NEEDS_REVIEW:
            reasons: list[str] = []
            if skipped_blocking:
                names = ", ".join(str(item.layer) for item in skipped_blocking)
                reasons.append(f"{names} did not run, so conformance is unproven")
            if critical_flags:
                domains = sorted({flag.domain for flag in critical_flags})
                reasons.append(f"issues in critical domain(s): {', '.join(domains)}")
            if warnings:
                reasons.append(f"{len(warnings)} warning(s)")
            return "Needs review: " + "; ".join(reasons) + "."
        case RoutingDecision.AUTO:
            return (
                "No blocking issues and no warnings from any layer that ran. For a "
                "standalone validation this means the resource is conformant."
            )


__all__ = ["CRITICAL_DOMAINS", "ValidationCascade", "ValidationSpec"]
