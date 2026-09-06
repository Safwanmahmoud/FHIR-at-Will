"""Wrong-patient and wrong-encounter guard for delivery."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Protocol

from fhirbridge.delivery.context import SubjectContext
from fhirbridge.delivery.invocation import TargetInvocation
from fhirbridge.delivery.models import (
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
)
from fhirbridge.domain.errors import DomainError, TargetUnavailableError


class ResourceReader(Protocol):
    async def read(self, invocation: TargetInvocation, reference: str) -> dict[str, Any]: ...


async def run_preflight(
    *,
    bundle: dict[str, Any],
    context: SubjectContext,
    invocation: TargetInvocation,
    reader: ResourceReader,
) -> PreflightReport:
    """Verify destination context against narrative evidence without selecting identity."""
    try:
        patient = await reader.read(invocation, context.patient_ref)
        encounter = (
            await reader.read(invocation, context.encounter_ref) if context.encounter_ref else None
        )
    except Exception as exc:
        if isinstance(exc, DomainError):
            raise
        raise TargetUnavailableError(
            "The target could not be read for identity preflight.",
            safe_context={"target_id": invocation.target_id},
        ) from exc

    checks: list[PreflightCheck] = []
    narrative_patient = _first_resource(bundle, "Patient")
    _check_gender(checks, narrative_patient, patient)
    _check_age(checks, bundle, patient)
    _check_encounter(checks, bundle, encounter)

    if any(check.status is PreflightStatus.FAILED for check in checks):
        status = PreflightStatus.FAILED
    elif checks and all(check.status is PreflightStatus.SKIPPED for check in checks):
        status = PreflightStatus.SKIPPED
    else:
        status = PreflightStatus.PASSED
    return PreflightReport(status=status, checks=checks)


def _first_resource(bundle: dict[str, Any], resource_type: str) -> dict[str, Any] | None:
    for entry in bundle.get("entry", []):
        if isinstance(entry, dict):
            resource = entry.get("resource")
            if isinstance(resource, dict) and resource.get("resourceType") == resource_type:
                return resource
    return None


def _check_gender(
    checks: list[PreflightCheck],
    narrative: dict[str, Any] | None,
    patient: dict[str, Any],
) -> None:
    stated = narrative.get("gender") if narrative else None
    actual = patient.get("gender")
    if not isinstance(stated, str):
        checks.append(
            PreflightCheck(
                check="gender",
                status=PreflightStatus.SKIPPED,
                detail="not stated",
            )
        )
    elif not isinstance(actual, str) or stated.lower() != actual.lower():
        checks.append(
            PreflightCheck(
                check="gender",
                status=PreflightStatus.FAILED,
                detail="patient gender mismatch",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check="gender",
                status=PreflightStatus.PASSED,
                detail="matched",
            )
        )


def _check_age(
    checks: list[PreflightCheck], bundle: dict[str, Any], patient: dict[str, Any]
) -> None:
    age = _narrative_age(bundle)
    birth_date = patient.get("birthDate")
    if age is None:
        checks.append(
            PreflightCheck(check="age", status=PreflightStatus.SKIPPED, detail="not stated")
        )
        return
    try:
        born = date.fromisoformat(str(birth_date))
    except ValueError:
        checks.append(
            PreflightCheck(
                check="age",
                status=PreflightStatus.FAILED,
                detail="patient birthDate unavailable",
            )
        )
        return
    today = datetime.now(UTC).date()
    actual = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    status = PreflightStatus.PASSED if abs(actual - age) <= 1 else PreflightStatus.FAILED
    checks.append(
        PreflightCheck(
            check="age",
            status=status,
            detail=(
                "matched within tolerance"
                if status is PreflightStatus.PASSED
                else "birthDate outside tolerance"
            ),
        )
    )


def _narrative_age(bundle: dict[str, Any]) -> int | None:
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {}) if isinstance(entry, dict) else {}
        if resource.get("resourceType") != "Observation":
            continue
        code = resource.get("code")
        text = code.get("text", "") if isinstance(code, dict) else ""
        value = resource.get("valueQuantity")
        if str(text).strip().lower() == "age" and isinstance(value, dict):
            raw = value.get("value")
            if isinstance(raw, (int, float)):
                return int(raw)
    return None


def _check_encounter(
    checks: list[PreflightCheck],
    bundle: dict[str, Any],
    encounter: dict[str, Any] | None,
) -> None:
    narrative = _first_resource(bundle, "Encounter")
    if narrative is None or not isinstance(narrative.get("period"), dict):
        checks.append(
            PreflightCheck(
                check="encounter_period",
                status=PreflightStatus.SKIPPED,
                detail="not stated",
            )
        )
        return
    target_period = encounter.get("period") if encounter else None
    if not isinstance(target_period, dict):
        checks.append(
            PreflightCheck(
                check="encounter_period",
                status=PreflightStatus.FAILED,
                detail="target encounter period unavailable",
            )
        )
        return
    stated = narrative["period"].get("start")
    target = target_period.get("start")
    status = PreflightStatus.PASSED if stated == target else PreflightStatus.FAILED
    checks.append(
        PreflightCheck(
            check="encounter_period",
            status=status,
            detail="matched" if status is PreflightStatus.PASSED else "encounter period mismatch",
        )
    )


__all__ = ["ResourceReader", "run_preflight"]
