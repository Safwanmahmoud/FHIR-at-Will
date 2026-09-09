"""Scripted doubles for the dependencies the cascade talks to.

These exist for the *layer* unit tests, where the point is what the layer does
with an answer rather than how the answer was fetched. The transport itself is
covered separately by the ``respx`` tests, which drive the real clients.

The doubles record every call so a test can assert the questions that were
asked, not only the conclusion drawn. "Did L3 actually check the ValueSet
membership?" is a different question from "did L3 report a pass?", and only the
first one catches a layer that quietly stopped checking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fhiratwill import (
    Coding,
    ExpansionResult,
    LookupResult,
    SubsumesResult,
    SubsumptionOutcome,
    TerminologyHealth,
    TranslateResult,
    ValidateCodeResult,
)
from fhiratwill.deid.core import Minimization

from fhirbridge.domain.errors import (
    DomainError,
    ErrorCode,
    TerminologyUnavailableError,
    ValidatorUnavailableError,
)
from fhirbridge.fhir.validator_client import (
    FhirPathOutcome,
    ValidatorIssue,
    ValidatorOutcome,
)
from fhirbridge.llm.gateway import DictationResult, LlmResult
from fhirbridge.llm.invocation import LlmInvocation, SttInvocation


@dataclass(frozen=True, slots=True)
class ValidateCodeCall:
    """One question asked of the terminology server."""

    system: str | None
    code: str
    value_set: str | None
    display: str | None = None


@dataclass
class FakeTerminologyClient:
    """A terminology server whose answers are scripted per code.

    ``answers`` maps ``code`` to the outcome of the CodeSystem check;
    ``membership`` maps ``code`` to the outcome of a ValueSet-scoped check.
    Keeping them separate matters: "this code does not exist" and "this code
    exists but is not in the bound ValueSet" are different findings at different
    severities, and a double that cannot express one without the other would
    make that distinction untestable. Anything not listed is confirmed.
    """

    answers: dict[str, bool] = field(default_factory=dict)
    membership: dict[str, bool] = field(default_factory=dict)
    unknown_value_sets: set[str] = field(default_factory=set)
    unknown_systems: set[str] = field(default_factory=set)
    displays: dict[str, str] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    unavailable: bool = False
    calls: list[ValidateCodeCall] = field(default_factory=list)

    async def validate_code(
        self,
        *,
        system: str | None,
        code: str,
        display: str | None = None,
        version: str | None = None,
        value_set: str | None = None,
    ) -> ValidateCodeResult:
        del version
        self.calls.append(ValidateCodeCall(system, code, value_set, display))

        if self.unavailable:
            raise TerminologyUnavailableError("scripted outage")
        if value_set is not None and value_set in self.unknown_value_sets:
            raise DomainError("unknown ValueSet", code=ErrorCode.UNKNOWN_VALUE_SET)
        if system is not None and system in self.unknown_systems:
            raise DomainError("unknown CodeSystem", code=ErrorCode.UNKNOWN_VALUE_SET)

        result = (
            self.membership.get(code, True)
            if value_set is not None
            else self.answers.get(code, True)
        )
        return ValidateCodeResult(
            result=result,
            coding=Coding(system=system, code=code, display=display),
            value_set=value_set,
            display=self.displays.get(code),
            message=self.messages.get(code),
        )

    async def lookup(self, *, system: str, code: str, version: str | None = None) -> LookupResult:
        return LookupResult(
            coding=Coding(system=system, code=code, display=self.displays.get(code)),
            display=self.displays.get(code),
            code_system_version=version,
        )

    async def expand(
        self,
        *,
        value_set: str,
        filter_text: str | None = None,
        count: int | None = None,
        offset: int | None = None,
    ) -> ExpansionResult:
        del filter_text, count, offset
        return ExpansionResult(value_set=value_set, contains=())

    async def subsumes(
        self, *, system: str, code_a: str, code_b: str, version: str | None = None
    ) -> SubsumesResult:
        return SubsumesResult(
            outcome=SubsumptionOutcome.NOT_SUBSUMED,
            left=Coding(system=system, code=code_a, version=version),
            right=Coding(system=system, code=code_b, version=version),
        )

    async def translate(
        self,
        *,
        system: str,
        code: str,
        target_system: str | None = None,
        concept_map: str | None = None,
    ) -> TranslateResult:
        del system, code, target_system, concept_map
        return TranslateResult(result=False)

    async def health(self, *, code_systems: Sequence[str] = ()) -> TerminologyHealth:
        del code_systems
        return TerminologyHealth(reachable=not self.unavailable)

    async def aclose(self) -> None:
        return None

    # --- Assertions helpers ----------------------------------------------

    def codes_checked(self) -> set[str]:
        return {call.code for call in self.calls}

    def value_sets_checked(self) -> set[str]:
        return {call.value_set for call in self.calls if call.value_set}


@dataclass
class FakeValidatorClient:
    """A validator sidecar with scripted issues and FHIRPath answers."""

    issues: tuple[ValidatorIssue, ...] = ()
    fhirpath_results: dict[str, Any] = field(default_factory=dict)
    default_fhirpath: Any = True
    unavailable: bool = False
    validate_calls: list[tuple[dict[str, Any], tuple[str, ...]]] = field(default_factory=list)
    fhirpath_calls: list[str] = field(default_factory=list)

    async def validate_resource(
        self,
        resource: dict[str, Any],
        *,
        profiles: Sequence[str] = (),
        best_practice: str | None = None,
    ) -> ValidatorOutcome:
        del best_practice
        self.validate_calls.append((resource, tuple(profiles)))
        if self.unavailable:
            raise ValidatorUnavailableError("scripted outage")
        return ValidatorOutcome(issues=self.issues, profiles=tuple(profiles), duration_ms=1)

    async def evaluate_fhirpath(self, resource: dict[str, Any], expression: str) -> FhirPathOutcome:
        del resource
        self.fhirpath_calls.append(expression)
        if self.unavailable:
            raise ValidatorUnavailableError("scripted outage")
        answer = self.fhirpath_results.get(expression, self.default_fhirpath)
        if isinstance(answer, BaseException):
            raise answer
        values = tuple(answer) if isinstance(answer, list) else (answer,)
        return FhirPathOutcome(expression=expression, values=values)


@dataclass
class FakeLlmGateway:
    """A gateway whose completion is scripted, so the router can be tested offline.

    The point of the router tests is what happens to the model's output — that it
    is run through the cascade and never trusted blind — not how the bytes were
    fetched. The gateway's own policy and transport are covered separately.
    """

    resource: dict[str, Any] = field(default_factory=dict)
    resources: list[dict[str, Any]] = field(default_factory=list)
    model: str = "openrouter/openai/gpt-4o-mini"
    error: BaseException | None = None
    complete_calls: list[tuple[str, str]] = field(default_factory=list)
    transcript: str = "Patient reports feeling well."
    stt_model: str = "gemini/gemini-2.5-flash"
    transcribe_error: BaseException | None = None
    transcribe_calls: list[tuple[str, int]] = field(default_factory=list)

    def authorize(self, invocation: LlmInvocation, *, sending_phi: bool) -> None:
        del invocation, sending_phi

    async def complete_json(
        self,
        invocation: LlmInvocation,
        *,
        system_prompt: str,
        user_prompt: str,
        minimization: Minimization,
        max_tokens: int = 4096,
    ) -> LlmResult:
        del invocation, minimization, max_tokens
        self.complete_calls.append((system_prompt, user_prompt))
        if self.error is not None:
            raise self.error
        call_index = len(self.complete_calls) - 1
        resource = self.resources[call_index] if self.resources else self.resource
        return LlmResult(
            resource=resource,
            model=self.model,
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            cost_usd=Decimal("0.0001"),
            latency_ms=5,
            finish_reason="stop",
        )

    async def transcribe(
        self,
        invocation: SttInvocation,
        *,
        audio: bytes,
        media_format: str,
    ) -> DictationResult:
        del invocation
        self.transcribe_calls.append((media_format, len(audio)))
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return DictationResult(
            text=self.transcript,
            model=self.stt_model,
            usage={"prompt_tokens": 5, "completion_tokens": 15, "total_tokens": 20},
            cost_usd=Decimal("0.00005"),
            latency_ms=7,
        )


def issue(
    severity: str = "error",
    code: str = "structure",
    message: str = "scripted issue",
    expression: str | None = None,
) -> ValidatorIssue:
    return ValidatorIssue(severity=severity, code=code, message=message, expression=expression)


__all__ = [
    "FakeLlmGateway",
    "FakeTerminologyClient",
    "FakeValidatorClient",
    "ValidateCodeCall",
    "issue",
]
