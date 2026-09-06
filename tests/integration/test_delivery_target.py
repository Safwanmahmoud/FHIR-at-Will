from __future__ import annotations

import os

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generic_fhir_target_accepts_atomic_transaction() -> None:
    """Opt-in smoke test for a local HAPI JPA or equivalent FHIR R4 server."""
    base_url = (os.environ.get("FHIR_TARGET_URL") or "").rstrip("/")
    if not base_url:
        pytest.skip("FHIR_TARGET_URL is not set; point it at a disposable FHIR R4 server")
    transaction = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "identifier": [
                        {
                            "system": "https://fhirbridge.org/test",
                            "value": "synthetic-delivery-patient",
                        }
                    ],
                },
                "request": {
                    "method": "POST",
                    "url": "Patient",
                    "ifNoneExist": (
                        "identifier=https%3A%2F%2Ffhirbridge.org%2Ftest"
                        "%7Csynthetic-delivery-patient"
                    ),
                },
            }
        ],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            base_url,
            json=transaction,
            headers={"Content-Type": "application/fhir+json"},
        )
    assert response.status_code == 200
    assert response.json()["resourceType"] == "Bundle"
