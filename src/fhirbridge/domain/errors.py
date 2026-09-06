"""The stable error catalogue and exception hierarchy (AGENTS.md 12).

Two rules govern everything in this module:

* Every error the API can return has a **stable machine code** here. The codes
  are published as a FHIR ``CodeSystem`` so clients can branch on them without
  parsing prose. Renaming a code is a breaking API change.
* ``detail`` and ``safe_context`` MUST NOT contain PHI or secrets (principles
  2.6 and 2.7). They are developer-authored strings and identifier-only
  key/value pairs. Clinical content belongs in response *bodies* (for example
  the validation report), never in error diagnostics, logs or metric labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Self

ERROR_CODE_SYSTEM: Final[str] = "https://fhirbridge.org/CodeSystem/errors"

type SafeContext = dict[str, str | int | float | bool]
"""Identifier-only error context. Scalars only, so nothing structured (and
therefore nothing clinical) can be attached by accident."""


class ErrorCategory(StrEnum):
    """Which response envelope an error renders into."""

    DOMAIN = "domain"
    """Clinical / FHIR-semantic failure. Rendered as an ``OperationOutcome``."""

    PLATFORM = "platform"
    """Transport, auth or plumbing failure. Rendered as ``{"error": {...}}``."""


class ErrorCode(StrEnum):
    """Stable machine-readable error codes. Values are part of the public API."""

    # --- BYOK / LLM (AGENTS.md 7); raised from M2 onward -------------------
    LLM_CREDENTIALS_REQUIRED = "llm-credentials-required"
    LLM_AUTH_FAILED = "llm-auth-failed"
    LLM_QUOTA_EXHAUSTED = "llm-quota-exhausted"
    LLM_RATE_LIMITED = "llm-rate-limited"
    LLM_CONTEXT_EXCEEDED = "llm-context-exceeded"
    LLM_SCHEMA_VIOLATION = "llm-schema-violation"
    LLM_CONTENT_FILTERED = "llm-content-filtered"
    MODEL_NOT_QUALIFIED = "model-not-qualified"
    BUDGET_EXCEEDED = "budget-exceeded"
    EGRESS_BLOCKED = "egress-blocked"
    PHI_EGRESS_NOT_ACKNOWLEDGED = "phi-egress-not-acknowledged"
    PHI_MINIMIZATION_UNAVAILABLE = "phi-minimization-unavailable"
    PHI_MINIMIZATION_REQUIRED = "phi-minimization-required"
    PHI_MINIMIZATION_FAILED = "phi-minimization-failed"
    AUDIO_EGRESS_NOT_PERMITTED = "audio-egress-not-permitted"
    INSECURE_TRANSPORT = "insecure-transport"
    CREDENTIAL_EXPIRED = "credential-expired"

    # --- Dependencies: these fail closed (principle 2.4) ------------------
    TERMINOLOGY_UNAVAILABLE = "terminology-unavailable"
    VALIDATOR_UNAVAILABLE = "validator-unavailable"
    TARGET_UNAVAILABLE = "target-unavailable"

    # --- Documents and conversion ----------------------------------------
    UNREADABLE_DOCUMENT = "unreadable-document"
    NO_CLINICAL_CONTENT = "no-clinical-content"
    PROFILE_IMPOSSIBLE = "profile-impossible"
    SPAN_VERIFICATION_FAILED = "span-verification-failed"
    REVIEW_REQUIRED = "review-required"

    # --- FHIR input handling ---------------------------------------------
    INVALID_FHIR_RESOURCE = "invalid-fhir-resource"
    UNSUPPORTED_FHIR_VERSION = "unsupported-fhir-version"
    UNSUPPORTED_RESOURCE_TYPE = "unsupported-resource-type"
    IG_NOT_LOADED = "ig-not-loaded"
    UNKNOWN_VALUE_SET = "unknown-value-set"

    # --- Platform ---------------------------------------------------------
    INVALID_REQUEST = "invalid-request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not-found"
    IDEMPOTENCY_CONFLICT = "idempotency-conflict"
    PRECONDITION_FAILED = "precondition-failed"
    PAYLOAD_TOO_LARGE = "payload-too-large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported-media-type"
    RATE_LIMITED = "rate-limited"
    NOT_IMPLEMENTED = "not-implemented"
    INTERNAL_ERROR = "internal-error"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """Static metadata for one error code."""

    code: ErrorCode
    http_status: int
    issue_type: str
    """A code from the FHIR ``issue-type`` value set (required binding)."""
    category: ErrorCategory
    title: str
    retryable: bool = False
    """True when a client may retry the identical request unchanged."""


def _spec(
    code: ErrorCode,
    http_status: int,
    issue_type: str,
    title: str,
    *,
    category: ErrorCategory = ErrorCategory.DOMAIN,
    retryable: bool = False,
) -> tuple[ErrorCode, ErrorSpec]:
    return code, ErrorSpec(
        code=code,
        http_status=http_status,
        issue_type=issue_type,
        category=category,
        title=title,
        retryable=retryable,
    )


_P: Final = ErrorCategory.PLATFORM

ERROR_SPECS: Final[dict[ErrorCode, ErrorSpec]] = dict(
    (
        _spec(
            ErrorCode.LLM_CREDENTIALS_REQUIRED,
            400,
            "security",
            "No LLM credentials could be resolved for this request",
        ),
        _spec(ErrorCode.LLM_AUTH_FAILED, 400, "security", "The LLM provider rejected the API key"),
        _spec(
            ErrorCode.LLM_QUOTA_EXHAUSTED,
            422,
            "business-rule",
            "The LLM provider reports the account quota is exhausted",
        ),
        _spec(
            ErrorCode.LLM_RATE_LIMITED,
            429,
            "throttled",
            "The LLM provider rate-limited this request",
            retryable=True,
        ),
        _spec(
            ErrorCode.LLM_CONTEXT_EXCEEDED,
            422,
            "too-long",
            "The input exceeds the model's context window",
        ),
        _spec(
            ErrorCode.LLM_SCHEMA_VIOLATION,
            422,
            "processing",
            "The model could not produce output matching the required schema",
        ),
        _spec(
            ErrorCode.LLM_CONTENT_FILTERED,
            422,
            "processing",
            "The LLM provider's content filter blocked this request",
        ),
        _spec(
            ErrorCode.MODEL_NOT_QUALIFIED,
            422,
            "business-rule",
            "The requested model is below the configured minimum qualification tier",
        ),
        _spec(
            ErrorCode.BUDGET_EXCEEDED,
            422,
            "too-costly",
            "The request would exceed the configured cost budget",
        ),
        _spec(
            ErrorCode.EGRESS_BLOCKED,
            451,
            "forbidden",
            "The requested external endpoint is blocked by egress policy",
        ),
        _spec(
            ErrorCode.PHI_EGRESS_NOT_ACKNOWLEDGED,
            422,
            "business-rule",
            "Sending PHI to an external provider requires explicit acknowledgement",
        ),
        _spec(
            ErrorCode.PHI_MINIMIZATION_UNAVAILABLE,
            503,
            "transient",
            "The required PHI minimization layer is unavailable",
            retryable=True,
        ),
        _spec(
            ErrorCode.PHI_MINIMIZATION_REQUIRED,
            422,
            "business-rule",
            "PHI minimization is required before external model egress",
        ),
        _spec(
            ErrorCode.PHI_MINIMIZATION_FAILED,
            500,
            "exception",
            "PHI minimization failed closed",
        ),
        _spec(
            ErrorCode.AUDIO_EGRESS_NOT_PERMITTED,
            422,
            "business-rule",
            "Audio egress is not permitted while PHI minimization is enforced",
        ),
        _spec(
            ErrorCode.INSECURE_TRANSPORT,
            400,
            "security",
            "Credentials may not be supplied over plaintext HTTP",
        ),
        _spec(
            ErrorCode.CREDENTIAL_EXPIRED,
            409,
            "expired",
            "The ephemeral credential for this job has expired and must be re-supplied",
        ),
        _spec(
            ErrorCode.TERMINOLOGY_UNAVAILABLE,
            503,
            "transient",
            "The terminology server is unavailable; failing closed",
            retryable=True,
        ),
        _spec(
            ErrorCode.VALIDATOR_UNAVAILABLE,
            503,
            "transient",
            "The FHIR validator is unavailable; failing closed",
            retryable=True,
        ),
        _spec(
            ErrorCode.TARGET_UNAVAILABLE,
            503,
            "transient",
            "The delivery target is unavailable; failing closed",
            retryable=True,
        ),
        _spec(ErrorCode.UNREADABLE_DOCUMENT, 422, "processing", "The document could not be read"),
        _spec(
            ErrorCode.NO_CLINICAL_CONTENT,
            422,
            "processing",
            "No clinical content was found in the document",
        ),
        _spec(
            ErrorCode.PROFILE_IMPOSSIBLE,
            422,
            "business-rule",
            "The source cannot satisfy the requested profile",
        ),
        _spec(
            ErrorCode.SPAN_VERIFICATION_FAILED,
            422,
            "processing",
            "Extracted spans could not be verified against the normalized document text",
        ),
        _spec(
            ErrorCode.REVIEW_REQUIRED,
            422,
            "business-rule",
            "This action requires a completed human review",
        ),
        _spec(
            ErrorCode.INVALID_FHIR_RESOURCE,
            400,
            "structure",
            "The supplied payload is not a structurally valid FHIR resource",
        ),
        _spec(
            ErrorCode.UNSUPPORTED_FHIR_VERSION,
            422,
            "not-supported",
            "The requested FHIR version is not supported by this build",
        ),
        _spec(
            ErrorCode.UNSUPPORTED_RESOURCE_TYPE,
            422,
            "not-supported",
            "The resource type is not part of the supported FHIR version",
        ),
        _spec(
            ErrorCode.IG_NOT_LOADED,
            422,
            "not-supported",
            "The requested implementation guide is not loaded in the validator",
        ),
        _spec(ErrorCode.UNKNOWN_VALUE_SET, 422, "not-found", "The requested ValueSet is not known"),
        _spec(
            ErrorCode.INVALID_REQUEST,
            400,
            "invalid",
            "The request is malformed",
            category=_P,
        ),
        _spec(
            ErrorCode.UNAUTHENTICATED,
            401,
            "login",
            "Authentication is required",
            category=_P,
        ),
        _spec(
            ErrorCode.FORBIDDEN,
            403,
            "forbidden",
            "The caller lacks the required scope",
            category=_P,
        ),
        _spec(
            ErrorCode.NOT_FOUND,
            404,
            "not-found",
            "No such resource",
            category=_P,
        ),
        _spec(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            409,
            "conflict",
            "This Idempotency-Key was already used with a different request body",
            category=_P,
        ),
        _spec(
            ErrorCode.PRECONDITION_FAILED,
            409,
            "conflict",
            "The supplied ETag is stale",
            category=_P,
        ),
        _spec(
            ErrorCode.PAYLOAD_TOO_LARGE,
            413,
            "too-long",
            "The payload exceeds the configured size limit",
            category=_P,
        ),
        _spec(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            415,
            "not-supported",
            "Unsupported Content-Type",
            category=_P,
        ),
        _spec(
            ErrorCode.RATE_LIMITED,
            429,
            "throttled",
            "Too many requests",
            category=_P,
            retryable=True,
        ),
        _spec(
            ErrorCode.NOT_IMPLEMENTED,
            501,
            "not-supported",
            "Not implemented in this version",
            category=_P,
        ),
        _spec(
            ErrorCode.INTERNAL_ERROR,
            500,
            "exception",
            "Internal error",
            category=_P,
        ),
    )
)

assert set(ERROR_SPECS) == set(ErrorCode), "every ErrorCode needs an ErrorSpec"


class FhirbridgeError(Exception):
    """Base class for every error this service raises deliberately.

    ``detail`` is a short, developer-authored, PHI-free sentence. ``safe_context``
    holds identifier-only key/value pairs (ids, resource types, FHIRPath
    expressions, hostnames) that help a caller act on the error.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: ErrorCode | None = None,
        safe_context: SafeContext | None = None,
        retry_after_s: int | None = None,
        http_status: int | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        self.spec = ERROR_SPECS[self.code]
        self.detail = detail or self.spec.title
        self.safe_context: SafeContext = dict(safe_context or {})
        self.retry_after_s = retry_after_s
        self.status_override = http_status
        super().__init__(f"{self.code}: {self.detail}")

    @property
    def http_status(self) -> int:
        """The status to return, which is the spec's unless overridden.

        The override exists for statuses that share a machine code but must not
        share a status. ``405`` and ``422`` both carry ``invalid-request``, and
        collapsing them onto the code's ``400`` would tell a client its body was
        malformed when the real problem was the method — and would lose the
        ``400``/``422`` distinction AGENTS.md 12 requires.
        """
        return self.status_override or self.spec.http_status

    @property
    def category(self) -> ErrorCategory:
        return self.spec.category

    def with_context(self, **kwargs: str | int | float | bool) -> Self:
        self.safe_context.update(kwargs)
        return self


class DomainError(FhirbridgeError):
    """Renders as a FHIR ``OperationOutcome``."""


class PlatformError(FhirbridgeError):
    """Renders as the ``{"error": {...}}`` JSON envelope."""


# --- Concrete errors used by M0/M1 -----------------------------------------


class InvalidRequestError(PlatformError):
    code = ErrorCode.INVALID_REQUEST


class UnauthenticatedError(PlatformError):
    code = ErrorCode.UNAUTHENTICATED


class ForbiddenError(PlatformError):
    code = ErrorCode.FORBIDDEN


class NotFoundError(PlatformError):
    code = ErrorCode.NOT_FOUND


class IdempotencyConflictError(PlatformError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT


class PayloadTooLargeError(PlatformError):
    code = ErrorCode.PAYLOAD_TOO_LARGE


class UnsupportedMediaTypeError(PlatformError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE


class NotImplementedInV1Error(PlatformError):
    code = ErrorCode.NOT_IMPLEMENTED


class InsecureTransportError(DomainError):
    code = ErrorCode.INSECURE_TRANSPORT


class UnreadableDocumentError(DomainError):
    code = ErrorCode.UNREADABLE_DOCUMENT


class InvalidFhirResourceError(DomainError):
    code = ErrorCode.INVALID_FHIR_RESOURCE


class UnsupportedFhirVersionError(DomainError):
    code = ErrorCode.UNSUPPORTED_FHIR_VERSION


class UnsupportedResourceTypeError(DomainError):
    code = ErrorCode.UNSUPPORTED_RESOURCE_TYPE


class IgNotLoadedError(DomainError):
    code = ErrorCode.IG_NOT_LOADED


class DependencyUnavailableError(DomainError):
    """Base for the fail-closed dependency errors (principle 2.4)."""

    default_retry_after_s: int = 30

    def __init__(
        self,
        detail: str | None = None,
        *,
        safe_context: SafeContext | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(
            detail,
            safe_context=safe_context,
            retry_after_s=retry_after_s
            if retry_after_s is not None
            else self.default_retry_after_s,
        )


class ValidatorUnavailableError(DependencyUnavailableError):
    code = ErrorCode.VALIDATOR_UNAVAILABLE


class TerminologyUnavailableError(DependencyUnavailableError):
    code = ErrorCode.TERMINOLOGY_UNAVAILABLE


class TargetUnavailableError(DependencyUnavailableError):
    code = ErrorCode.TARGET_UNAVAILABLE


# --- BYOK / LLM errors (AGENTS.md 7); raised from M2 onward ----------------
# The specs (status, issue-type, category) already live in ERROR_SPECS above;
# these are the concrete exception types handlers and the gateway raise.


class LlmCredentialsRequiredError(DomainError):
    code = ErrorCode.LLM_CREDENTIALS_REQUIRED


class LlmAuthFailedError(DomainError):
    code = ErrorCode.LLM_AUTH_FAILED


class LlmQuotaExhaustedError(DomainError):
    code = ErrorCode.LLM_QUOTA_EXHAUSTED


class LlmRateLimitedError(DomainError):
    code = ErrorCode.LLM_RATE_LIMITED


class LlmContextExceededError(DomainError):
    code = ErrorCode.LLM_CONTEXT_EXCEEDED


class LlmSchemaViolationError(DomainError):
    code = ErrorCode.LLM_SCHEMA_VIOLATION


class LlmContentFilteredError(DomainError):
    code = ErrorCode.LLM_CONTENT_FILTERED


class ModelNotQualifiedError(DomainError):
    code = ErrorCode.MODEL_NOT_QUALIFIED


class BudgetExceededError(DomainError):
    code = ErrorCode.BUDGET_EXCEEDED


class EgressBlockedError(DomainError):
    code = ErrorCode.EGRESS_BLOCKED


class PhiEgressNotAcknowledgedError(DomainError):
    code = ErrorCode.PHI_EGRESS_NOT_ACKNOWLEDGED


class PhiMinimizationUnavailableError(DependencyUnavailableError):
    code = ErrorCode.PHI_MINIMIZATION_UNAVAILABLE


class PhiMinimizationRequiredError(DomainError):
    code = ErrorCode.PHI_MINIMIZATION_REQUIRED


class PhiMinimizationFailedError(DomainError):
    code = ErrorCode.PHI_MINIMIZATION_FAILED

    def __init__(self) -> None:
        super().__init__("PHI minimization failed closed.")


class AudioEgressNotPermittedError(DomainError):
    code = ErrorCode.AUDIO_EGRESS_NOT_PERMITTED


class CredentialExpiredError(DomainError):
    code = ErrorCode.CREDENTIAL_EXPIRED


@dataclass(frozen=True, slots=True)
class ErrorCodeSystemConcept:
    """One concept in the published ``CodeSystem/errors``."""

    code: str
    display: str
    http_status: int
    issue_type: str
    category: str
    retryable: bool
    properties: dict[str, str] = field(default_factory=dict)


def error_code_system() -> dict[str, object]:
    """Build the FHIR ``CodeSystem`` resource that publishes this catalogue."""
    return {
        "resourceType": "CodeSystem",
        "id": "fhirbridge-errors",
        "url": ERROR_CODE_SYSTEM,
        "name": "FhirbridgeErrorCodes",
        "title": "fhirbridge error codes",
        "status": "active",
        "experimental": False,
        "content": "complete",
        "caseSensitive": True,
        "valueSet": f"{ERROR_CODE_SYSTEM}/vs",
        "description": (
            "Stable machine-readable error codes returned by the fhirbridge API. "
            "Codes are part of the public API contract; they are added, never renamed."
        ),
        "property": [
            {
                "code": "http-status",
                "type": "integer",
                "description": "HTTP status returned with this code.",
            },
            {
                "code": "issue-type",
                "type": "code",
                "description": "OperationOutcome.issue.code used for this error.",
            },
            {
                "code": "category",
                "type": "code",
                "description": "domain (OperationOutcome) or platform (JSON envelope).",
            },
            {
                "code": "retryable",
                "type": "boolean",
                "description": "Whether an unchanged retry may succeed.",
            },
        ],
        "count": len(ERROR_SPECS),
        "concept": [
            {
                "code": str(spec.code),
                "display": spec.title,
                "property": [
                    {"code": "http-status", "valueInteger": spec.http_status},
                    {"code": "issue-type", "valueCode": spec.issue_type},
                    {"code": "category", "valueCode": str(spec.category)},
                    {"code": "retryable", "valueBoolean": spec.retryable},
                ],
            }
            for spec in sorted(ERROR_SPECS.values(), key=lambda s: s.code)
        ],
    }


__all__ = [
    "ERROR_CODE_SYSTEM",
    "ERROR_SPECS",
    "AudioEgressNotPermittedError",
    "BudgetExceededError",
    "CredentialExpiredError",
    "DependencyUnavailableError",
    "DomainError",
    "EgressBlockedError",
    "ErrorCategory",
    "ErrorCode",
    "ErrorSpec",
    "FhirbridgeError",
    "ForbiddenError",
    "IdempotencyConflictError",
    "IgNotLoadedError",
    "InsecureTransportError",
    "InvalidFhirResourceError",
    "InvalidRequestError",
    "LlmAuthFailedError",
    "LlmContentFilteredError",
    "LlmContextExceededError",
    "LlmCredentialsRequiredError",
    "LlmQuotaExhaustedError",
    "LlmRateLimitedError",
    "LlmSchemaViolationError",
    "ModelNotQualifiedError",
    "NotFoundError",
    "NotImplementedInV1Error",
    "PayloadTooLargeError",
    "PhiEgressNotAcknowledgedError",
    "PhiMinimizationFailedError",
    "PhiMinimizationRequiredError",
    "PhiMinimizationUnavailableError",
    "PlatformError",
    "SafeContext",
    "TargetUnavailableError",
    "TerminologyUnavailableError",
    "UnauthenticatedError",
    "UnreadableDocumentError",
    "UnsupportedFhirVersionError",
    "UnsupportedMediaTypeError",
    "UnsupportedResourceTypeError",
    "ValidatorUnavailableError",
    "error_code_system",
]
