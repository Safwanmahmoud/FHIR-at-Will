"""Request and response schemas for the M0/M1 endpoints.

Every field carries a description: these models are the OpenAPI document, which
is the contract clients use to validate requests and responses.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fhiratwill import (
    AssemblyAction,
    FailurePolicy,
    ValidationReport,
)
from fhiratwill.terminology_binder import BindingAction
from fhiratwill.validation import IssueSeverity, ValidationLayer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fhirbridge.delivery.models import DeliveryReceipt, WritePlan

FhirResource = dict[str, Any]


class ValidateRequest(BaseModel):
    """Body of ``POST /v1/validate``.

    A bare FHIR resource (anything with a top-level ``resourceType``) is also
    accepted and is rewritten into this envelope with every option defaulted, so
    an existing FHIR client can post to this endpoint without learning a wrapper
    schema.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_resource(cls, value: Any) -> Any:
        if isinstance(value, dict) and "resourceType" in value and "resource" not in value:
            return {"resource": value}
        return value

    resource: FhirResource = Field(
        description=(
            "The FHIR R4 resource or Bundle to validate. Sent in the body, never in a "
            "query parameter, because it may contain PHI."
        )
    )
    profiles: list[str] = Field(
        default_factory=list,
        description=(
            "Profile canonical URLs to validate against, in addition to any declared in "
            "meta.profile. A profile the validator cannot resolve fails the request "
            "rather than passing silently."
        ),
        examples=[["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"]],
    )
    layers: list[ValidationLayer] | None = Field(
        default=None,
        description=(
            "Restrict the cascade to these layers. Omit to run every layer that can run. "
            "Layers you exclude are reported as skipped, never as passed."
        ),
    )
    severity_overrides: dict[str, IssueSeverity] = Field(
        default_factory=dict,
        description="Per-rule severity overrides for L5 plausibility, keyed by rule id.",
    )
    max_terminology_checks: Annotated[int, Field(ge=1, le=2000)] = Field(
        default=500,
        description=(
            "Cap on distinct $validate-code calls for L3. Reaching the cap is reported "
            "as a coverage note on the layer, not as a pass."
        ),
    )


class ConvertRequest(BaseModel):
    """Body of ``POST /v1/NAR2FHIR``.

    The clinical narrative is sent in the body, never a query parameter, because
    it is PHI (principle 2.6). The generated bundle is returned unvalidated.

    There is deliberately no ``profiles`` field. Assembly is deterministic and
    validates nothing, so it cannot honor a profile request; stamping
    ``meta.profile`` on that basis would be an unverified conformance claim. Pass
    profiles to ``POST /v1/validate`` instead.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description="The clinical narrative to convert into FHIR. Sent in the body as PHI.",
    )
    known_identifiers: KnownIdentifiers = Field(
        default_factory=lambda: KnownIdentifiers(),
        description=(
            "Identifiers the caller already knows, used for deterministic exact matching "
            "before model egress. These values are PHI and belong only in this body."
        ),
    )


class KnownIdentifiers(BaseModel):
    """Caller-declared PHI used to make de-identification deterministic."""

    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    medical_record_numbers: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    account_numbers: list[str] = Field(default_factory=list)
    license_numbers: list[str] = Field(default_factory=list)
    device_identifiers: list[str] = Field(default_factory=list)


class DeidentifyRequest(BaseModel):
    """Body of ``POST /v1/deidentify``."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description="The clinical narrative to de-identify. It is PHI and belongs in the body.",
    )
    known_identifiers: KnownIdentifiers = Field(default_factory=KnownIdentifiers)


class DeidentifyResponse(BaseModel):
    """A minimized narrative and PHI-free processing evidence."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The narrative with detected identifiers replaced by tokens.")
    deid: DeidInfo


class LlmCallInfo(BaseModel):
    """Aggregate model-call provenance. No prompt or completion content (principle 2.6)."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = Field(description="The model id reported by the extraction call.")
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="Token counts reported by the provider, when available.",
    )
    cost_usd: float | None = Field(
        default=None, description="Provider-reported cost of this call, when pricing is known."
    )
    latency_ms: int = 0
    qualification_tier: str = Field(description="This build's qualification tier for the model.")


class AssemblyNote(BaseModel):
    """One element that deterministic assembly could not simply place.

    PHI-free by construction: an entry index, an element name, and a reason, never
    a value. This list is how a caller separates a grounded element from one that
    was filled in — a distinction the resource itself cannot express.
    """

    model_config = ConfigDict(extra="forbid")

    entry_index: Annotated[int, Field(ge=0)] = Field(
        description="Index into bundle.entry of the resource this note is about."
    )
    resource_type: str = Field(description="Resource type of that entry.")
    element: str = Field(description="The FHIR element name this note concerns.")
    action: AssemblyAction = Field(description="What assembly did about the element.")
    detail: str = Field(
        description="Why assembly took this action. Never contains an extracted value."
    )


class BindingNote(BaseModel):
    """PHI-free evidence for one terminology binding attempt."""

    model_config = ConfigDict(extra="forbid")

    entry_index: Annotated[int, Field(ge=0)]
    resource_type: str
    element: str
    action: BindingAction
    value_set: str | None = None
    candidate_count: Annotated[int, Field(ge=0)] = 0
    detail: str


class BindRequest(BaseModel):
    """Body of ``POST /v1/bind``.

    The phrase is sent in the body, never a query parameter, because it may
    describe a patient's clinical state.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500, description="Free-text phrase to bind.")
    k: Annotated[int, Field(ge=1, le=20)] = Field(default=5, description="Neighbor count.")
    leaves_only: bool = Field(
        default=True,
        description="Prefer billable/leaf catalog rows when the dictionary marks them.",
    )
    min_score: float | None = Field(
        default=None,
        description="If set, abstain when the nearest cosine is below this threshold.",
    )
    dictionaries: list[str] = Field(
        default_factory=list,
        description="Restrict search to these dictionary names. Empty means all loaded.",
    )


class BindNeighbor(BaseModel):
    """One nearest-neighbor catalog hit."""

    model_config = ConfigDict(extra="forbid")

    dictionary: str
    system: str
    code: str
    display: str
    matched_text: str
    leaf: bool
    score: float
    coding: dict[str, str]


class BindResponse(BaseModel):
    """Body of ``POST /v1/bind``."""

    model_config = ConfigDict(extra="forbid")

    query: str
    coding: dict[str, str] | None = None
    confidence: float
    score_mean: float
    score_std: float
    neighbors: list[BindNeighbor] = Field(default_factory=list)


class BindingInfo(BaseModel):
    """Binding coverage and the versioned evidence supporting it."""

    model_config = ConfigDict(extra="forbid")

    table_version: str
    eligible: Annotated[int, Field(ge=0)]
    bound: Annotated[int, Field(ge=0)]
    unbound: Annotated[int, Field(ge=0)]
    ambiguous: Annotated[int, Field(ge=0)]
    notes: list[BindingNote] = Field(default_factory=list)


class DeidInfo(BaseModel):
    """PHI-free evidence about narrative minimization; never a compliance verdict."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    profile: str
    ruleset_version: str
    detections: dict[str, int] = Field(default_factory=dict)
    replacements: int = 0
    restored: int = 0
    residual_risk: Literal["not_assessed"] = "not_assessed"


class ConvertResponse(BaseModel):
    """Body of ``POST /v1/NAR2FHIR``."""

    model_config = ConfigDict(extra="forbid")

    conversion_id: str = Field(description="Opaque correlation id for this conversion.")
    bundle: FhirResource = Field(description="The generated FHIR R4 Bundle.")
    validated: Literal[False] = Field(
        default=False,
        description=("Always false. Submit the Bundle to POST /v1/validate before trusting it."),
    )
    assembly: list[AssemblyNote] = Field(
        default_factory=list,
        description=(
            "Every element assembly dropped, inferred, wired, or found in conflict. An "
            "empty list means each extracted fact mapped cleanly onto a typed element. "
            "Resources with an 'inferred' note also carry the machine-inferred tag."
        ),
    )
    binding: BindingInfo
    llm: LlmCallInfo
    deid: DeidInfo


class DictationCallInfo(BaseModel):
    """Provenance for the speech-to-text call. No audio or transcript (principle 2.6).

    Carries no qualification tier: dictation is a verbatim capture step and the tier
    registry ranks clinical-reasoning models, not transcription models, so a tier
    here would be a claim the build does not make.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = Field(description="The model id reported by the transcription call.")
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="Token counts reported by the provider, when available.",
    )
    cost_usd: float | None = Field(
        default=None, description="Provider-reported cost of this call, when pricing is known."
    )
    latency_ms: int = 0


class VoiceConvertResponse(ConvertResponse):
    """Body of ``POST /v1/VOICE2FHIR``.

    Everything ``POST /v1/NAR2FHIR`` returns, plus the transcript the model heard
    and that call's provenance. The transcript is returned on purpose: dictation can
    mishear a clinically decisive word (``no chest pain`` becoming ``chest pain``),
    and a reviewer cannot catch that from the Bundle alone. It is PHI, so it appears
    only in this response body, never in a log, URL, or metric.
    """

    transcript: str = Field(
        description=(
            "The verbatim transcript the dictation model produced, which was the input to "
            "extraction. Review it against the audio before trusting the Bundle."
        )
    )
    transcription: DictationCallInfo


class SubjectContextRequest(BaseModel):
    """Destination-native chart identity; never inferred from narrative."""

    model_config = ConfigDict(extra="forbid")

    patient_ref: str
    encounter_ref: str | None = None
    author_ref: str | None = None
    encounter_start: str | None = None


class WritePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversion_id: str
    bundle: FhirResource
    context: SubjectContextRequest
    profiles: list[str] = Field(default_factory=list)
    max_terminology_checks: Annotated[int, Field(ge=1, le=2000)] = 500


class DeliverRequest(WritePlanRequest):
    human_attested: bool = False
    reviewer_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._-]{1,64}$",
        description="Opaque reviewer identifier; never a name or email address.",
    )


class WritePlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation: ValidationReport
    plan: WritePlan


class DeliverResponse(WritePlanResponse):
    receipt: DeliveryReceipt


class TargetInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    failure_policy: FailurePolicy


class TargetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[TargetInfo]


class VersionResponse(BaseModel):
    """Body of ``GET /version`` — the pins required by principle 2.8."""

    model_config = ConfigDict(extra="forbid")

    service: str
    version: str = Field(description="fhirbridge code version.")
    fhir_version: str
    typed_model_fhir_version: str = Field(
        description="FHIR version of the typed models used for L1 structural validation."
    )
    prompt_set_version: str
    deid_ruleset_version: str
    fact_schema_version: str
    validation_report_schema_version: str
    binding_table_version: str
    target_descriptor_version: str
    delivery_plan_schema_version: str
    ig_packages: list[str]
    validator_version: str | None = None
    environment: str


class DependencyStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["up", "down", "degraded"]
    detail: str | None = None
    latency_ms: int | None = None
    version: str | None = None


class DependencyHealthResponse(BaseModel):
    """Body of ``GET /v1/health/dependencies``."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["up", "down", "degraded"] = Field(
        description=(
            "Worst status across dependencies. 'degraded' means the service is reachable "
            "but cannot make conformance claims, e.g. a required IG is not loaded."
        )
    )
    dependencies: list[DependencyStatus]


class ReadyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    dependencies: list[DependencyStatus] = Field(default_factory=list)


class LiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class CapabilitiesResponse(BaseModel):
    """Body of ``GET /v1/capabilities`` — what this build can actually do."""

    model_config = ConfigDict(extra="forbid")

    service: str
    version: str
    fhir_versions: list[str]
    ig_packages: list[str]
    validation_layers: list[dict[str, Any]]
    endpoints_implemented: list[str]
    endpoints_not_implemented: list[str]
    llm_required: bool = Field(
        description="False for this build: no endpoint here calls an LLM (milestone M1)."
    )
    local_only_mode: bool
    credential_storage: str
    min_qualification_tier: str
    deid_mode: str
    deid_profile: str
    deid_allow_audio_egress: bool
    delivery_mode: str


__all__ = [
    "AssemblyNote",
    "BindNeighbor",
    "BindRequest",
    "BindResponse",
    "BindingInfo",
    "BindingNote",
    "CapabilitiesResponse",
    "ConvertRequest",
    "ConvertResponse",
    "DeidInfo",
    "DeidentifyRequest",
    "DeidentifyResponse",
    "DeliverRequest",
    "DeliverResponse",
    "DependencyHealthResponse",
    "DependencyStatus",
    "DictationCallInfo",
    "KnownIdentifiers",
    "LiveResponse",
    "LlmCallInfo",
    "ReadyResponse",
    "SubjectContextRequest",
    "TargetInfo",
    "TargetsResponse",
    "ValidateRequest",
    "VersionResponse",
    "VoiceConvertResponse",
    "WritePlanRequest",
    "WritePlanResponse",
]
