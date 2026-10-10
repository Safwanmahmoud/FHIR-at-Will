"""Service integration for the framework-neutral :mod:`fhiratwill` core.

The algorithms live in the separately versioned PyPI package. This adapter keeps
the API's policy-enforcing gateway while composing the library's de-identification,
extraction, assembly, and terminology binding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fhiratwill import (
    AssembledBundle,
    DeclaredIdentifier,
    DeidentifyResult,
    DeidPolicy,
    ExtractionSchemaError,
    TerminologyClient,
    assemble_bundle,
)
from fhiratwill.conversion import parse_entities, parse_filled_resources
from fhiratwill.deid.core import minimize
from fhiratwill.terminology_binder import BoundBundle, bind_bundle

from fhirbridge.domain.errors import LlmSchemaViolationError
from fhirbridge.llm.gateway import LlmGateway, LlmResult
from fhirbridge.llm.invocation import LlmInvocation
from fhirbridge.llm.prompts import NARRATIVE_TO_ENTITIES
from fhirbridge.terminology.nn_index import get_index


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """What the shared pipeline produces from a narrative.

    ``extraction`` carries the model call's provenance (model, usage, cost,
    latency); ``assembled`` carries the Bundle and the PHI-free assembly notes.
    """

    assembled: AssembledBundle
    binding: BoundBundle
    extraction: LlmResult
    deid: DeidentifyResult


async def convert_narrative(
    text: str,
    *,
    gateway: LlmGateway,
    invocation: LlmInvocation,
    conversion_id: str,
    policy: DeidPolicy,
    terminology: TerminologyClient,
    declared_identifiers: Sequence[DeclaredIdentifier] = (),
) -> ConversionResult:
    """Extract grounded facts from ``text`` and assemble them into a FHIR Bundle.

    The Bundle's ``urn:uuid`` identifiers are seeded from ``conversion_id`` so the
    same narrative yields the same content on every run while staying distinct
    across conversions.
    """
    minimization = minimize(
        text,
        policy=policy,
        known_identifiers=declared_identifiers,
    )
    try:
        extraction = await gateway.complete_json(
            invocation,
            system_prompt=NARRATIVE_TO_ENTITIES.system,
            user_prompt=NARRATIVE_TO_ENTITIES.render_user(narrative=minimization.safe_text),
            minimization=minimization,
        )
        try:
            payload = extraction.resource
            if isinstance(payload, dict) and "resources" in payload:
                entities = parse_filled_resources(payload)
            else:
                entities = parse_entities(payload)
        except ExtractionSchemaError as exc:
            raise LlmSchemaViolationError(str(exc)) from exc
        restored = minimization.restore_entities(entities)
        assembled = assemble_bundle(restored, seed=conversion_id)
        binding = await bind_bundle(
            getattr(assembled, "bundle_dict", assembled.bundle),
            index=get_index(),
            client=terminology,
        )
        report = minimization.result()
        return ConversionResult(
            assembled=assembled,
            binding=binding,
            extraction=extraction,
            deid=report,
        )
    finally:
        minimization.close()


__all__ = ["ConversionResult", "convert_narrative"]
