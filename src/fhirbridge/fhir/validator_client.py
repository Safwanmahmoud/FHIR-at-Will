"""Client for the HAPI ``validator_cli.jar`` sidecar (AGENTS.md 4, 10 L2/L4).

Fail-closed rules (principle 2.4):

* Any transport error, timeout, non-2xx status or unparseable body raises
  :class:`ValidatorUnavailableError`, which the API renders as
  ``503 validator-unavailable``. We never fall back to "assume conformant".
* If the validator reports that a requested profile could not be resolved, that
  is *not* a pass — it means the IG is missing and every conformance claim we
  would make is void. It raises :class:`IgNotLoadedError`.

The sidecar's ``POST /loadIG`` endpoint was removed in org.hl7.fhir.core 6.6.0,
so IGs are baked in at container build time and passed with ``-ig`` at startup
instead. That is also better for reproducibility: a validator whose loaded IG
set can be mutated at runtime cannot support the byte-identical output claim in
principle 2.8. See docs/adr/0003-immutable-validator-igs.md and OPEN_QUESTIONS.md#Q2.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Self

import httpx
from fhiratwill import (
    FhirPathOutcome as CoreFhirPathOutcome,
)
from fhiratwill import (
    ValidatorIssue as CoreValidatorIssue,
)
from fhiratwill import (
    ValidatorOutcome as CoreValidatorOutcome,
)

from fhirbridge.domain.errors import IgNotLoadedError, ValidatorUnavailableError
from fhirbridge.observability.metrics import (
    DEPENDENCY_DURATION,
    DEPENDENCY_FAILURES,
    DEPENDENCY_UP,
)

logger = logging.getLogger(__name__)

DEPENDENCY: Final[str] = "validator"

_PROFILE_UNRESOLVED_MARKERS: Final[tuple[str, ...]] = (
    "could not be found",
    "could not be resolved",
    "unable to resolve profile",
    "profile reference",
    "is not known",
    "unknown profile",
    "has not been checked because it could not be found",
    "not been checked because",
)

_SEVERITY_ORDER: Final[dict[str, int]] = {
    "fatal": 4,
    "error": 3,
    "warning": 2,
    "information": 1,
}


class FhirPathNotEvaluableError(ValueError):
    """The FHIRPath host would not evaluate an expression.

    Deliberately a :class:`ValueError` and not a
    :class:`~fhirbridge.domain.errors.ValidatorUnavailableError`: the sidecar is
    healthy and answering, so the caller should record the single rule as
    inconclusive rather than failing the request closed.
    """


@dataclass(frozen=True, slots=True)
class ValidatorIssue(CoreValidatorIssue):
    @property
    def is_blocking(self) -> bool:
        return self.severity in ("fatal", "error")


@dataclass(frozen=True, slots=True)
class ValidatorOutcome(CoreValidatorOutcome):
    @property
    def errors(self) -> tuple[CoreValidatorIssue, ...]:
        return tuple(i for i in self.issues if i.severity in ("fatal", "error"))

    @property
    def warnings(self) -> tuple[CoreValidatorIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def informational(self) -> tuple[CoreValidatorIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "information")


@dataclass(frozen=True, slots=True)
class FhirPathOutcome(CoreFhirPathOutcome):
    raw: Any = None


@dataclass(frozen=True, slots=True)
class ValidatorHealth:
    """Result of a validator readiness probe."""

    reachable: bool
    profiles_loaded: tuple[str, ...] = ()
    profiles_missing: tuple[str, ...] = ()
    detail: str | None = None
    latency_ms: int | None = None

    @property
    def ready(self) -> bool:
        return self.reachable and not self.profiles_missing


@dataclass
class ValidatorClient:
    """Async client for one validator sidecar."""

    base_url: str
    timeout_s: float = 120.0
    client: httpx.AsyncClient | None = field(repr=False, default=None)
    _owns_client: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            self._owns_client = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        assert self.client is not None, "__post_init__ always installs a client"
        return self.client

    async def aclose(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()

    # --- Core operations --------------------------------------------------

    async def validate_resource(
        self,
        resource: dict[str, Any],
        *,
        profiles: Sequence[str] = (),
        best_practice: str | None = None,
    ) -> ValidatorOutcome:
        """Validate ``resource``, optionally against explicit profile URLs."""
        params: list[tuple[str, str]] = [("profile", profile) for profile in profiles]
        if best_practice:
            params.append(("bestPractice", best_practice))

        started = time.perf_counter()
        payload = await self._post_json(
            "/validateResource",
            operation="validateResource",
            params=params,
            content=json.dumps(resource, separators=(",", ":")).encode("utf-8"),
            content_type="application/fhir+json",
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        issues = _parse_operation_outcome(payload)
        _reject_unresolved_profiles(issues, profiles)
        return ValidatorOutcome(issues=issues, profiles=tuple(profiles), duration_ms=duration_ms)

    async def evaluate_fhirpath(self, resource: dict[str, Any], expression: str) -> FhirPathOutcome:
        """Evaluate a FHIRPath expression against ``resource``."""
        payload = await self._post_json(
            "/fhirpath",
            operation="fhirpath",
            params=[("expression", expression)],
            content=json.dumps(resource, separators=(",", ":")).encode("utf-8"),
            content_type="application/fhir+json",
        )
        return FhirPathOutcome(
            expression=expression,
            values=_parse_fhirpath_values(payload),
            raw=payload,
        )

    # --- Health -----------------------------------------------------------

    async def health(self, *, required_profiles: Sequence[str] = ()) -> ValidatorHealth:
        """Probe reachability and, when asked, that required profiles resolve.

        The profile check matters: a validator running without the US Core IG
        happily returns a clean OperationOutcome for a resource claiming a US
        Core profile, which would let us publish an unfounded conformance claim.
        """
        probe = {
            "resourceType": "Patient",
            "id": "fhirbridge-readiness-probe",
            "gender": "unknown",
        }
        started = time.perf_counter()
        try:
            outcome = await self.validate_resource(probe)
        except IgNotLoadedError as exc:  # pragma: no cover - probe sends no profile
            return ValidatorHealth(reachable=True, detail=exc.detail)
        except ValidatorUnavailableError as exc:
            DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(0)
            return ValidatorHealth(reachable=False, detail=exc.detail)
        latency_ms = int((time.perf_counter() - started) * 1000)
        del outcome

        loaded: list[str] = []
        missing: list[str] = []
        for profile in required_profiles:
            probe_with_profile = dict(probe) | {"meta": {"profile": [profile]}}
            try:
                await self.validate_resource(probe_with_profile, profiles=[profile])
            except IgNotLoadedError:
                missing.append(profile)
            except ValidatorUnavailableError as exc:
                DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(0)
                return ValidatorHealth(reachable=False, detail=exc.detail)
            else:
                loaded.append(profile)

        DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(0 if missing else 1)
        return ValidatorHealth(
            reachable=True,
            profiles_loaded=tuple(loaded),
            profiles_missing=tuple(missing),
            latency_ms=latency_ms,
            detail=None
            if not missing
            else (
                f"{len(missing)} required profile(s) do not resolve; start the validator "
                "with -ig for each required implementation guide"
            ),
        )

    # --- Transport --------------------------------------------------------

    async def _post_json(
        self,
        path: str,
        *,
        operation: str,
        params: Sequence[tuple[str, str]],
        content: bytes,
        content_type: str,
    ) -> Any:
        url = f"{self.base_url}{path}"
        started = time.perf_counter()
        try:
            response = await self.http.post(
                url,
                params=list(params),
                content=content,
                headers={"Content-Type": content_type, "Accept": "application/fhir+json"},
            )
        except httpx.TimeoutException as exc:
            self._record_failure(operation, "timeout")
            raise ValidatorUnavailableError(
                "The FHIR validator did not respond before the configured timeout.",
                safe_context={"operation": operation, "timeout_s": self.timeout_s},
            ) from exc
        except httpx.HTTPError as exc:
            self._record_failure(operation, "transport")
            raise ValidatorUnavailableError(
                "The FHIR validator could not be reached.",
                safe_context={"operation": operation},
            ) from exc
        finally:
            DEPENDENCY_DURATION.labels(dependency=DEPENDENCY, operation=operation).observe(
                time.perf_counter() - started
            )

        if response.status_code in (404, 405):
            self._record_failure(operation, "endpoint_missing")
            raise ValidatorUnavailableError(
                f"The validator does not expose {path}. Check that the sidecar is "
                "validator_cli.jar running in 'server' mode at the pinned version.",
                safe_context={"operation": operation, "status": response.status_code},
            )
        if operation == "fhirpath" and response.status_code == 400:
            # A rejected expression describes the expression, not the health of
            # the sidecar, so this must not fail closed: one unevaluable
            # invariant would otherwise 503 the whole resource.
            #
            # 6.10.2 rejects *any* expression containing a percent sign, even
            # correctly encoded as %25, which rules out every FHIRPath
            # environment variable (%resource, %context, %ucum). Those appear in
            # real FHIR invariants — bdl-3 among them — so this is reached in
            # normal operation, not just on a typo. L4 reports the rule as
            # inconclusive, which is the honest answer: not checked, not passed.
            raise FhirPathNotEvaluableError("The FHIRPath host refused to evaluate the expression.")
        if response.status_code >= 400:
            self._record_failure(operation, f"http_{response.status_code // 100}xx")
            raise ValidatorUnavailableError(
                "The FHIR validator returned an error status.",
                safe_context={"operation": operation, "status": response.status_code},
            )

        try:
            payload = response.json()
        except json.JSONDecodeError:
            # /fhirpath is not contractually JSON: it may answer with plain
            # `true`, or with an empty body for an empty collection. An empty
            # collection is a real FHIRPath result from a live server, so it must
            # not be treated as an outage — L4 already refuses to read it as a
            # pass. Failing closed here would 503 every resource that has an
            # empty-collection invariant.
            if operation == "fhirpath":
                return response.text.strip() or None
            self._record_failure(operation, "unparseable")
            raise ValidatorUnavailableError(
                "The FHIR validator returned a response that could not be parsed as JSON.",
                safe_context={"operation": operation},
            ) from None

        DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(1)
        return payload

    def _record_failure(self, operation: str, reason: str) -> None:
        DEPENDENCY_FAILURES.labels(dependency=DEPENDENCY, operation=operation, reason=reason).inc()
        DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(0)
        logger.warning(
            "validator_call_failed",
            extra={"dependency": DEPENDENCY, "operation": operation, "reason": reason},
        )


def _parse_operation_outcome(payload: Any) -> tuple[ValidatorIssue, ...]:
    """Normalize an ``OperationOutcome`` (or a Bundle of them) into issues."""
    if not isinstance(payload, dict):
        raise ValidatorUnavailableError(
            "The FHIR validator returned a payload that is not a FHIR resource.",
        )

    resource_type = payload.get("resourceType")
    if resource_type == "Bundle":
        issues: list[ValidatorIssue] = []
        for entry in payload.get("entry", []) or []:
            resource = (entry or {}).get("resource")
            if isinstance(resource, dict):
                issues.extend(_parse_operation_outcome(resource))
        return tuple(issues)

    if resource_type != "OperationOutcome":
        raise ValidatorUnavailableError(
            "The FHIR validator did not return an OperationOutcome.",
            safe_context={"resource_type": str(resource_type)},
        )

    parsed: list[ValidatorIssue] = []
    for raw in payload.get("issue", []) or []:
        if not isinstance(raw, dict):
            continue
        details = raw.get("details") or {}
        message = (
            raw.get("diagnostics")
            or (details.get("text") if isinstance(details, dict) else None)
            or raw.get("code")
            or ""
        )
        expressions = raw.get("expression") or raw.get("location") or []
        expression = expressions[0] if isinstance(expressions, list) and expressions else None
        parsed.append(
            ValidatorIssue(
                severity=str(raw.get("severity", "error")).lower(),
                code=str(raw.get("code", "processing")),
                message=str(message),
                expression=str(expression) if expression is not None else None,
                line=_extension_int(
                    raw, "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-line"
                ),
                column=_extension_int(
                    raw, "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-col"
                ),
            )
        )
    parsed.sort(key=lambda i: (-_SEVERITY_ORDER.get(i.severity, 0), i.expression or "", i.message))
    return tuple(parsed)


def _extension_int(issue_dict: dict[str, Any], url: str) -> int | None:
    for extension in issue_dict.get("extension", []) or []:
        if isinstance(extension, dict) and extension.get("url") == url:
            value = extension.get("valueInteger")
            if isinstance(value, int):
                return value
    return None


def _reject_unresolved_profiles(issues: Sequence[ValidatorIssue], profiles: Sequence[str]) -> None:
    """Fail closed when the validator could not resolve a requested profile."""
    if not profiles:
        return
    for candidate in issues:
        lowered = candidate.message.lower()
        if not any(marker in lowered for marker in _PROFILE_UNRESOLVED_MARKERS):
            continue
        for profile in profiles:
            if profile.lower() in lowered:
                raise IgNotLoadedError(
                    "The validator could not resolve a requested profile, so no "
                    "conformance claim can be made. Load the implementation guide "
                    "into the validator sidecar and retry.",
                    safe_context={"profile": profile},
                )


__all__ = [
    "DEPENDENCY",
    "FhirPathNotEvaluableError",
    "FhirPathOutcome",
    "ValidatorClient",
    "ValidatorHealth",
    "ValidatorIssue",
    "ValidatorOutcome",
]


def _parse_fhirpath_values(payload: Any) -> tuple[Any, ...]:
    """Normalize the sidecar's FHIRPath response into a value collection.

    The endpoint's response shape is not contractually specified, so accept the
    plausible encodings rather than guessing one: a bare JSON array, a
    ``Parameters`` resource, a scalar, or a plain-text ``true``/``false``.
    """
    if isinstance(payload, list):
        return tuple(payload)
    if isinstance(payload, dict):
        if payload.get("resourceType") == "Parameters":
            parameters = [p for p in payload.get("parameter", []) or [] if isinstance(p, dict)]
            # 6.10.2 echoes the request back in an `expression` parameter beside
            # the answer in `result`. Harvesting every `value*` would turn a
            # one-value answer into a two-value collection, and invariant
            # truthiness demands exactly one value — so a satisfied invariant
            # would read as violated. Only the answer counts.
            answers = [p for p in parameters if p.get("name") == "result"] or [
                p for p in parameters if p.get("name") != "expression"
            ]
            return tuple(
                value
                for parameter in answers
                for key, value in parameter.items()
                if key.startswith("value")
            )
        if "result" in payload:
            result = payload["result"]
            return tuple(result) if isinstance(result, list) else (result,)
        return (payload,)
    if isinstance(payload, str):
        text = payload.strip()
        if text.lower() in ("true", "false"):
            return (text.lower() == "true",)
        if not text:
            return ()
        return (text,)
    if payload is None:
        return ()
    return (payload,)
