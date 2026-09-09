"""``POST /v1/VOICE2FHIR``.

Dictated clinical audio in, an unvalidated FHIR Bundle out. This is ``NAR2FHIR``
with a transcription step in front: the audio is transcribed verbatim, and the
transcript is then run through the exact same grounded-extraction and deterministic
assembly path (:func:`fhirbridge.llm.conversion.convert_narrative`). Nothing about
the conversion changes because the narrative arrived as speech.

Two external calls happen, to two providers on two keys: dictation goes to a
speech-to-text provider via ``X-STT-*`` (Gemini by default; litellm cannot
transcribe through OpenRouter), and extraction goes to the model in ``X-LLM-*``.
Both are PHI egress and share the one ``X-PHI-Egress-Acknowledged`` header.

The audio and the transcript are PHI. The audio arrives in the request body as a
multipart upload, and the transcript is returned in the response body so a human
can check the dictation; neither ever reaches a log, URL, metric, or exception.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Response, UploadFile
from fhiratwill import AssemblyAction, DeidPolicy
from pydantic import ValidationError

from fhirbridge.api.auth import Scope
from fhirbridge.api.deps import (
    LlmGatewayDep,
    LlmInvocationDep,
    PrincipalDep,
    SettingsDep,
    SttInvocationDep,
    TerminologyDep,
)
from fhirbridge.api.routers.convert import (
    assembly_notes_of,
    binding_info_of,
    declared_identifiers_of,
    deid_info_of,
    llm_call_info_of,
)
from fhirbridge.api.schemas import (
    DictationCallInfo,
    KnownIdentifiers,
    VoiceConvertResponse,
)
from fhirbridge.domain.errors import (
    InvalidRequestError,
    PayloadTooLargeError,
    UnreadableDocumentError,
    UnsupportedMediaTypeError,
)
from fhirbridge.domain.ids import IdPrefix, new_id
from fhirbridge.llm.conversion import convert_narrative

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["conversion"])

_VOICE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Missing LLM or STT credentials, or a provider rejected the key."},
    413: {"description": "The audio exceeds the configured upload size limit."},
    415: {"description": "The uploaded file is not a recognized audio format."},
    422: {
        "description": (
            "The audio yielded no transcribable speech, the model output could not be "
            "parsed, a model is not qualified, or the budget would be exceeded."
        )
    },
    429: {"description": "A provider rate-limited the request. Retryable."},
    451: {"description": "A target provider host is blocked by egress policy."},
}

# litellm's ``input_audio`` block carries a bare format token, not a MIME type. Only
# formats a caller can plausibly capture and common dictation providers accept are
# listed; anything else is refused up front rather than sent and rejected downstream.
_FORMAT_BY_CONTENT_TYPE: dict[str, str] = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "application/ogg": "ogg",
    "audio/aiff": "aiff",
    "audio/x-aiff": "aiff",
    "audio/mp4": "mp4",
    "audio/x-m4a": "mp4",
    "audio/m4a": "mp4",
    "audio/webm": "webm",
}
_FORMAT_BY_EXTENSION: dict[str, str] = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".m4a": "mp4",
    ".mp4": "mp4",
    ".aac": "aac",
    ".flac": "flac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "ogg",
    ".aif": "aiff",
    ".aiff": "aiff",
    ".webm": "webm",
}


def _audio_format(content_type: str | None, filename: str | None) -> str:
    """Resolve the litellm audio ``format`` token, or refuse the upload.

    The declared content type is authoritative; a filename extension is the
    fallback for clients that upload as ``application/octet-stream``.
    """
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        resolved = _FORMAT_BY_CONTENT_TYPE.get(base)
        if resolved is not None:
            return resolved
    if filename and "." in filename:
        suffix = filename[filename.rindex(".") :].lower()
        resolved = _FORMAT_BY_EXTENSION.get(suffix)
        if resolved is not None:
            return resolved
    raise UnsupportedMediaTypeError(
        "Upload audio as one of wav, mp3, m4a/mp4, aac, flac, ogg/opus, aiff, or webm.",
        safe_context={"content_type": (content_type or "").split(";", 1)[0].strip().lower()},
    )


@router.post(
    "/VOICE2FHIR",
    summary="Transcribe dictated audio and convert it to an unvalidated FHIR Bundle (BYOK)",
    response_model=VoiceConvertResponse,
    responses=_VOICE_ERROR_RESPONSES,
)
async def voice2fhir(
    principal: PrincipalDep,
    invocation: LlmInvocationDep,
    stt: SttInvocationDep,
    gateway: LlmGatewayDep,
    settings: SettingsDep,
    terminology: TerminologyDep,
    response: Response,
    audio: Annotated[UploadFile, File(description="The dictated clinical audio to convert.")],
    known_identifiers: Annotated[
        str | None,
        Form(
            description=(
                "Optional JSON object matching KnownIdentifiers. It is PHI and is processed "
                "only in this request body."
            )
        ),
    ] = None,
) -> VoiceConvertResponse:
    """Transcribe ``audio`` to text, then run the narrative-to-FHIR pipeline on it."""
    principal.require(Scope.CONVERSIONS_WRITE)
    conversion_id = new_id(IdPrefix.CONVERSION)

    media_format = _audio_format(audio.content_type, audio.filename)
    raw = await audio.read()
    if len(raw) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            "The uploaded audio exceeds the configured maximum size.",
            safe_context={"max_bytes": settings.max_upload_bytes},
        )
    if not raw:
        raise UnreadableDocumentError("The uploaded audio is empty.")

    try:
        declared = (
            KnownIdentifiers.model_validate_json(known_identifiers)
            if known_identifiers
            else KnownIdentifiers()
        )
    except ValidationError as exc:
        raise InvalidRequestError("known_identifiers must be a valid JSON object.") from exc

    dictation = await gateway.transcribe(stt, audio=raw, media_format=media_format)
    result = await convert_narrative(
        dictation.text,
        gateway=gateway,
        invocation=invocation,
        conversion_id=conversion_id,
        policy=DeidPolicy(mode=settings.deid_mode, profile=settings.deid_profile),
        terminology=terminology,
        declared_identifiers=declared_identifiers_of(declared),
    )
    assembled = result.assembled

    logger.info(
        "voice_conversion_completed",
        extra={
            # Identifiers, counts, durations only. The audio, transcript, and bundle
            # are PHI and never reach a log (principle 2.6).
            "conversion_id": conversion_id,
            "actor_id": principal.actor_id,
            "stt_model": dictation.model,
            "model": result.extraction.model,
            "audio_bytes": len(raw),
            "audio_format": media_format,
            "resource_count": len(assembled.bundle["entry"]),
            "inferred_count": sum(
                1 for note in assembled.notes if note.action is AssemblyAction.INFERRED
            ),
            "dropped_count": sum(
                1 for note in assembled.notes if note.action is AssemblyAction.DROPPED
            ),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return VoiceConvertResponse(
        conversion_id=conversion_id,
        bundle=result.binding.bundle,
        validated=False,
        assembly=assembly_notes_of(assembled),
        binding=binding_info_of(result.binding),
        llm=llm_call_info_of(result.extraction, invocation),
        deid=deid_info_of(result.deid),
        transcript=dictation.text,
        transcription=DictationCallInfo(
            provider=stt.provider,
            model=dictation.model,
            usage=dictation.usage,
            cost_usd=float(dictation.cost_usd) if dictation.cost_usd is not None else None,
            latency_ms=dictation.latency_ms,
        ),
    )


__all__ = ["router"]
