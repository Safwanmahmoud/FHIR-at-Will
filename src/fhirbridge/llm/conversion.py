"""The narrative-to-FHIR core, shared by every entry point.

One grounded model call extracts catalog-constrained facts; assembly into FHIR is
then deterministic (:mod:`fhirbridge.fhir.assemble`). ``POST /v1/NAR2FHIR`` runs
this on caller-supplied text; ``POST /v1/VOICE2FHIR`` runs it on a transcript it
produced first. Keeping the pipeline here means both endpoints share one code path
and cannot drift, and the transcription step is a strict prefix rather than a fork.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fhirbridge.binding.bind import bind_bundle
from fhirbridge.binding.models import BoundBundle
from fhirbridge.deid.detectors import DeclaredIdentifier
from fhirbridge.deid.minimize import DeidReport, minimize
from fhirbridge.deid.policy import DeidPolicy
from fhirbridge.fhir.assemble import AssembledBundle, assemble_bundle
from fhirbridge.llm.gateway import LlmGateway, LlmResult
from fhirbridge.llm.invocation import LlmInvocation
from fhirbridge.llm.nar2fhir import parse_entities
from fhirbridge.llm.prompts import NARRATIVE_TO_ENTITIES
from fhirbridge.terminology.interface import TerminologyClient


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """What the shared pipeline produces from a narrative.

    ``extraction`` carries the model call's provenance (model, usage, cost,
    latency); ``assembled`` carries the Bundle and the PHI-free assembly notes.
    """

    assembled: AssembledBundle
    binding: BoundBundle
    extraction: LlmResult
    deid: DeidReport


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
    minimization = minimize(text, policy=policy, declared=declared_identifiers)
    try:
        extraction = await gateway.complete_json(
            invocation,
            system_prompt=NARRATIVE_TO_ENTITIES.system,
            user_prompt=NARRATIVE_TO_ENTITIES.render_user(narrative=minimization.safe_text),
            minimization=minimization,
        )
        entities = parse_entities(extraction.resource)
        restored = minimization.restore_entities(entities)
        assembled = assemble_bundle(restored, seed=conversion_id)
        binding = await bind_bundle(assembled.bundle, client=terminology)
        report = minimization.report()
        return ConversionResult(
            assembled=assembled,
            binding=binding,
            extraction=extraction,
            deid=report,
        )
    finally:
        minimization.close()


__all__ = ["ConversionResult", "convert_narrative"]
