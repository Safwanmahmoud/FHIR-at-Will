"""``POST /v1/NAR2FHIR``.

One grounded model call extracts catalog-constrained facts; assembly into FHIR is
then deterministic (:mod:`fhirbridge.fhir.assemble`). No model ever sees a Bundle,
so the same entities always produce the same Bundle, and the two failure modes of
a generation call -- inventing a code, or nesting a string where FHIR wants an
object -- are structurally unavailable rather than discouraged by a prompt.

This endpoint does not validate the generated Bundle; callers must submit it
separately to ``POST /v1/validate``. What assembly could not ground is reported in
``assembly`` on the response, which is the list a reviewer reads.

The endpoint is BYOK -- the caller supplies provider, model and key in
``X-LLM-*`` headers -- and requires the ``conversions:write`` scope.

This build's conversion is synchronous and stateless: like ``/v1/validate`` it
retains nothing. The persisted, fact-based, asynchronous ``/v1/conversions``
resource (with extraction stages and source-span fidelity) is milestone M3.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import (
    LlmGatewayDep,
    LlmInvocationDep,
    PrincipalDep,
    SettingsDep,
    TerminologyDep,
)
from fhirbridge.api.schemas import (
    AssemblyNote,
    BindingInfo,
    BindingNote,
    ConvertRequest,
    ConvertResponse,
    DeidInfo,
    KnownIdentifiers,
    LlmCallInfo,
)
from fhirbridge.deid.detectors import DeclaredIdentifier
from fhirbridge.deid.policy import DeidPolicy
from fhirbridge.deid.spans import IdentifierClass
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.fhir.assemble import AssembledBundle, AssemblyAction
from fhirbridge.llm.conversion import convert_narrative
from fhirbridge.llm.gateway import LlmResult
from fhirbridge.llm.invocation import LlmInvocation
from fhirbridge.llm.qualification import resolve_tier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["conversion"])

_LLM_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "No LLM credentials, or the provider rejected the key."},
    422: {
        "description": (
            "The model output could not be parsed, the model is not qualified, the "
            "input exceeds the context window, or the budget would be exceeded."
        )
    },
    429: {"description": "The LLM provider rate-limited the request. Retryable."},
    451: {"description": "The target LLM host is blocked by egress policy."},
}

_DECLARED_FIELDS: dict[str, IdentifierClass] = {
    "names": IdentifierClass.NAME,
    "dates": IdentifierClass.DATE,
    "medical_record_numbers": IdentifierClass.MRN,
    "phones": IdentifierClass.PHONE,
    "emails": IdentifierClass.EMAIL,
    "addresses": IdentifierClass.ADDRESS,
    "locations": IdentifierClass.LOCATION,
    "account_numbers": IdentifierClass.ACCOUNT,
    "license_numbers": IdentifierClass.LICENSE,
    "device_identifiers": IdentifierClass.DEVICE,
}


def declared_identifiers_of(known: KnownIdentifiers) -> list[DeclaredIdentifier]:
    return [
        DeclaredIdentifier(identifier_class=identifier_class, value=value)
        for field, identifier_class in _DECLARED_FIELDS.items()
        for value in getattr(known, field)
        if value.strip()
    ]


def deid_info_of(report: Any) -> DeidInfo:
    return DeidInfo(
        mode=report.mode,
        profile=report.profile,
        ruleset_version=report.ruleset_version,
        detections=report.detections,
        replacements=report.replacements,
        restored=report.restored,
        residual_risk="not_assessed",
    )


def assembly_notes_of(assembled: AssembledBundle) -> list[AssemblyNote]:
    """Map the assembler's PHI-free notes onto the response schema.

    Shared with the voice endpoint so both report assembly identically.
    """
    return [
        AssemblyNote(
            entry_index=note.entry_index,
            resource_type=note.resource_type,
            element=note.element,
            action=note.action,
            detail=note.detail,
        )
        for note in assembled.notes
    ]


def binding_info_of(bound: Any) -> BindingInfo:
    """Map binding evidence without exposing source text."""
    return BindingInfo(
        table_version=bound.table_version,
        eligible=bound.coverage.eligible,
        bound=bound.coverage.bound,
        unbound=bound.coverage.unbound,
        ambiguous=bound.coverage.ambiguous,
        notes=[
            BindingNote(
                entry_index=note.entry_index,
                resource_type=note.resource_type,
                element=note.element,
                action=note.action,
                value_set=note.value_set,
                candidate_count=note.candidate_count,
                detail=note.detail,
            )
            for note in bound.notes
        ],
    )


def llm_call_info_of(extraction: LlmResult, invocation: LlmInvocation) -> LlmCallInfo:
    """Build the extraction-call provenance shared by both conversion endpoints."""
    return LlmCallInfo(
        provider=invocation.provider,
        model=extraction.model,
        usage=extraction.usage,
        cost_usd=float(extraction.cost_usd) if extraction.cost_usd is not None else None,
        latency_ms=extraction.latency_ms,
        qualification_tier=str(resolve_tier(invocation.model)),
    )


@router.post(
    "/NAR2FHIR",
    summary="Convert narrative to an unvalidated FHIR Bundle with grounded extraction (BYOK)",
    response_model=ConvertResponse,
    responses=_LLM_ERROR_RESPONSES,
)
async def nar2fhir(
    body: ConvertRequest,
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    gateway: LlmGatewayDep,
    settings: SettingsDep,
    terminology: TerminologyDep,
    response: Response,
) -> ConvertResponse:
    """Extract grounded facts and assemble them into an unvalidated FHIR Bundle."""
    principal.require(Scope.CONVERSIONS_WRITE)
    conversion_id = new_id(IdPrefix.CONVERSION)

    result = await convert_narrative(
        body.text,
        gateway=gateway,
        invocation=invocation,
        conversion_id=conversion_id,
        policy=DeidPolicy.from_settings(settings),
        terminology=terminology,
        declared_identifiers=declared_identifiers_of(body.known_identifiers),
    )
    assembled = result.assembled

    logger.info(
        "conversion_completed",
        extra={
            # Identifiers and counts only; the narrative and the bundle are PHI and
            # never reach a log (principle 2.6). Assembly notes are PHI-free by
            # construction, so their counts are safe to record.
            "conversion_id": conversion_id,
            "actor_id": principal.actor_id,
            "model": result.extraction.model,
            "resource_count": len(assembled.bundle["entry"]),
            "inferred_count": sum(
                1 for note in assembled.notes if note.action is AssemblyAction.INFERRED
            ),
            "dropped_count": sum(
                1 for note in assembled.notes if note.action is AssemblyAction.DROPPED
            ),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return ConvertResponse(
        conversion_id=conversion_id,
        bundle=result.binding.bundle,
        validated=False,
        assembly=assembly_notes_of(assembled),
        binding=binding_info_of(result.binding),
        llm=llm_call_info_of(result.extraction, invocation),
        deid=deid_info_of(result.deid),
    )


__all__ = [
    "assembly_notes_of",
    "binding_info_of",
    "declared_identifiers_of",
    "deid_info_of",
    "llm_call_info_of",
    "router",
]
