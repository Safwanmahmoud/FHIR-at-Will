"""``POST /v1/NAR2FHIR``.

The gateway is scripted here (``FakeLlmGateway``): the point is what the endpoint
does with the model's output rather than how the bytes were fetched. Since Bundle
assembly became deterministic, the model's only contribution is the entity list, so
these tests script that and assert on the Bundle the endpoint built from it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fhiratwill.tags import (
    AI_DERIVED,
    MACHINE_CODED,
    MACHINE_INFERRED,
    PROVENANCE_TAG_SYSTEM,
)

from fhirbridge.api.auth import Principal
from fhirbridge.api.deps import get_llm_gateway, get_principal
from fhirbridge.domain.errors import EgressBlockedError, LlmSchemaViolationError
from tests.fakes import FakeLlmGateway

EXTRACTED = {
    "entities": [
        {
            "resourceType": "Patient",
            "instance": "patient-1",
            "keyword": "gender",
            "value": "male",
        },
        {
            "resourceType": "Observation",
            "instance": "obs-hr",
            "keyword": "code",
            "value": "heart rate",
        },
        {
            "resourceType": "Observation",
            "instance": "obs-hr",
            "keyword": "valueQuantity",
            "value": "72 /min",
        },
    ]
}

BYOK_HEADERS = {
    "X-LLM-Provider": "openrouter",
    "X-LLM-Model": "openai/gpt-4o-mini",
    "X-LLM-API-Key": "sk-test",
    "X-PHI-Egress-Acknowledged": "true",
}


def nar2fhir_gateway() -> FakeLlmGateway:
    return FakeLlmGateway(resource=EXTRACTED)


@pytest.fixture(autouse=True)
def _binding_terminology(terminology_valid: object) -> None:
    del terminology_valid


def resource_at(body: dict, resource_type: str) -> dict:
    return next(
        entry["resource"]
        for entry in body["bundle"]["entry"]
        if entry["resource"]["resourceType"] == resource_type
    )


def mask_uuids(value: Any) -> Any:
    """Replace conversion-scoped ``urn:uuid`` values so two Bundles can be compared.

    Linkage itself is asserted separately; here the question is whether the same
    entities produced the same content.
    """
    if isinstance(value, dict):
        return {key: mask_uuids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_uuids(item) for item in value]
    if isinstance(value, str) and value.startswith("urn:uuid:"):
        return "urn:uuid:<masked>"
    return value


class TestConvert:
    async def test_it_returns_the_assembled_bundle_without_validation(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = nar2fhir_gateway()
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["bundle"]["resourceType"] == "Bundle"
        assert body["bundle"]["type"] == "collection"
        assert body["validated"] is False
        assert "report" not in body

    async def test_only_the_extraction_call_reaches_the_model(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """Assembly is deterministic; a second call would reintroduce model variance."""
        gateway = nar2fhir_gateway()
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)

        assert len(gateway.complete_calls) == 1

    async def test_it_assembles_typed_elements_from_the_extracted_entities(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = nar2fhir_gateway

        body = (
            await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)
        ).json()

        observation = resource_at(body, "Observation")
        assert observation["code"]["text"] == "heart rate"
        assert observation["code"]["coding"][0]["code"] == "8867-4"
        assert observation["valueQuantity"]["value"] == 72
        assert observation["valueQuantity"]["code"] == "/min"
        assert observation["subject"]["reference"] == body["bundle"]["entry"][0]["fullUrl"]

    async def test_the_same_entities_produce_the_same_bundle_content(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """Entry identifiers are conversion-scoped; everything else must be stable."""
        app.dependency_overrides[get_llm_gateway] = nar2fhir_gateway

        first, second = [
            (await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)).json()
            for _ in range(2)
        ]

        assert first["conversion_id"] != second["conversion_id"]
        assert first["bundle"]["entry"][0]["fullUrl"] != second["bundle"]["entry"][0]["fullUrl"]
        assert mask_uuids(first["bundle"]) == mask_uuids(second["bundle"])
        assert first["assembly"] == second["assembly"]

    async def test_it_reports_what_it_could_not_ground(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = nar2fhir_gateway

        body = (
            await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)
        ).json()

        actions = {note["action"] for note in body["assembly"]}
        assert "inferred" in actions, "Observation.status is required and was not stated"
        assert "wired" in actions, "Observation.subject was not extracted"
        for note in body["assembly"]:
            assert note["entry_index"] < len(body["bundle"]["entry"])

    async def test_an_inferred_element_is_disclosed_by_a_provenance_tag(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = nar2fhir_gateway

        body = (
            await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)
        ).json()

        observation = resource_at(body, "Observation")
        codes = {
            coding["code"]
            for coding in observation["meta"]["tag"]
            if coding["system"] == PROVENANCE_TAG_SYSTEM
        }
        assert codes == {AI_DERIVED, MACHINE_CODED, MACHINE_INFERRED}

        patient = resource_at(body, "Patient")
        patient_codes = {coding["code"] for coding in patient["meta"]["tag"]}
        assert MACHINE_INFERRED not in patient_codes

    async def test_it_returns_generation_metadata(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = nar2fhir_gateway()
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        body = (
            await client.post("/v1/NAR2FHIR", json={"text": "HR 72"}, headers=BYOK_HEADERS)
        ).json()

        assert body["conversion_id"].startswith("cnv_")
        assert body["validated"] is False
        assert body["llm"]["model"] == gateway.model
        assert body["llm"]["usage"] == {
            "completion_tokens": 20,
            "prompt_tokens": 10,
            "total_tokens": 30,
        }
        assert body["llm"]["latency_ms"] == 5
        assert body["llm"]["qualification_tier"] == "silver"

    async def test_a_profiles_field_is_rejected_rather_than_silently_ignored(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """Deterministic assembly validates nothing, so it cannot claim a profile.

        Accepting the field and doing nothing would let a caller believe the Bundle
        had been targeted at their profile. Pass profiles to /v1/validate instead.
        """
        app.dependency_overrides[get_llm_gateway] = nar2fhir_gateway

        response = await client.post(
            "/v1/NAR2FHIR",
            json={"text": "HR 72", "profiles": ["http://example.org/StructureDefinition/x"]},
            headers=BYOK_HEADERS,
        )

        assert response.status_code == 400

    async def test_an_entity_without_an_instance_key_is_a_schema_violation(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(
            resource={
                "entities": [
                    {"resourceType": "Patient", "keyword": "gender", "value": "male"},
                ]
            }
        )
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/NAR2FHIR", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 422

    async def test_it_requires_the_conversions_write_scope(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            tenant_id="ten_x", actor_type="api_key", actor_id="key_x", scopes=frozenset()
        )
        app.dependency_overrides[get_llm_gateway] = nar2fhir_gateway

        response = await client.post("/v1/NAR2FHIR", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 403

    async def test_a_missing_llm_key_is_a_credentials_error(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/NAR2FHIR",
            json={"text": "x"},
            headers={"X-LLM-Model": "openai/gpt-4o-mini"},
        )

        assert response.status_code == 400
        assert (
            response.json()["issue"][0]["details"]["coding"][0]["code"]
            == "llm-credentials-required"
        )

    async def test_an_egress_block_from_the_gateway_surfaces_as_451(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(error=EgressBlockedError("blocked", safe_context={"host": "x"}))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/NAR2FHIR", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 451

    async def test_a_schema_violation_from_the_model_surfaces_as_422(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(error=LlmSchemaViolationError("not an object"))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/NAR2FHIR", json={"text": "x"}, headers=BYOK_HEADERS)

        assert response.status_code == 422

    async def test_the_narrative_is_never_echoed_in_an_error_body(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(error=LlmSchemaViolationError("not an object"))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway
        secret_note = "PATIENT-NAME-DO-NOT-LEAK"

        response = await client.post(
            "/v1/NAR2FHIR", json={"text": secret_note}, headers=BYOK_HEADERS
        )

        assert secret_note not in response.text

    async def test_the_narrative_is_never_echoed_in_an_assembly_note(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """Notes are safe to log, which only holds if they carry no extracted value."""
        secret_note = "PATIENT-NAME-DO-NOT-LEAK"
        gateway = FakeLlmGateway(
            resource={
                "entities": [
                    {
                        "resourceType": "Patient",
                        "instance": "patient-1",
                        "keyword": "birthDate",
                        "value": secret_note,
                    }
                ]
            }
        )
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        body = (await client.post("/v1/NAR2FHIR", json={"text": "x"}, headers=BYOK_HEADERS)).json()

        assert body["assembly"], "expected the unparseable birthDate to be reported"
        for note in body["assembly"]:
            assert secret_note not in str(note)


class TestNarrativeRouteSurface:
    async def test_alternative_narrative_routes_do_not_exist(
        self, client: httpx.AsyncClient
    ) -> None:
        for path in (
            "/v1/convert",
            "/v1/craft",
            "/v1/craft/stream",
            "/fhir/R4/$convert",
            "/fhir/R4/$extract",
            "/v1/llm/probe",
            "/v1/terminology/search",
            "/v1/terminology/validate-code",
            "/v1/terminology/map",
            "/fhir/R4/$validate",
            "/v1/translate/hl7v2",
            "/v1/translate/cda",
            "/v1/translate/tabular",
        ):
            response = await client.post(path, json={})
            assert response.status_code == 404, path
