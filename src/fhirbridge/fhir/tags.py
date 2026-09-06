"""Provenance tags stamped onto generated resources (AGENTS.md 9.1 assemble).

Downstream consumers need to be able to tell, from the resource alone, that it
was machine-derived and how much scrutiny it has had. ``docs/clinical-safety.md``
documents the semantics these tags promise.
"""

from __future__ import annotations

from typing import Final

PROVENANCE_TAG_SYSTEM: Final[str] = "https://fhirbridge.org/CodeSystem/provenance-tags"

AI_DERIVED: Final[str] = "ai-derived"
"""The resource's content was proposed by a language model, not a human."""

HUMAN_REVIEWED: Final[str] = "human-reviewed"
"""A human reviewer accepted this resource through the review plane."""

UNQUALIFIED_MODEL: Final[str] = "unqualified-model"
"""Produced with a model below MIN_QUALIFICATION_TIER, acknowledged by the caller."""

NONDETERMINISM_RISK: Final[str] = "nondeterminism-risk"
"""Reproducibility could not be guaranteed for this resource (AGENTS.md 7.10)."""

MACHINE_INFERRED: Final[str] = "machine-inferred"
"""A required element was filled from a declared default, not from the source.

FHIR requires elements a narrative rarely states — ``Observation.status``,
``Encounter.class``, ``MedicationRequest.intent``. Deterministic assembly supplies
them from a reviewed constant table so the value is auditable and identical on
every run, but it is still not grounded in the source. This tag marks resources
carrying at least one such value; the per-element detail is reported alongside the
Bundle rather than embedded in it.

Narrower than :data:`AI_DERIVED` on purpose: structural inference that cannot be
wrong given the input, such as pointing ``subject`` at the only Patient in the
document, is reported but not tagged, so this tag keeps meaning "a required value
here was fabricated".
"""

MACHINE_CODED: Final[str] = "machine-coded"
"""A reviewed deterministic rule proposed a code and terminology verified it.

The source wording remains in ``CodeableConcept.text``. This tag says only how
the additional Coding was produced; it is not evidence of human review.
"""

ALL_TAGS: Final[tuple[str, ...]] = (
    AI_DERIVED,
    HUMAN_REVIEWED,
    UNQUALIFIED_MODEL,
    NONDETERMINISM_RISK,
    MACHINE_INFERRED,
    MACHINE_CODED,
)


def tag(code: str, display: str | None = None) -> dict[str, str]:
    """Build a ``meta.tag`` Coding."""
    coding = {"system": PROVENANCE_TAG_SYSTEM, "code": code}
    if display:
        coding["display"] = display
    return coding


__all__ = [
    "AI_DERIVED",
    "ALL_TAGS",
    "HUMAN_REVIEWED",
    "MACHINE_CODED",
    "MACHINE_INFERRED",
    "NONDETERMINISM_RISK",
    "PROVENANCE_TAG_SYSTEM",
    "UNQUALIFIED_MODEL",
    "tag",
]
