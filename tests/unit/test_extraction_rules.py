"""The extraction rule pack.

A rule is only worth its tokens if the model can act on it and the pipeline can
carry the result. The pack is composed onto the published core prompt; these
tests lock the safety-critical guidance and the catalog contract so a silent
drop cannot happen again.
"""

from __future__ import annotations

import re

import pytest
from fhiratwill import assemble_bundle
from fhiratwill.conversion import parse_entities
from fhiratwill.conversion.prompts import resource_catalog_text

from fhirbridge.llm.extraction_rules import (
    AGE_IS_NOT_A_BIRTH_DATE,
    EXTRACTION_RULES,
    NEGATED_AND_ATTRIBUTED_FINDINGS,
    ExtractionRule,
    extraction_rules_text,
)
from fhirbridge.llm.prompts import NARRATIVE_TO_ENTITIES

RULE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
ELEMENT_PATH = re.compile(r"\b([A-Z][A-Za-z]+)\.([a-zA-Z][a-zA-Z0-9]*)\b")


def _catalog() -> dict[str, set[str]]:
    parsed: dict[str, set[str]] = {}
    for line in resource_catalog_text().splitlines():
        resource_type, _, keys = line.partition(":")
        parsed[resource_type.strip()] = {key.strip() for key in keys.split(",") if key.strip()}
    return parsed


CATALOG = _catalog()


@pytest.fixture(params=EXTRACTION_RULES, ids=lambda rule: rule.id)
def rule(request: pytest.FixtureRequest) -> ExtractionRule:
    return request.param


class TestPackShape:
    def test_the_pack_is_populated(self) -> None:
        assert len(EXTRACTION_RULES) >= 1

    def test_rule_ids_are_unique(self) -> None:
        ids = [item.id for item in EXTRACTION_RULES]

        assert len(ids) == len(set(ids))

    def test_every_rule_is_fully_populated(self, rule: ExtractionRule) -> None:
        assert RULE_ID.match(rule.id), rule.id
        assert rule.title.strip()
        assert rule.guidance.strip()
        assert rule.rationale.strip(), "a rule without a rationale cannot be reviewed"


class TestRulesMatchThePipeline:
    def test_declared_elements_are_in_the_catalog(self, rule: ExtractionRule) -> None:
        for path in rule.elements:
            resource_type, _, element = path.partition(".")
            allowed = CATALOG.get(resource_type)
            assert allowed is not None, f"{rule.id} names unknown resource type {resource_type}"
            assert element in allowed, f"{rule.id} names {path}, which is not in the catalog"

    def test_guidance_never_cites_an_element_outside_the_catalog(
        self, rule: ExtractionRule
    ) -> None:
        for resource_type, element in ELEMENT_PATH.findall(rule.guidance):
            allowed = CATALOG.get(resource_type)
            if allowed is None:
                continue
            assert element in allowed, (
                f"{rule.id} guidance cites {resource_type}.{element}, which the catalog "
                "does not allow; the whole extraction would be rejected"
            )


class TestRendering:
    def test_every_title_and_guidance_reaches_the_prompt(self, rule: ExtractionRule) -> None:
        rendered = extraction_rules_text()

        assert rule.title in rendered
        for line in rule.guidance.splitlines():
            assert line.strip() in rendered

    def test_rationales_are_never_sent_to_the_model(self, rule: ExtractionRule) -> None:
        assert rule.rationale not in extraction_rules_text()
        assert rule.rationale not in NARRATIVE_TO_ENTITIES.system

    def test_the_rules_are_numbered_in_declaration_order(self) -> None:
        rendered = extraction_rules_text()
        positions = [rendered.index(f"{index}. ") for index in range(1, len(EXTRACTION_RULES) + 1)]

        assert positions == sorted(positions)

    def test_the_pack_is_embedded_in_the_extraction_prompt(self) -> None:
        assert extraction_rules_text() in NARRATIVE_TO_ENTITIES.system


class TestSafetyCriticalContent:
    """These two rules prevent confident, undetectable clinical errors."""

    def test_the_age_rule_forbids_computing_a_birth_date(self) -> None:
        guidance = AGE_IS_NOT_A_BIRTH_DATE.guidance

        assert "never compute a birth date from" in guidance
        assert "Patient.birthDate" in guidance
        assert "`Age`" in guidance, "the rule must say where the age does go"

    def test_family_history_is_recorded_on_the_relative_not_the_patient(self) -> None:
        guidance = NEGATED_AND_ATTRIBUTED_FINDINGS.guidance

        assert "family member" in guidance
        assert "FamilyMemberHistory" in guidance
        assert "not `Condition`" in guidance

    def test_a_denial_is_recorded_as_refuted_rather_than_dropped(self) -> None:
        guidance = NEGATED_AND_ATTRIBUTED_FINDINGS.guidance

        assert "`refuted`" in guidance
        assert "Condition.verificationStatus" in NEGATED_AND_ATTRIBUTED_FINDINGS.elements


class TestFamilyAttributedFindings:
    """A relative's condition must never assemble into a patient Condition.

    Assembly cannot see 'father had': it wires Condition.subject to the only
    Patient. The signal has to be captured at extraction time.
    """

    def test_a_rule_following_extraction_does_not_create_a_patient_condition(self) -> None:
        entities = parse_entities(
            {
                "entities": [
                    {
                        "resourceType": "Patient",
                        "instance": "patient-1",
                        "keyword": "gender",
                        "value": "male",
                    }
                ]
            }
        )

        assembled = assemble_bundle(entities, seed="family-history")
        types = [entry["resource"]["resourceType"] for entry in assembled.bundle_dict["entry"]]

        assert "Condition" not in types

    def test_assembly_would_pin_a_leaked_family_condition_on_the_patient(self) -> None:
        """Keeps the load-bearing rationale honest: nothing downstream catches this."""
        entities = parse_entities(
            {
                "entities": [
                    {
                        "resourceType": "Patient",
                        "instance": "patient-1",
                        "keyword": "gender",
                        "value": "male",
                    },
                    {
                        "resourceType": "Condition",
                        "instance": "cond-colon",
                        "keyword": "code",
                        "value": "colon cancer",
                    },
                ]
            }
        )

        assembled = assemble_bundle(entities, seed="misattributed")
        condition = next(
            entry["resource"]
            for entry in assembled.bundle_dict["entry"]
            if entry["resource"]["resourceType"] == "Condition"
        )

        assert condition["subject"]["reference"].startswith("urn:uuid:")
        assert "verificationStatus" not in condition
        wired = [
            note
            for note in assembled.notes
            if note.resource_type == "Condition" and note.element == "subject"
        ]
        assert wired and wired[0].detail == "pointed at the only Patient"
