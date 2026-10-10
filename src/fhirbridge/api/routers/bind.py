"""``POST /v1/bind`` — nearest-neighbor terminology binding for a phrase."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import PrincipalDep
from fhirbridge.api.schemas import BindNeighbor, BindRequest, BindResponse
from fhirbridge.terminology.nn_index import get_index

router = APIRouter(prefix="/v1", tags=["terminology"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {"description": "The terminology index could not be loaded."},
}


@router.post(
    "/bind",
    summary="Bind a free-text phrase to the nearest catalog code (ICD-10-CM by default)",
    response_model=BindResponse,
    responses=_ERROR_RESPONSES,
)
async def bind_phrase(
    body: BindRequest,
    principal: PrincipalDep,
    response: Response,
) -> BindResponse:
    principal.require(Scope.CONVERSIONS_WRITE)
    result = get_index().bind(
        body.query,
        k=body.k,
        leaves_only=body.leaves_only,
        min_score=body.min_score,
        names=body.dictionaries or None,
    )
    response.headers["Cache-Control"] = "no-store"
    return BindResponse(
        query=result.query,
        coding=result.coding,
        confidence=result.confidence,
        score_mean=result.score_mean,
        score_std=result.score_std,
        neighbors=[
            BindNeighbor(
                dictionary=hit.dictionary,
                system=hit.system,
                code=hit.code,
                display=hit.display,
                matched_text=hit.matched_text,
                leaf=hit.leaf,
                score=hit.score,
                coding=hit.coding,
            )
            for hit in result.neighbors
        ],
    )


__all__ = ["router"]
