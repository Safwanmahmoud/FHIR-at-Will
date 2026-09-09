"""The gateway's policy gates, exception mapping and output parsing.

These are the safety-relevant parts of the harness and they are all pure: no
network, no litellm. The provider call itself is exercised end-to-end through the
router tests with a scripted gateway.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from fhiratwill.conversion.prompts import DICTATION_TRANSCRIBE
from pydantic import SecretStr

from fhirbridge.config import QualificationTier, Settings
from fhirbridge.domain.errors import (
    EgressBlockedError,
    LlmAuthFailedError,
    LlmContentFilteredError,
    LlmContextExceededError,
    LlmQuotaExhaustedError,
    LlmRateLimitedError,
    LlmSchemaViolationError,
    ModelNotQualifiedError,
    PhiEgressNotAcknowledgedError,
    UnreadableDocumentError,
)
from fhirbridge.llm.gateway import (
    LlmGateway,
    _extract_json_object,
    _litellm_model,
    _map_llm_exception,
)
from fhirbridge.llm.invocation import LlmInvocation, SttInvocation


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "FHIRBRIDGE_ENV": "development",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "VALIDATOR_URL": "http://validator.test",
        "TERMINOLOGY_URL": "http://terminology.test",
        "LLM_EGRESS_ALLOWLIST": "openrouter.ai",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _inv(
    *,
    model: str = "openai/gpt-4o-mini",
    provider: str = "openrouter",
    base_url: str | None = None,
    ack: bool = True,
) -> LlmInvocation:
    return LlmInvocation(
        provider=provider,
        model=model,
        api_key=SecretStr("sk-test"),
        base_url=base_url,
        phi_egress_acknowledged=ack,
    )


class TestAuthorize:
    def test_a_permitted_qualified_acknowledged_call_returns_its_tier(self) -> None:
        gateway = LlmGateway(settings=_settings())

        tier = gateway.authorize(_inv(), sending_phi=True)

        assert tier is QualificationTier.SILVER

    def test_a_disallowed_provider_is_blocked(self) -> None:
        gateway = LlmGateway(settings=_settings(LLM_ALLOWED_PROVIDERS="anthropic"))

        with pytest.raises(EgressBlockedError):
            gateway.authorize(_inv(provider="openrouter"), sending_phi=True)

    def test_a_host_off_the_allowlist_is_blocked(self) -> None:
        gateway = LlmGateway(settings=_settings(LLM_EGRESS_ALLOWLIST=""))

        with pytest.raises(EgressBlockedError):
            gateway.authorize(_inv(), sending_phi=True)

    def test_local_only_mode_blocks_external_hosts(self) -> None:
        gateway = LlmGateway(settings=_settings(LOCAL_ONLY_MODE=True))

        with pytest.raises(EgressBlockedError):
            gateway.authorize(_inv(), sending_phi=True)

    def test_local_only_mode_permits_loopback(self) -> None:
        gateway = LlmGateway(settings=_settings(LOCAL_ONLY_MODE=True))

        tier = gateway.authorize(
            _inv(provider="ollama", base_url="http://localhost:11434/v1", model="openai/gpt-4o"),
            sending_phi=True,
        )

        assert tier is QualificationTier.GOLD

    def test_unacknowledged_phi_egress_is_refused(self) -> None:
        gateway = LlmGateway(settings=_settings())

        with pytest.raises(PhiEgressNotAcknowledgedError):
            gateway.authorize(_inv(ack=False), sending_phi=True)

    def test_a_probe_does_not_require_the_phi_acknowledgement(self) -> None:
        """A probe sends no clinical content, so the ack gate does not apply."""
        gateway = LlmGateway(settings=_settings())

        tier = gateway.authorize(_inv(ack=False), sending_phi=False)

        assert tier is QualificationTier.SILVER

    def test_a_model_below_the_minimum_tier_is_refused(self) -> None:
        gateway = LlmGateway(settings=_settings(MIN_QUALIFICATION_TIER="gold"))

        with pytest.raises(ModelNotQualifiedError):
            gateway.authorize(_inv(model="openai/gpt-4o-mini"), sending_phi=True)

    def test_an_unknown_model_is_unqualified_and_refused_by_default(self) -> None:
        gateway = LlmGateway(settings=_settings())

        with pytest.raises(ModelNotQualifiedError):
            gateway.authorize(_inv(model="some/unknown-model"), sending_phi=True)

    def test_lowering_the_tier_lets_an_unknown_model_through(self) -> None:
        gateway = LlmGateway(settings=_settings(MIN_QUALIFICATION_TIER="unqualified"))

        tier = gateway.authorize(_inv(model="some/unknown-model"), sending_phi=True)

        assert tier is QualificationTier.UNQUALIFIED


def _stt(
    *,
    model: str = "gemini-2.5-flash",
    provider: str = "gemini",
    base_url: str | None = None,
    ack: bool = True,
    language: str | None = None,
) -> SttInvocation:
    return SttInvocation(
        provider=provider,
        model=model,
        api_key=SecretStr("gk-test"),
        base_url=base_url,
        phi_egress_acknowledged=ack,
        language=language,
    )


def _stt_settings(**overrides: object) -> Settings:
    return _settings(LLM_EGRESS_ALLOWLIST="generativelanguage.googleapis.com", **overrides)


def _reply(content: str) -> SimpleNamespace:
    """The minimal completion-response shape the gateway reads content from."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="gemini/gemini-2.5-flash", usage=None)


def _patch_acompletion(
    monkeypatch: pytest.MonkeyPatch, content: str, captured: dict[str, Any]
) -> None:
    """Stand in for the litellm call so transcribe runs offline.

    Patching the litellm entry point rather than the gateway keeps the real
    kwargs-building path (:meth:`_call_kwargs`) under test; the gateway is a slotted
    dataclass, so its bound methods cannot be patched anyway.
    """
    import litellm

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _reply(content)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)


class TestTranscribe:
    async def test_it_transcribes_and_builds_an_input_audio_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gateway = LlmGateway(settings=_stt_settings())
        captured: dict[str, Any] = {}
        _patch_acompletion(monkeypatch, "Patient denies chest pain.", captured)

        result = await gateway.transcribe(_stt(), audio=b"AUDIO-BYTES", media_format="wav")

        assert result.text == "Patient denies chest pain."
        assert result.model == "gemini/gemini-2.5-flash"
        assert "response_format" not in captured, "dictation is plain text, not JSON mode"
        assert captured["model"] == "gemini/gemini-2.5-flash"
        system, user = captured["messages"]
        assert system == {"role": "system", "content": DICTATION_TRANSCRIBE.system}
        audio_block = user["content"][-1]
        assert audio_block["type"] == "input_audio"
        assert audio_block["input_audio"]["format"] == "wav"
        expected_data = base64.b64encode(b"AUDIO-BYTES").decode("ascii")
        assert audio_block["input_audio"]["data"] == expected_data

    async def test_a_language_hint_is_passed_as_leading_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gateway = LlmGateway(settings=_stt_settings())
        captured: dict[str, Any] = {}
        _patch_acompletion(monkeypatch, "bonjour", captured)

        await gateway.transcribe(_stt(language="fr"), audio=b"x", media_format="mp3")

        first_part = captured["messages"][1]["content"][0]
        assert first_part["type"] == "text"
        assert "fr" in first_part["text"]

    async def test_empty_speech_is_an_unreadable_document(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gateway = LlmGateway(settings=_stt_settings())
        _patch_acompletion(monkeypatch, "   ", {})

        with pytest.raises(UnreadableDocumentError):
            await gateway.transcribe(_stt(), audio=b"x", media_format="wav")

    async def test_it_does_not_apply_the_qualification_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A speech-to-text model is not tier-ranked; the tier gate must not reject it."""
        gateway = LlmGateway(settings=_stt_settings(MIN_QUALIFICATION_TIER="gold"))
        _patch_acompletion(monkeypatch, "ok", {})

        result = await gateway.transcribe(
            _stt(model="whisper-does-not-rank"), audio=b"x", media_format="wav"
        )

        assert result.text == "ok"

    async def test_a_host_off_the_allowlist_is_blocked(self) -> None:
        gateway = LlmGateway(settings=_settings(LLM_EGRESS_ALLOWLIST="openrouter.ai"))

        with pytest.raises(EgressBlockedError):
            await gateway.transcribe(_stt(), audio=b"x", media_format="wav")

    async def test_unacknowledged_phi_egress_is_refused(self) -> None:
        gateway = LlmGateway(settings=_stt_settings())

        with pytest.raises(PhiEgressNotAcknowledgedError):
            await gateway.transcribe(_stt(ack=False), audio=b"x", media_format="wav")


class TestLitellmModelId:
    def test_gemini_gets_the_gemini_prefix(self) -> None:
        assert _litellm_model(_stt(model="gemini-2.5-flash")) == "gemini/gemini-2.5-flash"

    def test_an_already_prefixed_gemini_model_is_left_alone(self) -> None:
        assert _litellm_model(_stt(model="gemini/gemini-2.5-pro")) == "gemini/gemini-2.5-pro"

    def test_openrouter_still_gets_its_prefix(self) -> None:
        assert _litellm_model(_inv(model="openai/gpt-4o-mini")) == "openrouter/openai/gpt-4o-mini"


class TestExceptionMapping:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("AuthenticationError", LlmAuthFailedError),
            ("PermissionDeniedError", LlmAuthFailedError),
            ("ContextWindowExceededError", LlmContextExceededError),
            ("ContentPolicyViolationError", LlmContentFilteredError),
            ("Timeout", LlmRateLimitedError),
            ("APIConnectionError", LlmRateLimitedError),
            ("ServiceUnavailableError", LlmRateLimitedError),
        ],
    )
    def test_known_provider_errors_map_to_the_catalogue(
        self, name: str, expected: type[Exception]
    ) -> None:
        exc = type(name, (Exception,), {})("boom")

        mapped = _map_llm_exception(exc)

        assert isinstance(mapped, expected)

    def test_a_plain_rate_limit_is_rate_limited(self) -> None:
        exc = type("RateLimitError", (Exception,), {})("slow down")

        mapped = _map_llm_exception(exc)

        assert isinstance(mapped, LlmRateLimitedError)
        assert mapped.retry_after_s is not None

    def test_a_quota_rate_limit_is_reported_as_quota_exhausted(self) -> None:
        exc = type("RateLimitError", (Exception,), {})("insufficient_quota: add billing")

        assert isinstance(_map_llm_exception(exc), LlmQuotaExhaustedError)

    def test_an_unrecognized_error_is_left_to_surface_generically(self) -> None:
        assert _map_llm_exception(ValueError("who knows")) is None


class TestJsonExtraction:
    def test_a_bare_object_parses(self) -> None:
        assert _extract_json_object('{"resourceType": "Bundle"}') == {"resourceType": "Bundle"}

    def test_a_fenced_object_parses(self) -> None:
        fenced = '```json\n{"resourceType": "Bundle"}\n```'

        assert _extract_json_object(fenced) == {"resourceType": "Bundle"}

    def test_invalid_json_is_a_schema_violation(self) -> None:
        with pytest.raises(LlmSchemaViolationError):
            _extract_json_object("not json at all")

    def test_a_non_object_is_a_schema_violation(self) -> None:
        with pytest.raises(LlmSchemaViolationError):
            _extract_json_object("[1, 2, 3]")

    def test_empty_output_is_a_schema_violation(self) -> None:
        with pytest.raises(LlmSchemaViolationError):
            _extract_json_object("   ")
