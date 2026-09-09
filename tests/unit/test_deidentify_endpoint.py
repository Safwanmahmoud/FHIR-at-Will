"""``POST /v1/deidentify``."""

from __future__ import annotations

import httpx
from fhiratwill import DeidMode

from fhirbridge.api.deps import AppServices
from fhirbridge.config import Settings


async def test_it_returns_only_the_minimized_narrative(
    client: httpx.AsyncClient,
    services: AppServices,
    settings: Settings,
) -> None:
    services.settings = settings.model_copy(update={"deid_mode": DeidMode.ENFORCED})
    name = "Jane Unique-Smith"

    response = await client.post(
        "/v1/deidentify",
        json={
            "text": f"{name} was seen on 01/02/2026.",
            "known_identifiers": {"names": [name]},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert name not in body["text"]
    assert "01/02/2026" not in body["text"]
    assert "[[NAME_" in body["text"]
    assert body["deid"]["detections"]["name"] >= 1
    assert response.headers["Cache-Control"] == "no-store"


async def test_it_fails_closed_when_deidentification_is_not_enforced(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/deidentify", json={"text": "Jane Smith"})

    assert response.status_code == 422
    assert response.json()["issue"][0]["details"]["coding"][0]["code"] == (
        "phi-minimization-required"
    )
