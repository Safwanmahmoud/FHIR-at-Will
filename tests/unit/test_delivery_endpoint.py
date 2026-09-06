from __future__ import annotations

import httpx
import respx
from fastapi import FastAPI

from fhirbridge.api.auth import Principal, Scope
from fhirbridge.api.deps import AppServices, get_principal
from fhirbridge.config import DeliveryMode
from tests.helpers import fhir_json

TARGET_HEADERS = {
    "X-Target-Id": "generic-fhir-r4",
    "X-Target-Base-Url": "https://target.test/fhir/R4",
    "X-Target-Token": "synthetic-token",
    "X-PHI-Egress-Acknowledged": "true",
}


def _principal() -> Principal:
    return Principal(
        tenant_id="ten_delivery",
        actor_type="api_key",
        actor_id="key_delivery",
        scopes=frozenset({Scope.DELIVERIES_WRITE}),
    )


async def test_lists_the_built_in_target(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_principal] = _principal
    response = await client.get("/v1/targets")
    assert response.status_code == 200
    assert response.json()["targets"] == [
        {
            "id": "generic-fhir-r4",
            "version": "v1",
            "failure_policy": "atomic_transaction",
        }
    ]


async def test_compiles_a_plan_after_readback_preflight(
    app: FastAPI,
    client: httpx.AsyncClient,
    services: AppServices,
    mock_http: respx.MockRouter,
    all_dependencies_healthy: None,
) -> None:
    del all_dependencies_healthy
    app.dependency_overrides[get_principal] = _principal
    services.settings = services.settings.model_copy(
        update={
            "delivery_mode": DeliveryMode.PLAN_ONLY,
            "target_egress_allowlist": ["target.test"],
        }
    )
    mock_http.get("https://target.test/fhir/R4/Patient/target").mock(
        return_value=fhir_json(
            {
                "resourceType": "Patient",
                "id": "target",
                "gender": "male",
                "birthDate": "2000-01-01",
            }
        )
    )
    response = await client.post(
        "/v1/write-plan",
        headers=TARGET_HEADERS,
        json={
            "conversion_id": "cnv_test",
            "bundle": {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [
                    {
                        "fullUrl": "urn:uuid:patient",
                        "resource": {
                            "resourceType": "Patient",
                            "gender": "male",
                        },
                    },
                    {
                        "fullUrl": "urn:uuid:observation",
                        "resource": {
                            "resourceType": "Observation",
                            "status": "final",
                            "code": {
                                "coding": [
                                    {
                                        "system": "http://loinc.org",
                                        "code": "8867-4",
                                        "display": "Heart rate",
                                    }
                                ]
                            },
                            "subject": {"reference": "urn:uuid:patient"},
                            "valueQuantity": {
                                "value": 72,
                                "system": "http://unitsofmeasure.org",
                                "code": "/min",
                                "unit": "per minute",
                            },
                        },
                    },
                ],
            },
            "context": {"patient_ref": "Patient/target"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["plan"]["ready"] is True
    assert payload["plan"]["transaction"]["type"] == "transaction"
    assert payload["plan"]["preflight"]["status"] == "passed"
