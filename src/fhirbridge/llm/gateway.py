"""The BYOK/BYOM provider gateway (AGENTS.md 7).

This is the harness around the model call, and it is the point of the whole
milestone: the value is not that we can call an LLM, it is that every call is
gated by policy the operator controls and that its output is measured before it
is trusted.

Order of operations for a call:

1. **Authorize** (:meth:`LlmGateway.authorize`) — provider allowed, egress host
   on the allowlist, PHI egress acknowledged, model qualified. All of this is
   pure and runs before any network call, so a blocked request costs nothing and
   leaks nothing.
2. **Budget** — a best-effort pre-flight estimate refuses a call that would
   exceed ``MAX_COST_USD_PER_CONVERSION``; ``max_tokens`` bounds the worst case.
3. **Call** — litellm is imported lazily (it is heavy and unused by validate-only
   deployments) and provider exceptions are mapped onto the stable error
   catalogue.
4. **Parse** — the completion is required to be a single JSON object; anything
   else is ``llm-schema-violation``, not a silently-accepted string.

Prompt and completion content is never logged unless ``DEBUG_CAPTURE_LLM_IO`` is
set, which production forbids (principle 2.6).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Protocol

from fhiratwill import DeidMode
from fhiratwill.conversion.prompts import DICTATION_TRANSCRIBE
from fhiratwill.deid.core import Minimization
from fhiratwill.deid.errors import (
    PhiMinimizationFailedError as CorePhiMinimizationFailedError,
)

from fhirbridge.config import QualificationTier, Settings
from fhirbridge.domain.errors import (
    AudioEgressNotPermittedError,
    BudgetExceededError,
    EgressBlockedError,
    FhirbridgeError,
    LlmAuthFailedError,
    LlmContentFilteredError,
    LlmContextExceededError,
    LlmQuotaExhaustedError,
    LlmRateLimitedError,
    LlmSchemaViolationError,
    ModelNotQualifiedError,
    PhiEgressNotAcknowledgedError,
    PhiMinimizationFailedError,
    PhiMinimizationRequiredError,
    UnreadableDocumentError,
)
from fhirbridge.llm.invocation import LlmInvocation, SttInvocation
from fhirbridge.llm.qualification import resolve_tier

logger = logging.getLogger(__name__)

_LOOPBACK: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

DEFAULT_MAX_TOKENS: Final[int] = 4096
"""Bounds the worst-case cost of a single completion when pricing is unknown."""

DICTATION_MAX_TOKENS: Final[int] = 8192
"""A transcript can be long; give dictation more room than a JSON extraction."""

_LLM_CALL_TIMEOUT_S: Final[float] = 120.0


class _ProviderCall(Protocol):
    """The shape both the LLM and the dictation invocation share.

    The transport helpers are written against this rather than a concrete type so
    one code path drives both calls without either invocation type standing in for
    the other where policy cares about the difference. Every member is read-only so
    the frozen invocation dataclasses satisfy it.
    """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def base_url(self) -> str | None: ...

    @property
    def extra_headers(self) -> dict[str, str]: ...

    @property
    def api_key(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class LlmResult:
    """The outcome of a JSON completion."""

    resource: dict[str, Any]
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    latency_ms: int = 0
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DictationResult:
    """The outcome of a speech-to-text call.

    ``text`` is the transcript and is therefore PHI: it may be returned in a
    response body but must never reach a log, metric, exception, or trace.
    """

    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    latency_ms: int = 0


@dataclass(slots=True)
class LlmGateway:
    """Policy-enforcing entry point for every LLM call."""

    settings: Settings

    # --- Policy (pure, pre-network) ---------------------------------------

    def authorize(
        self,
        invocation: LlmInvocation,
        *,
        sending_phi: bool,
        minimization: Minimization | None = None,
    ) -> QualificationTier:
        """Enforce every policy gate for a completion, returning the resolved tier.

        Raises the specific catalogue error for the first gate that fails.
        """
        self._authorize_egress(
            provider=invocation.provider,
            host=invocation.egress_host,
            sending_phi=sending_phi,
            phi_acknowledged=invocation.phi_egress_acknowledged,
        )
        if self.settings.deid_mode is DeidMode.ENFORCED and (
            minimization is None or not minimization.applied
        ):
            raise PhiMinimizationRequiredError()

        tier = resolve_tier(invocation.model)
        if not tier.satisfies(self.settings.min_qualification_tier):
            raise ModelNotQualifiedError(
                "This model is below the configured minimum qualification tier. Qualify "
                "it, choose a qualified model, or lower MIN_QUALIFICATION_TIER.",
                safe_context={
                    "model": invocation.model,
                    "tier": str(tier),
                    "required": str(self.settings.min_qualification_tier),
                },
            )
        return tier

    def _authorize_egress(
        self, *, provider: str, host: str, sending_phi: bool, phi_acknowledged: bool
    ) -> None:
        """The provider, egress-host, and PHI gates shared by every external call.

        Deliberately excludes the qualification tier: that gate protects the model
        that *interprets* clinical meaning. Transcription is a verbatim capture step,
        and the tier registry does not rank speech-to-text models, so applying it
        would reject every usable dictation model rather than express a real policy.
        """
        if not self.settings.provider_allowed(provider):
            raise EgressBlockedError(
                "This provider is not permitted by server policy.",
                safe_context={"provider": provider},
            )

        self._check_egress(host)

        if (
            sending_phi
            and self.settings.require_phi_egress_ack
            and host not in _LOOPBACK
            and not phi_acknowledged
        ):
            raise PhiEgressNotAcknowledgedError(
                "Set X-PHI-Egress-Acknowledged: true to send clinical content to an "
                "external provider.",
                safe_context={"host": host},
            )

    def _check_egress(self, host: str) -> None:
        if self.settings.local_only_mode:
            if host not in _LOOPBACK:
                raise EgressBlockedError(
                    "LOCAL_ONLY_MODE is on; only loopback LLM endpoints are permitted.",
                    safe_context={"host": host},
                )
            return
        allowed = {_normalize_host(entry) for entry in self.settings.llm_egress_allowlist}
        if not host or host not in allowed:
            raise EgressBlockedError(
                "The target LLM host is not in LLM_EGRESS_ALLOWLIST.",
                safe_context={"host": host},
            )

    # --- Completions ------------------------------------------------------

    async def complete_json(
        self,
        invocation: LlmInvocation,
        *,
        system_prompt: str,
        user_prompt: str,
        minimization: Minimization,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmResult:
        """Authorize, call the provider, and parse a single JSON object back."""
        self.authorize(invocation, sending_phi=True, minimization=minimization)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            minimization.assert_safe_payload(messages)
        except CorePhiMinimizationFailedError as exc:
            raise PhiMinimizationFailedError() from exc
        self._enforce_budget(invocation, messages, max_tokens)

        response, latency_ms = await self._acompletion(
            invocation, messages, max_tokens=max_tokens, json_mode=True
        )
        content = _first_content(response)
        resource = _extract_json_object(content)
        return LlmResult(
            resource=resource,
            model=_response_model(response, invocation),
            usage=_usage(response),
            cost_usd=_completion_cost(response),
            latency_ms=latency_ms,
            finish_reason=_finish_reason(response),
        )

    # --- Transcription ----------------------------------------------------

    async def transcribe(
        self,
        invocation: SttInvocation,
        *,
        audio: bytes,
        media_format: str,
    ) -> DictationResult:
        """Authorize and transcribe audio to plain text via a multimodal call.

        Gemini and most current speech-to-text models take audio as an
        ``input_audio`` content block on a chat completion rather than a Whisper-style
        transcription endpoint, so the audio rides in the message as raw base64.

        No token-budget pre-flight runs here: audio is priced by duration, not
        tokens, and litellm's token counter raises on ``input_audio`` blocks. The
        completion cost is still recorded from the response.
        """
        self._authorize_egress(
            provider=invocation.provider,
            host=invocation.egress_host,
            sending_phi=True,
            phi_acknowledged=invocation.phi_egress_acknowledged,
        )
        if (
            self.settings.deid_mode is DeidMode.ENFORCED
            and invocation.egress_host not in _LOOPBACK
            and not self.settings.deid_allow_audio_egress
        ):
            raise AudioEgressNotPermittedError()
        encoded = base64.b64encode(audio).decode("ascii")
        audio_block: dict[str, Any] = {
            "type": "input_audio",
            "input_audio": {"data": encoded, "format": media_format},
        }
        user_content: list[dict[str, Any]] = [audio_block]
        if invocation.language:
            user_content.insert(
                0,
                {"type": "text", "text": f"The spoken language is {invocation.language}."},
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": DICTATION_TRANSCRIBE.system},
            {"role": "user", "content": user_content},
        ]

        response, latency_ms = await self._acompletion(
            invocation, messages, max_tokens=DICTATION_MAX_TOKENS, json_mode=False
        )
        text = _first_content(response).strip()
        if not text:
            raise UnreadableDocumentError(
                "The dictation model returned no transcribable speech from the audio."
            )
        return DictationResult(
            text=text,
            model=_response_model(response, invocation),
            usage=_usage(response),
            cost_usd=_completion_cost(response),
            latency_ms=latency_ms,
        )

    # --- Provider transport (lazy litellm) --------------------------------

    async def _acompletion(
        self,
        invocation: _ProviderCall,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        json_mode: bool,
    ) -> tuple[Any, int]:
        import litellm  # lazy: heavy import, and validate-only deployments never call an LLM

        kwargs = self._call_kwargs(
            invocation,
            messages,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        if self.settings.debug_capture_llm_io:
            logger.warning("llm_request_captured", extra={"messages": messages})

        started = time.perf_counter()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            # Broad by necessity: litellm raises a wide family of provider errors.
            # Known ones map onto the catalogue; anything else is re-raised and
            # surfaces as a generic 500 rather than a wrong, confident code.
            mapped = _map_llm_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        return response, latency_ms

    def _call_kwargs(
        self,
        invocation: _ProviderCall,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": _litellm_model(invocation),
            "messages": messages,
            "api_key": invocation.api_key.get_secret_value(),
            "temperature": 0,
            "max_tokens": max_tokens,
            "timeout": _LLM_CALL_TIMEOUT_S,
        }
        if invocation.base_url:
            kwargs["api_base"] = invocation.base_url
        if invocation.extra_headers:
            kwargs["extra_headers"] = dict(invocation.extra_headers)
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _enforce_budget(
        self, invocation: LlmInvocation, messages: list[dict[str, str]], max_tokens: int
    ) -> None:
        estimate = self._estimate_max_cost(invocation, messages, max_tokens)
        if estimate is None:
            return
        cap = self.settings.max_cost_usd_per_conversion
        if estimate > cap:
            raise BudgetExceededError(
                "The estimated worst-case cost of this call exceeds MAX_COST_USD_PER_CONVERSION.",
                safe_context={"estimate_usd": float(estimate), "cap_usd": float(cap)},
            )

    def _estimate_max_cost(
        self, invocation: LlmInvocation, messages: list[dict[str, str]], max_tokens: int
    ) -> Decimal | None:
        """Best-effort worst-case cost, or ``None`` when pricing is unknown.

        Returning ``None`` deliberately does not block the call: an unknown price
        is not evidence of an over-budget one, and ``max_tokens`` still bounds the
        exposure. The post-call actual cost is always recorded on the result.
        """
        try:
            import litellm

            model = _litellm_model(invocation)
            prompt_tokens = int(litellm.token_counter(model=model, messages=messages))
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model, prompt_tokens=prompt_tokens, completion_tokens=max_tokens
            )
        except Exception:
            # Pricing and token counting are advisory; an unknown price is not
            # evidence of an over-budget one, so never fail the call on it.
            return None
        total = (prompt_cost or 0.0) + (completion_cost or 0.0)
        if total <= 0:
            return None
        return Decimal(str(total))


def _litellm_model(invocation: _ProviderCall) -> str:
    """The model id litellm expects.

    OpenRouter is addressed through litellm's ``openrouter/`` prefix, and Google AI
    Studio through the ``gemini/`` prefix; for any other provider the caller is
    expected to pass a litellm-compatible model id (for example
    ``anthropic/claude-3.5-sonnet``, ``gpt-4o``, or ``groq/whisper-large-v3``).
    """
    model = invocation.model
    if invocation.provider == "openrouter" and not model.startswith("openrouter/"):
        return f"openrouter/{model}"
    if invocation.provider == "gemini" and not model.startswith(("gemini/", "vertex_ai/")):
        return f"gemini/{model}"
    return model


def _normalize_host(entry: str) -> str:
    from urllib.parse import urlparse

    text = entry.strip().lower()
    if not text:
        return ""
    if "://" in text:
        return (urlparse(text).hostname or "").lower()
    return text.split("/", 1)[0].split(":", 1)[0]


def _first_content(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def _finish_reason(response: Any) -> str | None:
    try:
        reason = response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    return reason if isinstance(reason, str) else None


def _response_model(response: Any, invocation: _ProviderCall) -> str:
    model = getattr(response, "model", None)
    return model if isinstance(model, str) and model else invocation.model


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


def _completion_cost(response: Any) -> Decimal | None:
    try:
        import litellm

        # Typed as float upstream, but treated as optional here: litellm has
        # returned None for unpriced models across versions, and a wrong non-None
        # is worse than a missing cost.
        cost: Any = litellm.completion_cost(completion_response=response)
    except Exception:
        # Cost is advisory provenance metadata, never fatal to the response.
        return None
    if cost is None:
        return None
    try:
        return Decimal(str(cost))
    except (ValueError, ArithmeticError):
        return None


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse a single JSON object out of a completion, tolerating a code fence."""
    text = _strip_code_fence(content.strip())
    if not text:
        raise LlmSchemaViolationError("The model returned an empty completion.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmSchemaViolationError("The model did not return a valid JSON object.") from exc
    if not isinstance(parsed, dict):
        raise LlmSchemaViolationError("The model returned JSON that was not an object.")
    return parsed


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    # Drop the opening ``` (with optional language) and a closing fence if present.
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _map_llm_exception(exc: Exception) -> FhirbridgeError | None:
    """Map a litellm exception onto the stable catalogue, or ``None`` to re-raise.

    Matched by class name so litellm is not imported here (this runs in the
    ``except`` of the lazy-import call site). Returning ``None`` lets a genuinely
    unexpected error surface as a generic 500 rather than a wrong, confident code.
    """
    name = type(exc).__name__
    text = str(exc).lower()
    match name:
        case "AuthenticationError" | "PermissionDeniedError":
            return LlmAuthFailedError("The LLM provider rejected the supplied API key.")
        case "RateLimitError":
            if any(marker in text for marker in ("quota", "insufficient_quota", "billing")):
                return LlmQuotaExhaustedError(
                    "The LLM provider reports the account quota is exhausted."
                )
            return LlmRateLimitedError(
                "The LLM provider rate-limited this request.", retry_after_s=5
            )
        case "ContextWindowExceededError":
            return LlmContextExceededError("The input exceeds the model's context window.")
        case "ContentPolicyViolationError":
            return LlmContentFilteredError(
                "The LLM provider's content filter blocked this request."
            )
        case "Timeout" | "APIConnectionError" | "ServiceUnavailableError" | "InternalServerError":
            # A transient upstream failure. The only retryable LLM code is
            # rate-limited, so it carries the retry signal even though the cause
            # differs; the message says so plainly.
            return LlmRateLimitedError(
                "The LLM provider is temporarily unavailable; retry shortly.",
                retry_after_s=5,
            )
        case _:
            return None


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DICTATION_MAX_TOKENS",
    "DictationResult",
    "LlmGateway",
    "LlmResult",
]
