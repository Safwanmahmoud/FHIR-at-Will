"""Compile and submit verified target-specific FHIR write plans."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, Response
from sqlalchemy import select

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import (
    CascadeDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    TargetInvocationDep,
    TerminologyDep,
)
from fhirbridge.api.schemas import (
    DeliverRequest,
    DeliverResponse,
    TargetInfo,
    TargetsResponse,
    WritePlanRequest,
    WritePlanResponse,
)
from fhirbridge.binding.bind import bind_bundle
from fhirbridge.delivery.context import SubjectContext
from fhirbridge.delivery.models import DeliveryReceipt, PreflightStatus, WritePlan
from fhirbridge.delivery.plan import compile_write_plan
from fhirbridge.delivery.preflight import run_preflight
from fhirbridge.delivery.submit import FhirTargetClient
from fhirbridge.delivery.targets import TARGETS
from fhirbridge.domain.errors import (
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
)
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.storage.models import DeliveryAttempt, IdempotencyKey
from fhirbridge.validation.cascade import ValidationSpec
from fhirbridge.validation.models import RoutingDecision, ValidationReport

router = APIRouter(prefix="/v1", tags=["delivery"])


def _attempt(
    *,
    principal: PrincipalDep,
    body: DeliverRequest,
    target_id: str,
    report: ValidationReport,
    plan: WritePlan,
    status: str,
    response_status: int | None,
    duration_ms: int,
    delivery_id: str | None = None,
) -> DeliveryAttempt:
    preflight_status = str(plan.preflight.status) if plan.preflight else "skipped"
    return DeliveryAttempt(
        id=delivery_id or new_id(IdPrefix.DELIVERY),
        tenant_id=principal.tenant_id,
        tenant_fk=principal.tenant_id,
        conversion_id=body.conversion_id,
        target_id=target_id,
        status=status,
        validation_status=str(report.status),
        preflight_status=preflight_status,
        human_attested=bool(body.human_attested and body.reviewer_id),
        reviewer_id=body.reviewer_id if body.human_attested else None,
        response_status=response_status,
        resource_count=sum(1 for step in plan.steps if str(step.status) == "ready"),
        duration_ms=duration_ms,
    )


def _context(body: WritePlanRequest) -> SubjectContext:
    return SubjectContext(
        patient_ref=body.context.patient_ref,
        encounter_ref=body.context.encounter_ref,
        author_ref=body.context.author_ref,
        encounter_start=body.context.encounter_start,
    )


async def _build(
    body: WritePlanRequest,
    *,
    tenant_id: str,
    target: TargetInvocationDep,
    cascade: CascadeDep,
    terminology: TerminologyDep,
    settings: SettingsDep,
) -> tuple[ValidationReport, WritePlan]:
    descriptor = TARGETS.get(target.target_id)
    if descriptor is None:
        raise NotFoundError(
            "The requested delivery target is not configured.",
            safe_context={"target_id": target.target_id},
        )
    bound = await bind_bundle(body.bundle, client=terminology)
    report = await cascade.run(
        bound.bundle,
        ValidationSpec(
            profiles=tuple(body.profiles),
            max_terminology_checks=body.max_terminology_checks,
            ig_packages=settings.ig_coordinates,
        ),
    )
    report = report.model_copy(update={"conversion_id": body.conversion_id})
    client = FhirTargetClient(settings)
    try:
        preflight = await run_preflight(
            bundle=bound.bundle,
            context=_context(body),
            invocation=target,
            reader=client,
        )
    finally:
        await client.aclose()
    plan = compile_write_plan(
        bundle=bound.bundle,
        context=_context(body),
        conversion_id=body.conversion_id,
        tenant_id=tenant_id,
        descriptor=descriptor,
    ).model_copy(update={"preflight": preflight})
    if preflight.status is PreflightStatus.FAILED:
        plan = plan.model_copy(update={"ready": False, "transaction": None})
    return report, plan


@router.post("/write-plan", response_model=WritePlanResponse)
async def write_plan(
    body: WritePlanRequest,
    principal: PrincipalDep,
    target: TargetInvocationDep,
    cascade: CascadeDep,
    terminology: TerminologyDep,
    settings: SettingsDep,
    response: Response,
) -> WritePlanResponse:
    principal.require(Scope.DELIVERIES_WRITE)
    report, plan = await _build(
        body,
        tenant_id=principal.tenant_id,
        target=target,
        cascade=cascade,
        terminology=terminology,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    return WritePlanResponse(validation=report, plan=plan)


@router.post("/deliver", response_model=DeliverResponse)
async def deliver(
    body: DeliverRequest,
    principal: PrincipalDep,
    target: TargetInvocationDep,
    cascade: CascadeDep,
    terminology: TerminologyDep,
    settings: SettingsDep,
    session: SessionDep,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DeliverResponse:
    principal.require(Scope.DELIVERIES_WRITE)
    if not idempotency_key:
        raise InvalidRequestError("Idempotency-Key is required for delivery.")
    hash_input = {
        "body": body.model_dump(mode="json"),
        "target_id": target.target_id,
        "target_base_url": target.base_url,
    }
    request_hash = hashlib.sha256(
        json.dumps(hash_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.endpoint == "/v1/deliver",
            IdempotencyKey.key == idempotency_key,
        )
    )

    report, plan = await _build(
        body,
        tenant_id=principal.tenant_id,
        target=target,
        cascade=cascade,
        terminology=terminology,
        settings=settings,
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError("The idempotency key was used with another request.")
        if existing.status == "completed" and existing.response_body:
            response.headers["Cache-Control"] = "no-store"
            return DeliverResponse(
                validation=report,
                plan=plan,
                receipt=DeliveryReceipt.model_validate(existing.response_body),
            )
        raise IdempotencyConflictError("An identical delivery is already in progress.")

    if report.status is RoutingDecision.REJECT or not plan.ready:
        session.add(
            _attempt(
                principal=principal,
                body=body,
                target_id=target.target_id,
                report=report,
                plan=plan,
                status="denied",
                response_status=None,
                duration_ms=0,
            )
        )
        await session.commit()
        raise InvalidRequestError("The verified write plan is not eligible for delivery.")
    if (
        report.status is RoutingDecision.NEEDS_REVIEW
        or (plan.preflight is not None and plan.preflight.status is PreflightStatus.SKIPPED)
    ) and not (body.human_attested and body.reviewer_id):
        session.add(
            _attempt(
                principal=principal,
                body=body,
                target_id=target.target_id,
                report=report,
                plan=plan,
                status="denied",
                response_status=None,
                duration_ms=0,
            )
        )
        await session.commit()
        raise InvalidRequestError("Human attestation and reviewer_id are required.")

    key_row = IdempotencyKey(
        id=new_id(IdPrefix.DECISION),
        tenant_id=principal.tenant_id,
        tenant_fk=principal.tenant_id,
        key=idempotency_key,
        endpoint="/v1/deliver",
        request_hash=request_hash,
        status="in_progress",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(key_row)
    await session.flush()

    client = FhirTargetClient(settings)
    started = datetime.now(UTC)
    try:
        receipt = await client.submit(target, plan)
    except Exception as exc:
        key_row.status = "failed"
        safe_context = getattr(exc, "safe_context", {})
        target_status = safe_context.get("status") if isinstance(safe_context, dict) else None
        session.add(
            _attempt(
                principal=principal,
                body=body,
                target_id=target.target_id,
                report=report,
                plan=plan,
                status="failed",
                response_status=target_status if isinstance(target_status, int) else None,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )
        )
        await session.commit()
        raise
    finally:
        await client.aclose()
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    session.add(
        _attempt(
            principal=principal,
            body=body,
            target_id=target.target_id,
            report=report,
            plan=plan,
            status="submitted",
            response_status=receipt.response_status,
            duration_ms=duration_ms,
            delivery_id=receipt.delivery_id,
        )
    )
    key_row.status = "completed"
    key_row.response_status = 200
    key_row.response_body = receipt.model_dump(mode="json")
    key_row.resource_id = receipt.delivery_id
    response.headers["Cache-Control"] = "no-store"
    return DeliverResponse(validation=report, plan=plan, receipt=receipt)


@router.get("/targets", response_model=TargetsResponse)
async def targets(principal: PrincipalDep) -> TargetsResponse:
    principal.require(Scope.DELIVERIES_WRITE)
    return TargetsResponse(
        targets=[
            TargetInfo(
                id=target.target_id,
                version=target.version,
                failure_policy=target.failure_policy,
            )
            for target in TARGETS.values()
        ]
    )


__all__ = ["router"]
