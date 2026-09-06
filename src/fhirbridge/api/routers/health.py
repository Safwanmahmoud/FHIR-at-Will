"""Liveness, readiness, version and metrics.

The distinction that matters operationally: ``/livez`` answers "is this process
alive" and never touches a dependency, so a terminology outage does not get the
container killed and restarted pointlessly. ``/readyz`` answers "can this
process do its job", which for a service whose whole premise is verification
means the validator and terminology server must both answer — and, for the
validator, must actually have the configured IGs loaded. A validator without US
Core returns a clean outcome for a resource claiming a US Core profile, so
"reachable" is not "ready".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from fhirbridge.api.deps import PrincipalDep, Services
from fhirbridge.api.schemas import (
    DependencyHealthResponse,
    DependencyStatus,
    LiveResponse,
    ReadyResponse,
    VersionResponse,
)
from fhirbridge.observability import metrics
from fhirbridge.storage.rls import check_rls
from fhirbridge.version import (
    BINDING_TABLE_VERSION,
    CODE_VERSION,
    DEID_RULESET_VERSION,
    DELIVERY_PLAN_SCHEMA_VERSION,
    FACT_SCHEMA_VERSION,
    PROMPT_SET_VERSION,
    TARGET_DESCRIPTOR_VERSION,
    TYPED_MODEL_FHIR_VERSION,
    VALIDATION_REPORT_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["platform"])

SENTINEL_PROFILES: Final[dict[str, str]] = {
    "hl7.fhir.us.core": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient",
}
"""One profile per known IG, used to prove the IG is really loaded.

Resolving a single profile is enough to distinguish "IG present" from "IG
missing", which is the failure this probe exists to catch. It is not a claim
that every profile in the package resolves.
"""

_SNOMED: Final[str] = "http://snomed.info/sct"
_LOINC: Final[str] = "http://loinc.org"


def _sentinel_profiles(ig_coordinates: tuple[str, ...]) -> list[str]:
    profiles: list[str] = []
    for coordinate in ig_coordinates:
        name = coordinate.split("#", 1)[0]
        profile = SENTINEL_PROFILES.get(name)
        if profile:
            profiles.append(profile)
    return profiles


async def _database_status(services: Services) -> DependencyStatus:
    try:
        async with services.session_factory() as session:
            await session.execute(text("SELECT 1"))
            rls = await check_rls(session)
    except Exception as exc:
        logger.warning("dependency_down", extra={"dependency": "postgres"})
        metrics.DEPENDENCY_UP.labels(dependency="postgres").set(0)
        return DependencyStatus(
            name="postgres",
            status="down",
            detail=f"{type(exc).__name__} while executing the readiness query.",
        )

    if not rls.enforced:
        # Reachable, and every query would succeed — which is the problem. With
        # RLS inert, one missing WHERE clause anywhere serves another tenant's
        # chart. Refuse traffic by default rather than discover it later
        # (AGENTS.md 8.2; principle 2.4 applied to isolation).
        required = services.settings.require_rls_enforcement
        metrics.DEPENDENCY_UP.labels(dependency="postgres").set(0 if required else 1)
        metrics.RLS_ENFORCED.set(0)
        return DependencyStatus(
            name="postgres",
            status="down" if required else "degraded",
            detail=rls.detail,
        )

    metrics.RLS_ENFORCED.set(1)
    metrics.DEPENDENCY_UP.labels(dependency="postgres").set(1)
    return DependencyStatus(name="postgres", status="up")


async def _validator_status(services: Services) -> DependencyStatus:
    required = _sentinel_profiles(services.settings.ig_coordinates)
    health = await services.validator.health(required_profiles=required)
    if not health.reachable:
        return DependencyStatus(
            name="validator",
            status="down",
            detail=health.detail,
            version=services.settings.validator_version,
        )
    if health.profiles_missing:
        # Reachable but useless for conformance: report degraded rather than up,
        # so a deployment missing its IGs cannot quietly serve clean reports.
        return DependencyStatus(
            name="validator",
            status="degraded",
            detail=health.detail,
            latency_ms=health.latency_ms,
            version=services.settings.validator_version,
        )
    return DependencyStatus(
        name="validator",
        status="up",
        latency_ms=health.latency_ms,
        version=services.settings.validator_version,
    )


async def _terminology_status(services: Services) -> DependencyStatus:
    health = await services.terminology.health(code_systems=(_SNOMED, _LOINC))
    if not health.reachable:
        return DependencyStatus(name="terminology", status="down", detail=health.detail)
    return DependencyStatus(
        name="terminology",
        status="up",
        latency_ms=health.latency_ms,
        version=health.software,
    )


async def _collect(services: Services) -> list[DependencyStatus]:
    results = await asyncio.gather(
        _database_status(services),
        _validator_status(services),
        _terminology_status(services),
        return_exceptions=True,
    )
    statuses: list[DependencyStatus] = []
    for name, result in zip(("postgres", "validator", "terminology"), results, strict=True):
        if isinstance(result, DependencyStatus):
            statuses.append(result)
        else:
            # A probe that itself raised counts as down. The exception message is
            # not echoed: it can contain connection strings.
            logger.warning(
                "dependency_probe_failed",
                extra={"dependency": name, "exception_type": type(result).__name__},
            )
            statuses.append(
                DependencyStatus(
                    name=name, status="down", detail="The health probe raised an exception."
                )
            )
    return statuses


def _worst(statuses: list[DependencyStatus]) -> str:
    if any(item.status == "down" for item in statuses):
        return "down"
    if any(item.status == "degraded" for item in statuses):
        return "degraded"
    return "up"


@router.get(
    "/livez",
    summary="Liveness probe",
    response_model=LiveResponse,
    include_in_schema=False,
)
async def livez() -> LiveResponse:
    return LiveResponse()


@router.get(
    "/readyz",
    summary="Readiness probe",
    response_model=ReadyResponse,
    include_in_schema=False,
)
async def readyz(services: Services, response: Response) -> ReadyResponse:
    statuses = await _collect(services)
    overall = _worst(statuses)
    ready = overall == "up"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "15"
    return ReadyResponse(ready=ready, dependencies=statuses)


@router.get(
    "/version",
    summary="Build and pin versions",
    response_model=VersionResponse,
)
async def version(services: Services) -> VersionResponse:
    settings = services.settings
    return VersionResponse(
        service=settings.service_name,
        version=CODE_VERSION,
        fhir_version=settings.default_fhir_version,
        typed_model_fhir_version=TYPED_MODEL_FHIR_VERSION,
        prompt_set_version=PROMPT_SET_VERSION,
        deid_ruleset_version=DEID_RULESET_VERSION,
        fact_schema_version=FACT_SCHEMA_VERSION,
        validation_report_schema_version=VALIDATION_REPORT_SCHEMA_VERSION,
        binding_table_version=BINDING_TABLE_VERSION,
        target_descriptor_version=TARGET_DESCRIPTOR_VERSION,
        delivery_plan_schema_version=DELIVERY_PLAN_SCHEMA_VERSION,
        ig_packages=list(settings.ig_coordinates),
        validator_version=settings.validator_version,
        environment=str(settings.environment),
    )


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    include_in_schema=False,
    response_class=Response,
)
async def prometheus_metrics() -> Response:
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


@router.get(
    "/v1/health/dependencies",
    summary="Detailed dependency health",
    response_model=DependencyHealthResponse,
    tags=["platform"],
)
async def dependency_health(
    services: Services, principal: PrincipalDep, response: Response
) -> DependencyHealthResponse:
    """The detailed view, which ``/readyz`` deliberately is not.

    Authenticated, unlike ``/readyz``: this response names the terminology server
    software and the validator version, which is exactly the fingerprint an
    attacker wants and which no orchestrator needs.
    """
    del principal
    statuses = await _collect(services)
    overall = _worst(statuses)
    if overall == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "15"
    return DependencyHealthResponse(
        status=overall,  # type: ignore[arg-type]  # _worst returns exactly these literals
        dependencies=statuses,
    )


__all__ = ["router"]
