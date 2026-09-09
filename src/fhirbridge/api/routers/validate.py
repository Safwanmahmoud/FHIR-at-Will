"""``POST /v1/validate`` — score any FHIR resource or Bundle (AGENTS.md 11.4).

This endpoint is the project's on-ramp, and the milestone that must exist before
any generation code does: you cannot build a trustworthy generator before you can
measure one. It requires authentication (an unauthenticated compute endpoint is a
denial-of-service invitation) but no scope, no LLM credentials, and it retains
nothing — the submitted resource is validated and dropped.

Profiles may be declared in ``meta.profile`` or passed explicitly. Either way, a
profile the validator cannot resolve produces ``422 ig-not-loaded`` rather than a
clean report, and a dependency outage produces ``503`` (principle 2.4).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Response
from fhiratwill import ValidationReport, ValidationSpec

from fhirbridge.api.deps import CascadeDep, PrincipalDep, SettingsDep
from fhirbridge.api.schemas import ValidateRequest
from fhirbridge.fhir.operation_outcome import FHIR_JSON_MEDIA_TYPE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["validation"])

_EXAMPLE: dict[str, Any] = {
    "resource": {
        "resourceType": "Observation",
        "status": "preliminary",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]
        },
        "subject": {"reference": "Patient/example"},
        "valueQuantity": {
            "value": 72,
            "unit": "beats/minute",
            "system": "http://unitsofmeasure.org",
            "code": "/min",
        },
    },
    "profiles": [
        "http://hl7.org/fhir/us/core/StructureDefinition/us-core-vital-signs",
    ],
}


def _spec_for(body: ValidateRequest, ig_packages: tuple[str, ...]) -> ValidationSpec:
    return ValidationSpec(
        profiles=tuple(body.profiles),
        layers=frozenset(body.layers) if body.layers is not None else None,
        severity_overrides={key: str(value) for key, value in body.severity_overrides.items()},
        max_terminology_checks=body.max_terminology_checks,
        ig_packages=ig_packages,
    )


@router.post(
    "/validate",
    summary="Validate a FHIR resource or Bundle through the L1-L5 cascade",
    response_model=ValidationReport,
    responses={
        400: {"description": "The payload is not a structurally valid FHIR resource."},
        422: {"description": "A requested profile is not loaded in the validator sidecar."},
        503: {
            "description": (
                "The validator or terminology server is unavailable. The request fails "
                "closed rather than returning an unverified report."
            )
        },
    },
)
async def validate(
    principal: PrincipalDep,
    cascade: CascadeDep,
    settings: SettingsDep,
    response: Response,
    body: ValidateRequest = Body(openapi_examples={"vital_sign": {"value": _EXAMPLE}}),
) -> ValidationReport:
    report = await cascade.run(body.resource, _spec_for(body, settings.ig_coordinates))

    logger.info(
        "validate_completed",
        extra={
            # Counts and decisions only. Issue messages quote element values and
            # must never reach a log (principle 2.6).
            "resource_type": report.resource_type,
            "resource_count": report.resource_count,
            "decision": str(report.status),
            "actor_id": principal.actor_id,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return report


@router.post(
    "/validate/outcome",
    summary="Validate and return a FHIR OperationOutcome instead of a report",
    response_class=Response,
    responses={
        200: {
            "content": {FHIR_JSON_MEDIA_TYPE: {}},
            "description": "An OperationOutcome carrying one issue per finding.",
        }
    },
)
async def validate_as_outcome(
    principal: PrincipalDep,
    cascade: CascadeDep,
    settings: SettingsDep,
    body: ValidateRequest = Body(),
) -> Response:
    """The same cascade, rendered as an ``OperationOutcome``.

    Offered because FHIR tooling expects a validation call to answer with an
    OperationOutcome. The report shape carries strictly more information, so
    prefer ``POST /v1/validate`` when the client is ours.
    """
    del principal
    report = await cascade.run(body.resource, _spec_for(body, settings.ig_coordinates))
    return Response(
        content=json.dumps(report_to_outcome(report), separators=(",", ":")),
        media_type=FHIR_JSON_MEDIA_TYPE,
        headers={"Cache-Control": "no-store"},
    )


_ISSUE_TYPES = frozenset(
    {
        "invalid",
        "structure",
        "required",
        "value",
        "invariant",
        "security",
        "login",
        "unknown",
        "expired",
        "forbidden",
        "suppressed",
        "processing",
        "not-supported",
        "duplicate",
        "multiple-matches",
        "not-found",
        "deleted",
        "too-long",
        "code-invalid",
        "extension",
        "too-costly",
        "business-rule",
        "conflict",
        "transient",
        "lock-error",
        "no-store",
        "exception",
        "timeout",
        "incomplete",
        "throttled",
        "informational",
    }
)
"""The FHIR ``issue-type`` value set, which has a required binding.

L5 issue codes are rule ids, not issue types, so they are mapped to
``processing`` rather than emitted verbatim — otherwise the OperationOutcome we
hand back would itself fail validation.
"""


def report_to_outcome(report: ValidationReport) -> dict[str, Any]:
    """Render a :class:`ValidationReport` as a FHIR ``OperationOutcome``.

    Every layer contributes, including the ones that did not run: those become
    ``information`` issues naming the layer and the reason. A caller reading only
    the outcome still learns which checks were not performed, which is the whole
    point of reporting skipped layers explicitly.
    """
    issues: list[dict[str, Any]] = []
    for layer in report.layers:
        for issue in layer.issues:
            entry: dict[str, Any] = {
                "severity": str(issue.severity),
                "code": issue.code if issue.code in _ISSUE_TYPES else "processing",
                "details": {"text": f"[{layer.layer}] {issue.message}"},
            }
            if issue.expression:
                entry["expression"] = [issue.expression]
            issues.append(entry)
        if layer.skipped_reason:
            issues.append(
                {
                    "severity": "information",
                    "code": "informational",
                    "details": {"text": f"[{layer.layer}] {layer.status}: {layer.skipped_reason}"},
                }
            )
    if not issues:
        issues.append(
            {
                "severity": "information",
                "code": "informational",
                "details": {"text": "No issues found by any layer that ran."},
            }
        )
    return {"resourceType": "OperationOutcome", "issue": issues}


__all__ = ["report_to_outcome", "router"]
