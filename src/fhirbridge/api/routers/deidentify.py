"""``POST /v1/deidentify`` — expose the configured deterministic minimizer."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response
from fhiratwill import DeidPolicy
from fhiratwill import deidentify as deidentify_text

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import PrincipalDep, SettingsDep
from fhirbridge.api.routers.convert import declared_identifiers_of, deid_info_of
from fhirbridge.api.schemas import DeidentifyRequest, DeidentifyResponse
from fhirbridge.domain.errors import PhiMinimizationRequiredError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["conversion"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "description": (
            "De-identification is not enforced for this deployment, or the request is invalid."
        )
    },
    503: {"description": "The required de-identification assets are unavailable."},
}


@router.post(
    "/deidentify",
    summary="De-identify a clinical narrative with the configured deterministic layer",
    response_model=DeidentifyResponse,
    responses=_ERROR_RESPONSES,
)
async def deidentify(
    body: DeidentifyRequest,
    principal: PrincipalDep,
    settings: SettingsDep,
    response: Response,
) -> DeidentifyResponse:
    principal.require(Scope.CONVERSIONS_WRITE)
    policy = DeidPolicy(mode=settings.deid_mode, profile=settings.deid_profile)
    if not policy.enforced:
        raise PhiMinimizationRequiredError(
            "Set DEID_MODE=enforced before using the de-identification endpoint."
        )

    result = deidentify_text(
        body.text,
        policy=policy,
        known_identifiers=declared_identifiers_of(body.known_identifiers),
    )
    logger.info(
        "narrative_deidentified",
        extra={
            "profile": result.profile,
            "ruleset_version": result.ruleset_version,
            "replacement_count": result.replacements,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return DeidentifyResponse(text=result.text, deid=deid_info_of(result))


__all__ = ["router"]
