"""``POST /v1/VOICE2FHIR``.

The gateway is scripted (``FakeLlmGateway``): the point is the endpoint's own
behaviour -- that it transcribes first, feeds the transcript through the same
deterministic pipeline as ``NAR2FHIR``, returns the transcript for review, and
guards the audio -- not how either provider call was made over the wire.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from fhirbridge.api.auth import Principal
from fhirbridge.api.deps import AppServices, get_llm_gateway, get_principal
from fhirbridge.domain.errors import EgressBlockedError, LlmSchemaViolationError
from tests.fakes import FakeLlmGateway

EXTRACTED = {
    "entities": [
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

VOICE_HEADERS = {
    "X-LLM-Provider": "openrouter",
    "X-LLM-Model": "openai/gpt-4o-mini",
    "X-LLM-API-Key": "sk-test",
    "X-STT-Provider": "gemini",
    "X-STT-Model": "gemini-2.5-flash",
    "X-STT-API-Key": "gk-test",
    "X-PHI-Egress-Acknowledged": "true",
}

WAV = ("dictation.wav", b"RIFF....audio-bytes....", "audio/wav")


def voice_gateway() -> FakeLlmGateway:
    return FakeLlmGateway(resource=EXTRACTED, transcript="Heart rate seventy two.")


@pytest.fixture(autouse=True)
def _binding_terminology(terminology_valid: object) -> None:
    del terminology_valid


class TestVoiceConvert:
    async def test_it_transcribes_then_returns_the_assembled_bundle(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway

        response = await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["bundle"]["resourceType"] == "Bundle"
        assert body["conversion_id"].startswith("cnv_")
        assert body["validated"] is False

    async def test_it_returns_the_transcript_and_its_provenance(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = voice_gateway()
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        body = (
            await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)
        ).json()

        assert body["transcript"] == "Heart rate seventy two."
        assert body["transcription"]["provider"] == "gemini"
        assert body["transcription"]["model"] == gateway.stt_model
        assert body["transcription"]["latency_ms"] == 7
        assert "qualification_tier" not in body["transcription"]

    async def test_one_transcription_call_precedes_one_extraction_call(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = voice_gateway()
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)

        assert gateway.transcribe_calls == [("wav", len(WAV[1]))]
        assert len(gateway.complete_calls) == 1
        # The transcript, not the audio, is what extraction sees.
        assert "Heart rate seventy two." in gateway.complete_calls[0][1]

    async def test_the_transcript_flows_into_the_assembled_bundle(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway

        body = (
            await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)
        ).json()

        observation = next(
            entry["resource"]
            for entry in body["bundle"]["entry"]
            if entry["resource"]["resourceType"] == "Observation"
        )
        assert observation["valueQuantity"]["value"] == 72
        assert observation["valueQuantity"]["code"] == "/min"
        assert body["assembly"], "required elements the audio did not state should be reported"

    async def test_a_content_type_free_upload_falls_back_to_the_extension(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway

        response = await client.post(
            "/v1/VOICE2FHIR",
            files={"audio": ("dictation.mp3", b"id3-audio", "application/octet-stream")},
            headers=VOICE_HEADERS,
        )

        assert response.status_code == 200

    async def test_a_non_audio_upload_is_unsupported_media_type(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway

        response = await client.post(
            "/v1/VOICE2FHIR",
            files={"audio": ("notes.txt", b"just text", "text/plain")},
            headers=VOICE_HEADERS,
        )

        assert response.status_code == 415

    async def test_empty_audio_is_rejected(self, app: FastAPI, client: httpx.AsyncClient) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway

        response = await client.post(
            "/v1/VOICE2FHIR",
            files={"audio": ("dictation.wav", b"", "audio/wav")},
            headers=VOICE_HEADERS,
        )

        assert response.status_code == 422

    async def test_audio_larger_than_the_upload_cap_is_rejected(
        self, app: FastAPI, client: httpx.AsyncClient, services: AppServices
    ) -> None:
        # Shrink the cap rather than uploading megabytes; the guard is what matters.
        app.dependency_overrides[get_llm_gateway] = voice_gateway
        services.settings = services.settings.model_copy(update={"max_upload_bytes": 8})

        response = await client.post(
            "/v1/VOICE2FHIR",
            files={"audio": ("dictation.wav", b"0123456789ABCDEF", "audio/wav")},
            headers=VOICE_HEADERS,
        )

        assert response.status_code == 413

    async def test_a_missing_stt_key_is_a_credentials_error(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway
        headers = {key: value for key, value in VOICE_HEADERS.items() if key != "X-STT-API-Key"}

        response = await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=headers)

        assert response.status_code == 400
        assert (
            response.json()["issue"][0]["details"]["coding"][0]["code"]
            == "llm-credentials-required"
        )

    async def test_a_missing_llm_key_is_a_credentials_error(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_llm_gateway] = voice_gateway
        headers = {key: value for key, value in VOICE_HEADERS.items() if key != "X-LLM-API-Key"}

        response = await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=headers)

        assert response.status_code == 400

    async def test_a_transcription_egress_block_surfaces_as_451(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(
            resource=EXTRACTED,
            transcribe_error=EgressBlockedError("blocked", safe_context={"host": "x"}),
        )
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)

        assert response.status_code == 451
        assert gateway.complete_calls == [], "extraction must not run on a failed transcription"

    async def test_a_downstream_extraction_failure_surfaces_after_a_good_transcription(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        gateway = FakeLlmGateway(resource=EXTRACTED, error=LlmSchemaViolationError("not an object"))
        app.dependency_overrides[get_llm_gateway] = lambda: gateway

        response = await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)

        assert response.status_code == 422
        assert len(gateway.transcribe_calls) == 1

    async def test_it_requires_the_conversions_write_scope(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            tenant_id="ten_x", actor_type="api_key", actor_id="key_x", scopes=frozenset()
        )
        app.dependency_overrides[get_llm_gateway] = voice_gateway

        response = await client.post("/v1/VOICE2FHIR", files={"audio": WAV}, headers=VOICE_HEADERS)

        assert response.status_code == 403
