"""The enforced layer removes declared PHI before every model network call."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fhiratwill import (
    DeclaredIdentifier,
    DeidMode,
    DeidPolicy,
    DeidProfile,
    IdentifierClass,
)
from fhiratwill.deid.core import minimize
from pydantic import SecretStr

from fhirbridge.api.deps import AppServices, get_llm_gateway
from fhirbridge.config import Settings
from fhirbridge.domain.errors import (
    AudioEgressNotPermittedError,
    PhiMinimizationFailedError,
)
from fhirbridge.llm.gateway import LlmGateway
from fhirbridge.llm.invocation import LlmInvocation, SttInvocation
from tests.fakes import FakeLlmGateway

pytestmark = pytest.mark.security

BYOK_HEADERS = {
    "X-LLM-Provider": "openrouter",
    "X-LLM-Model": "openai/gpt-4o-mini",
    "X-LLM-API-Key": "sk-test",
    "X-PHI-Egress-Acknowledged": "true",
}


def enforced(settings: Settings, *, audio: bool = False) -> Settings:
    return settings.model_copy(
        update={
            "deid_mode": DeidMode.ENFORCED,
            "deid_profile": DeidProfile.HIPAA_SAFE_HARBOR,
            "deid_allow_audio_egress": audio,
            "llm_egress_allowlist": [
                "openrouter.ai",
                "generativelanguage.googleapis.com",
            ],
        }
    )


async def test_endpoint_sends_the_fake_gateway_only_minimized_text(
    app: FastAPI,
    client: httpx.AsyncClient,
    services: AppServices,
    settings: Settings,
) -> None:
    services.settings = enforced(settings)
    gateway = FakeLlmGateway(resource={"entities": []})
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    name = "Jane Unique-Smith"

    response = await client.post(
        "/v1/NAR2FHIR",
        headers=BYOK_HEADERS,
        json={
            "text": f"{name} reports chest pain.",
            "known_identifiers": {"names": [name]},
        },
    )

    assert response.status_code == 200
    outgoing_prompt = gateway.complete_calls[0][1]
    assert name not in outgoing_prompt
    assert "[[NAME_" in outgoing_prompt
    assert response.json()["deid"]["detections"]["name"] >= 1


async def test_gateway_leak_sweep_fails_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    import litellm

    called = False

    async def forbidden_call(**kwargs: Any) -> None:
        nonlocal called
        del kwargs
        called = True

    monkeypatch.setattr(litellm, "acompletion", forbidden_call)
    name = "Jane Unique-Smith"
    policy = DeidPolicy(
        mode=DeidMode.ENFORCED,
        profile=DeidProfile.HIPAA_SAFE_HARBOR,
    )
    result = minimize(
        name,
        policy=policy,
        known_identifiers=[DeclaredIdentifier(IdentifierClass.NAME, name)],
    )
    invocation = LlmInvocation(
        provider="openrouter",
        model="openai/gpt-4o-mini",
        api_key=SecretStr("sk-test"),
        phi_egress_acknowledged=True,
    )

    with pytest.raises(PhiMinimizationFailedError):
        await LlmGateway(enforced(settings)).complete_json(
            invocation,
            system_prompt=f"Leaked identifier: {name}",
            user_prompt=result.safe_text,
            minimization=result,
        )

    assert not called


async def test_enforced_mode_blocks_external_audio_before_provider_call(
    settings: Settings,
) -> None:
    invocation = SttInvocation(
        provider="gemini",
        model="gemini-2.5-flash",
        api_key=SecretStr("test-key"),
        phi_egress_acknowledged=True,
    )

    with pytest.raises(AudioEgressNotPermittedError):
        await LlmGateway(enforced(settings)).transcribe(
            invocation,
            audio=b"audio",
            media_format="wav",
        )
