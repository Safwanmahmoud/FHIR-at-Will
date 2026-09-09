"""A :class:`fhiratwill.TerminologyClient` adapter for any FHIR terminology server.

Two decisions worth stating explicitly:

* **Operations are invoked with POST and a ``Parameters`` body, never GET with
  query parameters.** Codes and ValueSet URLs describe a patient's clinical
  state, and principle 2.6 keeps that out of URLs (and therefore out of proxy
  logs and access logs). It also avoids URL length limits on ``$expand``
  filters.
* **Fail closed.** Transport failure, timeout, 5xx, or an unparseable body
  raises :class:`TerminologyUnavailableError`, rendered as
  ``503 terminology-unavailable``. An unknown ValueSet raises a domain error. We
  never answer "not valid" to a question the server could not answer.
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
    CodeSystemVersion,
    ExpansionResult,
    SubsumesResult,
    SubsumptionOutcome,
    TranslateResult,
)
from fhiratwill import (
    Coding as CoreCoding,
)
from fhiratwill import (
    LookupResult as CoreLookupResult,
)
from fhiratwill import (
    TerminologyHealth as CoreTerminologyHealth,
)
from fhiratwill import (
    TranslateMatch as CoreTranslateMatch,
)
from fhiratwill import (
    ValidateCodeResult as CoreValidateCodeResult,
)

from fhirbridge.config import Settings, TerminologyAuthMode
from fhirbridge.domain.errors import (
    DomainError,
    ErrorCode,
    TerminologyUnavailableError,
)
from fhirbridge.observability.metrics import (
    DEPENDENCY_DURATION,
    DEPENDENCY_FAILURES,
    DEPENDENCY_UP,
    TERMINOLOGY_CACHE,
    TERMINOLOGY_VALIDATE_CODE,
)
from fhirbridge.terminology.cache import TtlCache

logger = logging.getLogger(__name__)

DEPENDENCY: Final[str] = "terminology"

_ValidateCacheKey = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class Coding(CoreCoding):
    @property
    def key(self) -> tuple[str, str, str]:
        return (self.system or "", self.code or "", self.version or "")

    def __str__(self) -> str:
        return f"{self.system}|{self.code}"


@dataclass(frozen=True, slots=True)
class ValidateCodeResult(CoreValidateCodeResult):
    @property
    def in_value_set(self) -> bool | None:
        return None if self.value_set is None else self.result


@dataclass(frozen=True, slots=True)
class LookupResult(CoreLookupResult):
    name: str | None = None
    designations: tuple[str, ...] = ()
    inactive: bool | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TranslateMatch(CoreTranslateMatch):
    source: str | None = None


@dataclass(frozen=True, slots=True)
class TerminologyHealth(CoreTerminologyHealth):
    latency_ms: int | None = None

    @property
    def ready(self) -> bool:
        return self.reachable


class FhirTerminologyClient:
    """Adapter for HAPI FHIR JPA, Ontoserver, Snowstorm and tx.fhir.org."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 30.0,
        auth_mode: TerminologyAuthMode = TerminologyAuthMode.NONE,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        cache_ttl_s: float = 86_400.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._headers = self._build_auth_headers(auth_mode, username, password, token)
        self._validate_cache: TtlCache[_ValidateCacheKey, ValidateCodeResult] = TtlCache(
            ttl_s=cache_ttl_s
        )
        self._lookup_cache: TtlCache[tuple[str, str, str], LookupResult] = TtlCache(
            ttl_s=cache_ttl_s
        )

    @classmethod
    def from_settings(cls, settings: Settings, *, client: httpx.AsyncClient | None = None) -> Self:
        return cls(
            base_url=settings.terminology_base_url,
            timeout_s=settings.terminology_timeout_s,
            auth_mode=settings.terminology_auth_mode,
            username=settings.terminology_username,
            password=(
                settings.terminology_password.get_secret_value()
                if settings.terminology_password
                else None
            ),
            token=(
                settings.terminology_token.get_secret_value()
                if settings.terminology_token
                else None
            ),
            cache_ttl_s=settings.terminology_cache_ttl.total_seconds(),
            client=client,
        )

    @staticmethod
    def _build_auth_headers(
        mode: TerminologyAuthMode,
        username: str | None,
        password: str | None,
        token: str | None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/fhir+json",
            "Content-Type": "application/fhir+json",
        }
        match mode:
            case TerminologyAuthMode.BASIC if username and password:
                import base64

                raw = f"{username}:{password}".encode()
                headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
            case TerminologyAuthMode.BEARER | TerminologyAuthMode.OAUTH2_CLIENT_CREDENTIALS if (
                token
            ):
                headers["Authorization"] = f"Bearer {token}"
            case _:
                pass
        return headers

    def __repr__(self) -> str:
        # Never let the Authorization header reach a log line or traceback.
        return f"FhirTerminologyClient(base_url={self.base_url!r})"

    __str__ = __repr__

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- Operations -------------------------------------------------------

    async def validate_code(
        self,
        *,
        system: str | None,
        code: str,
        display: str | None = None,
        version: str | None = None,
        value_set: str | None = None,
    ) -> ValidateCodeResult:
        cache_key: _ValidateCacheKey = (
            system or "",
            code,
            version or "",
            value_set or "",
            display or "",
        )
        cached = self._validate_cache.get(cache_key)
        if cached is not None:
            TERMINOLOGY_CACHE.labels(operation="validate-code", outcome="hit").inc()
            return cached
        TERMINOLOGY_CACHE.labels(operation="validate-code", outcome="miss").inc()

        parameters: list[dict[str, Any]] = [{"name": "code", "valueCode": code}]
        if system:
            parameters.append({"name": "system", "valueUri": system})
        if version:
            parameters.append({"name": "systemVersion", "valueString": version})
        if display:
            parameters.append({"name": "display", "valueString": display})
        if value_set:
            parameters.append({"name": "url", "valueUri": value_set})
            if not system:
                # $validate-code accepts a code on its own only alongside
                # inferSystem: the spec's parameter combinations are
                # coding | codeableConcept | code+system | code+inferSystem.
                # Omitting it makes a conformant server answer 422, which this
                # client (rightly) treats as a fail-closed outage — so every
                # primitive-code binding in bindings.yaml, which by definition
                # carries no system, would 503 rather than be checked.
                parameters.append({"name": "inferSystem", "valueBoolean": True})

        endpoint = "/ValueSet/$validate-code" if value_set else "/CodeSystem/$validate-code"
        payload = await self._invoke(endpoint, "validate-code", parameters)
        values = _parameters_to_dict(payload)

        result = bool(values.get("result", False))
        outcome = ValidateCodeResult(
            result=result,
            coding=Coding(system=system, code=code, display=display, version=version),
            value_set=value_set,
            display=_as_str(values.get("display")),
            message=_as_str(values.get("message")),
            code_system_version=_as_str(values.get("version")),
            issues=_issue_texts(values.get("issues")),
        )
        TERMINOLOGY_VALIDATE_CODE.labels(result="valid" if result else "invalid").inc()
        self._validate_cache.set(cache_key, outcome)
        return outcome

    async def lookup(self, *, system: str, code: str, version: str | None = None) -> LookupResult:
        cache_key = (system, code, version or "")
        cached = self._lookup_cache.get(cache_key)
        if cached is not None:
            TERMINOLOGY_CACHE.labels(operation="lookup", outcome="hit").inc()
            return cached
        TERMINOLOGY_CACHE.labels(operation="lookup", outcome="miss").inc()

        parameters: list[dict[str, Any]] = [
            {"name": "system", "valueUri": system},
            {"name": "code", "valueCode": code},
        ]
        if version:
            parameters.append({"name": "version", "valueString": version})

        payload = await self._invoke("/CodeSystem/$lookup", "lookup", parameters)
        values = _parameters_to_dict(payload)
        result = LookupResult(
            coding=Coding(
                system=system, code=code, display=_as_str(values.get("display")), version=version
            ),
            name=_as_str(values.get("name")),
            display=_as_str(values.get("display")),
            code_system_version=_as_str(values.get("version")),
            designations=_designation_values(payload),
            properties=_property_values(payload),
            inactive=_as_bool(values.get("inactive")),
        )
        self._lookup_cache.set(cache_key, result)
        return result

    async def expand(
        self,
        *,
        value_set: str,
        filter_text: str | None = None,
        count: int | None = None,
        offset: int | None = None,
    ) -> ExpansionResult:
        parameters: list[dict[str, Any]] = [{"name": "url", "valueUri": value_set}]
        if filter_text:
            parameters.append({"name": "filter", "valueString": filter_text})
        if count is not None:
            parameters.append({"name": "count", "valueInteger": count})
        if offset is not None:
            parameters.append({"name": "offset", "valueInteger": offset})

        payload = await self._invoke("/ValueSet/$expand", "expand", parameters)
        if not isinstance(payload, dict) or payload.get("resourceType") != "ValueSet":
            raise TerminologyUnavailableError(
                "The terminology server did not return a ValueSet for $expand.",
                safe_context={"value_set": value_set},
            )
        expansion = payload.get("expansion") or {}
        contains = tuple(
            Coding(
                system=_as_str(item.get("system")),
                code=_as_str(item.get("code")),
                display=_as_str(item.get("display")),
                version=_as_str(item.get("version")),
            )
            for item in expansion.get("contains", []) or []
            if isinstance(item, dict)
        )
        return ExpansionResult(
            value_set=value_set,
            contains=contains,
            total=expansion.get("total") if isinstance(expansion.get("total"), int) else None,
            offset=expansion.get("offset") if isinstance(expansion.get("offset"), int) else None,
            incomplete=bool(expansion.get("total") or 0) and len(contains) < (expansion["total"]),
        )

    async def subsumes(
        self,
        *,
        system: str,
        code_a: str,
        code_b: str,
        version: str | None = None,
    ) -> SubsumesResult:
        parameters: list[dict[str, Any]] = [
            {"name": "system", "valueUri": system},
            {"name": "codeA", "valueCode": code_a},
            {"name": "codeB", "valueCode": code_b},
        ]
        if version:
            parameters.append({"name": "version", "valueString": version})

        payload = await self._invoke("/CodeSystem/$subsumes", "subsumes", parameters)
        values = _parameters_to_dict(payload)
        raw = _as_str(values.get("outcome")) or "not-subsumed"
        try:
            outcome = SubsumptionOutcome(raw)
        except ValueError:
            outcome = SubsumptionOutcome.NOT_SUBSUMED
        return SubsumesResult(
            outcome=outcome,
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
        parameters: list[dict[str, Any]] = [
            {"name": "system", "valueUri": system},
            {"name": "code", "valueCode": code},
        ]
        if target_system:
            parameters.append({"name": "target", "valueUri": target_system})
        if concept_map:
            parameters.append({"name": "url", "valueUri": concept_map})

        payload = await self._invoke("/ConceptMap/$translate", "translate", parameters)
        values = _parameters_to_dict(payload)
        return TranslateResult(
            result=bool(values.get("result", False)),
            matches=_translate_matches(payload),
            message=_as_str(values.get("message")),
        )

    async def health(self, *, code_systems: Sequence[str] = ()) -> TerminologyHealth:
        """Read ``/metadata`` and report the versions of the named CodeSystems."""
        started = time.perf_counter()
        try:
            response = await self._client.get(
                f"{self.base_url}/metadata",
                params={"_summary": "true"},
                headers={"Accept": "application/fhir+json"},
            )
        except httpx.HTTPError as exc:
            self._record_failure("metadata", _failure_reason(exc))
            return TerminologyHealth(
                reachable=False, detail="The terminology server could not be reached."
            )
        finally:
            DEPENDENCY_DURATION.labels(dependency=DEPENDENCY, operation="metadata").observe(
                time.perf_counter() - started
            )

        if response.status_code >= 400:
            self._record_failure("metadata", f"http_{response.status_code // 100}xx")
            return TerminologyHealth(
                reachable=False,
                detail="The terminology server returned an error for /metadata.",
            )

        try:
            body = response.json()
        except json.JSONDecodeError:
            self._record_failure("metadata", "unparseable")
            return TerminologyHealth(reachable=False, detail="/metadata was not valid JSON.")

        software = None
        if isinstance(body, dict) and isinstance(body.get("software"), dict):
            name = body["software"].get("name")
            version = body["software"].get("version")
            software = " ".join(part for part in (name, version) if part) or None

        versions: list[CodeSystemVersion] = []
        for system in code_systems:
            try:
                probe = await self.lookup(system=system, code="__fhirbridge_probe__")
                versions.append(CodeSystemVersion(system, probe.code_system_version))
            except (TerminologyUnavailableError, DomainError):
                versions.append(CodeSystemVersion(system, None))

        DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(1)
        return TerminologyHealth(
            reachable=True,
            software=software,
            fhir_version=(_as_str(body.get("fhirVersion")) if isinstance(body, dict) else None),
            code_systems=tuple(versions),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- Transport --------------------------------------------------------

    async def _invoke(self, path: str, operation: str, parameters: Sequence[dict[str, Any]]) -> Any:
        body = {"resourceType": "Parameters", "parameter": list(parameters)}
        url = f"{self.base_url}{path}"
        started = time.perf_counter()
        try:
            response = await self._client.post(
                url,
                content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            reason = _failure_reason(exc)
            self._record_failure(operation, reason)
            raise TerminologyUnavailableError(
                "The terminology server could not be reached; failing closed rather "
                "than emitting unvalidated codes.",
                safe_context={"operation": operation, "reason": reason},
            ) from exc
        finally:
            DEPENDENCY_DURATION.labels(dependency=DEPENDENCY, operation=operation).observe(
                time.perf_counter() - started
            )

        if response.status_code in (401, 403):
            self._record_failure(operation, "auth")
            raise TerminologyUnavailableError(
                "The terminology server rejected our credentials.",
                safe_context={"operation": operation, "status": response.status_code},
            )
        if response.status_code == 404:
            self._record_failure(operation, "not_found")
            raise DomainError(
                "The terminology server does not know the requested CodeSystem, "
                "ValueSet or ConceptMap.",
                code=ErrorCode.UNKNOWN_VALUE_SET,
                safe_context={"operation": operation},
            )
        if response.status_code >= 400:
            self._record_failure(operation, f"http_{response.status_code // 100}xx")
            raise TerminologyUnavailableError(
                "The terminology server returned an error status.",
                safe_context={"operation": operation, "status": response.status_code},
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            self._record_failure(operation, "unparseable")
            raise TerminologyUnavailableError(
                "The terminology server returned a response that was not valid JSON.",
                safe_context={"operation": operation},
            ) from exc

        if isinstance(payload, dict) and payload.get("resourceType") == "OperationOutcome":
            self._record_failure(operation, "operation_outcome")
            raise TerminologyUnavailableError(
                "The terminology server returned an OperationOutcome instead of a result.",
                safe_context={"operation": operation},
            )

        DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(1)
        return payload

    def _record_failure(self, operation: str, reason: str) -> None:
        DEPENDENCY_FAILURES.labels(dependency=DEPENDENCY, operation=operation, reason=reason).inc()
        DEPENDENCY_UP.labels(dependency=DEPENDENCY).set(0)
        logger.warning(
            "terminology_call_failed",
            extra={"dependency": DEPENDENCY, "operation": operation, "reason": reason},
        )


# --- Parameters helpers ----------------------------------------------------


def _failure_reason(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    return "transport"


def _parameters_to_dict(payload: Any) -> dict[str, Any]:
    """Flatten a ``Parameters`` resource into ``{name: value}``.

    ``value[x]`` is the common case, but not the only one: ``$validate-code``
    returns its ``issues`` parameter as a nested ``OperationOutcome`` under
    ``resource``, and ``$lookup`` nests ``designation`` and ``property`` under
    ``part``. Handling only ``value[x]`` silently discards the server's own
    explanation of why a code was rejected.
    """
    if not isinstance(payload, dict) or payload.get("resourceType") != "Parameters":
        raise TerminologyUnavailableError(
            "The terminology server did not return a Parameters resource."
        )
    result: dict[str, Any] = {}
    for parameter in payload.get("parameter", []) or []:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        if not isinstance(name, str) or name in result:
            continue
        for key, value in parameter.items():
            if key.startswith("value"):
                result[name] = value
                break
        else:
            if "resource" in parameter:
                result[name] = parameter["resource"]
            elif "part" in parameter:
                result[name] = parameter["part"]
    return result


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _issue_texts(value: Any) -> tuple[str, ...]:
    """Pull issue diagnostics out of the ``issues`` parameter of $validate-code."""
    if not isinstance(value, dict) or value.get("resourceType") != "OperationOutcome":
        return ()
    texts: list[str] = []
    for item in value.get("issue", []) or []:
        if not isinstance(item, dict):
            continue
        details = item.get("details")
        text = item.get("diagnostics") or (
            details.get("text") if isinstance(details, dict) else None
        )
        if isinstance(text, str):
            texts.append(text)
    return tuple(texts)


def _designation_values(payload: Any) -> tuple[str, ...]:
    designations: list[str] = []
    if not isinstance(payload, dict):
        return ()
    for parameter in payload.get("parameter", []) or []:
        if not isinstance(parameter, dict) or parameter.get("name") != "designation":
            continue
        for part in parameter.get("part", []) or []:
            if isinstance(part, dict) and part.get("name") == "value":
                value = part.get("valueString")
                if isinstance(value, str):
                    designations.append(value)
    return tuple(designations)


def _property_values(payload: Any) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return properties
    for parameter in payload.get("parameter", []) or []:
        if not isinstance(parameter, dict) or parameter.get("name") != "property":
            continue
        code: str | None = None
        value: Any = None
        for part in parameter.get("part", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("name") == "code":
                code = _as_str(part.get("valueCode")) or _as_str(part.get("valueString"))
            else:
                for key, candidate in part.items():
                    if key.startswith("value"):
                        value = candidate
                        break
        if code is not None:
            properties[code] = value
    return properties


def _translate_matches(payload: Any) -> tuple[TranslateMatch, ...]:
    matches: list[TranslateMatch] = []
    if not isinstance(payload, dict):
        return ()
    for parameter in payload.get("parameter", []) or []:
        if not isinstance(parameter, dict) or parameter.get("name") != "match":
            continue
        equivalence = "unmatched"
        concept: Coding | None = None
        source: str | None = None
        for part in parameter.get("part", []) or []:
            if not isinstance(part, dict):
                continue
            match part.get("name"):
                case "equivalence" | "relationship":
                    equivalence = _as_str(part.get("valueCode")) or equivalence
                case "concept":
                    raw = part.get("valueCoding")
                    if isinstance(raw, dict):
                        concept = Coding(
                            system=_as_str(raw.get("system")),
                            code=_as_str(raw.get("code")),
                            display=_as_str(raw.get("display")),
                            version=_as_str(raw.get("version")),
                        )
                case "source":
                    source = _as_str(part.get("valueUri"))
                case _:
                    pass
        if concept is not None:
            matches.append(TranslateMatch(equivalence=equivalence, concept=concept, source=source))
    return tuple(matches)


__all__ = ["DEPENDENCY", "FhirTerminologyClient"]
