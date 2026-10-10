"""Extraction rules embedded in the entity-extraction system prompt.

These are the clinical instructions that cannot be expressed by the resource catalog
alone. The catalog says *which* elements exist; a rule says what to do when the
narrative's shape and FHIR's shape disagree — an age where FHIR wants a birth date, a
blood pressure where FHIR wants one number, a father's diagnosis where a ``Condition``
would assert the patient has it.

A rule pack is a reviewed, versioned artifact (AGENTS.md). Rules are composed
onto the published core extraction prompt in :mod:`fhirbridge.llm.prompts`, so
editing one changes ``prompt_set_fingerprint()`` and fails the pinning test until
``PROMPT_SET_VERSION`` moves with it. Adding a rule means appending to
:data:`EXTRACTION_RULES` and bumping that version; nothing else.

Two constraints bound what a rule may say, and both come from the pipeline:

- **A rule may only name elements in the catalog that assembly can actually build.**
  Directing the model at ``Observation.component`` would be useless: it is a backbone
  element with no single-string form, so every value sent there is dropped. The
  ``elements`` field on each rule is asserted against the catalog in tests.
- **A rule may not ask the model to invent a value.** Rules exist to stop guessing,
  not to license it. Where a fact cannot be represented, the rule says to leave the
  source wording so coercion refuses it and the assembly report names it — a reported
  gap is a better outcome than a plausible fabrication.

``rationale`` is reviewer-facing and is never sent to a model, so editing it does not
move the fingerprint. It records the failure the rule prevents, which is the part a
future reader needs to decide whether the rule still earns its tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ExtractionRule:
    """One clinical instruction added to the extraction prompt."""

    id: str
    """Stable identifier, used in review and in tests. Not sent to the model."""

    title: str
    """Short imperative heading, rendered as the rule's first line."""

    guidance: str
    """The model-facing instruction. Counts toward the prompt fingerprint."""

    elements: tuple[str, ...]
    """``ResourceType.element`` paths this rule steers toward or away from.

    Declared rather than inferred so a test can assert every one is in the catalog
    and is a datatype assembly can build.
    """

    rationale: str
    """Why this rule exists, for reviewers. Never sent to a model."""


AGE_IS_NOT_A_BIRTH_DATE: Final[ExtractionRule] = ExtractionRule(
    id="age-is-not-a-birth-date",
    title="An age is not a birth date",
    guidance=(
        "Never place an age in `Patient.birthDate`, and never compute a birth date from "
        "an age. Someone aged 62 at a visit on 2024-01-15 was born between 1961-01-16 "
        "and 1962-01-15, so even the year would be a guess.\n"
        "Record a stated age as its own Observation instance: `code` of `Age` and "
        "`valueQuantity` holding the number with its unit, such as `62 years`. Use "
        "`Patient.birthDate` only when the narrative states an actual date of birth."
    ),
    elements=("Patient.birthDate", "Observation.code", "Observation.valueQuantity"),
    rationale=(
        "An age is the most common demographic fact in a narrative and FHIR has nowhere "
        "in Patient to put it, so a model asked for birthDate does arithmetic and "
        "returns a date that looks sourced. Before this rule the age was extracted as "
        "Patient.birthDate, refused by coercion, and lost: faithful, but the number went "
        "missing. An Age Observation keeps it without asserting a date nobody stated."
    ),
)

ONE_MEASUREMENT_PER_VALUE: Final[ExtractionRule] = ExtractionRule(
    id="one-measurement-per-value",
    title="One measurement per value",
    guidance=(
        "Each `value` must hold exactly one measurement. A blood pressure of "
        "`128/82 mmHg` is two numbers and cannot go in one `valueQuantity`.\n"
        "Emit one Observation instance per component, each with its own `instance`, "
        "`code`, and `valueQuantity`: `obs-bp-systolic` with `Systolic blood pressure` "
        "and `128 mmHg`, then `obs-bp-diastolic` with `Diastolic blood pressure` and "
        "`82 mmHg`. Never separate a number from its unit, and never drop one half of a "
        "paired reading."
    ),
    elements=("Observation.code", "Observation.valueQuantity"),
    rationale=(
        "A compound reading is refused by the Quantity coercer, so without this rule a "
        "blood pressure is reported as unusable and both numbers are lost. FHIR would "
        "prefer a single Observation with two components, but component is a backbone "
        "element that the flat entity schema cannot express and assembly would drop; "
        "two Observations with distinct codes is valid R4 and is representable today."
    ),
)

RESOLVE_DATES_ONLY_AGAINST_A_STATED_ANCHOR: Final[ExtractionRule] = ExtractionRule(
    id="resolve-dates-only-against-a-stated-anchor",
    title="Resolve a relative date only against a stated anchor",
    guidance=(
        "Resolve a relative date only when the narrative itself states the date it is "
        "relative to, and only to the precision the phrase supports. `Three weeks before "
        "the 2024-01-15 visit` supports `2023-12`. `Recently`, `last winter`, and `a few "
        "months ago` support nothing and must be left as the source wording.\n"
        "Never anchor an offset to today's date, and never turn a vague phrase into a "
        "specific day."
    ),
    elements=(
        "Condition.onsetDateTime",
        "Encounter.period",
        "Observation.effectiveDateTime",
        "Procedure.performedPeriod",
    ),
    rationale=(
        "Today's date is not in the prompt and is not a fact about the patient, so an "
        "offset resolved against it is fiction. Partial precision is the FHIR-sanctioned "
        "way to say 'this month, not this day', and leaving a vague phrase intact routes "
        "it to the assembly report instead of into a resource."
    ),
)

NEGATED_AND_ATTRIBUTED_FINDINGS: Final[ExtractionRule] = ExtractionRule(
    id="negated-and-attributed-findings",
    title="Never turn a denial or a relative's history into a diagnosis",
    guidance=(
        "A negated or attributed finding must never become a positive assertion about "
        "this patient.\n"
        "For a condition the narrative explicitly denies or rules out, emit the "
        "Condition with its `code` and a `verificationStatus` of `refuted`, so the "
        "denial survives rather than vanishing.\n"
        "For a condition belonging to a family member, emit `FamilyMemberHistory`, "
        "not `Condition`. Put the relative in `relationship` (for example `father`) "
        "and the diagnosis in `condition`. A `Condition` would state that the patient "
        "has it."
    ),
    elements=(
        "Condition.code",
        "Condition.verificationStatus",
        "FamilyMemberHistory.relationship",
        "FamilyMemberHistory.condition",
    ),
    rationale=(
        "The highest-severity extraction error available: 'father had colon cancer' or "
        "'denies chest pain' becoming an active patient diagnosis. Nothing downstream "
        "catches it, because the resource is perfectly conformant. FamilyMemberHistory "
        "is in the catalog, so the relative's disease is recorded there instead of "
        "being dropped or pinned on the patient."
    ),
)

SPLIT_MEDICATION_PHRASES: Final[ExtractionRule] = ExtractionRule(
    id="split-medication-phrases",
    title="Split a medication phrase across its elements",
    guidance=(
        "Put the drug in `medicationCodeableConcept` and the dosing in "
        "`dosageInstruction`, rather than the whole phrase in the drug name. `Metformin "
        "500 mg by mouth twice daily` yields `medicationCodeableConcept` of `metformin` "
        "and `dosageInstruction` of `500 mg by mouth twice daily`.\n"
        "Keep `medicationCodeableConcept` to the drug, including its strength only when "
        "the strength identifies the product."
    ),
    elements=(
        "MedicationRequest.medicationCodeableConcept",
        "MedicationRequest.dosageInstruction",
    ),
    rationale=(
        "A medicationCodeableConcept reading 'metformin 500 mg by mouth twice daily' "
        "cannot be coded against RxNorm by any later terminology step, so the drug "
        "becomes unmatchable. The split costs nothing and is the difference between a "
        "codeable product and free text."
    ),
)

ONE_INSTANCE_PER_REAL_WORLD_THING: Final[ExtractionRule] = ExtractionRule(
    id="one-instance-per-real-world-thing",
    title="One instance per real-world thing",
    guidance=(
        "Two entities share an `instance` only when a single resource would carry both. "
        "Give every distinct measurement, condition, encounter, and medication its own "
        "`instance`.\n"
        "Never reuse an `instance` across two different `code` values, and never emit "
        "the same key twice for one `instance`; the second value is discarded."
    ),
    elements=("Observation.code",),
    rationale=(
        "Grouping is the one job only the model can do, and mis-grouping is the one "
        "error validation cannot detect: pairing 'heart rate' with '128/82 mmHg' yields "
        "a structurally valid Observation carrying the wrong clinical value. Worth "
        "restating as a rule even though the schema paragraph introduces the key."
    ),
)

PRESERVE_REDACTION_TOKENS: Final[ExtractionRule] = ExtractionRule(
    id="preserve-redaction-tokens",
    title="Preserve de-identification tokens exactly",
    guidance=(
        "Text such as `[[NAME_0123ABCDEF45]]` or `[[DATE_0123ABCDEF45]]` is an opaque "
        "de-identification token. Copy any such token exactly when it belongs in an "
        "extracted value. Never alter, complete, interpret, invent, or perform arithmetic "
        "on a token."
    ),
    elements=("Patient.name",),
    rationale=(
        "The service restores tokens locally after extraction. Any token mutation would "
        "either lose a grounded value or leave an identifier surrogate in the Bundle."
    ),
)

EXTRACTION_RULES: Final[tuple[ExtractionRule, ...]] = (
    AGE_IS_NOT_A_BIRTH_DATE,
    ONE_MEASUREMENT_PER_VALUE,
    RESOLVE_DATES_ONLY_AGAINST_A_STATED_ANCHOR,
    NEGATED_AND_ATTRIBUTED_FINDINGS,
    SPLIT_MEDICATION_PHRASES,
    ONE_INSTANCE_PER_REAL_WORLD_THING,
    PRESERVE_REDACTION_TOKENS,
)
"""The ordered rule pack. Append to extend, and bump ``PROMPT_SET_VERSION``."""


def extraction_rules_text() -> str:
    """Render the rule pack for the system prompt. Rationales are not included."""
    return "\n\n".join(
        "\n".join(
            (
                f"{index}. {rule.title}",
                *(f"   {line}" for line in rule.guidance.splitlines()),
            )
        )
        for index, rule in enumerate(EXTRACTION_RULES, start=1)
    )


__all__ = [
    "EXTRACTION_RULES",
    "ExtractionRule",
    "extraction_rules_text",
]
