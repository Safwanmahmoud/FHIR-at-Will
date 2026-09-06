from __future__ import annotations

import pytest

from fhirbridge.binding.bind import bind_bundle
from fhirbridge.binding.models import BindingAction
from fhirbridge.binding.rules import load_concepts
from fhirbridge.domain.errors import TerminologyUnavailableError
from fhirbridge.fhir.tags import MACHINE_CODED
from tests.fakes import FakeTerminologyClient


def _bundle() -> dict[str, object]:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "fullUrl": "urn:uuid:observation",
                "resource": {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"text": "Heart rate"},
                    "valueQuantity": {"value": 72, "unit": "beats/minute"},
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_binds_reviewed_vital_and_ucum_after_verification() -> None:
    result = await bind_bundle(_bundle(), client=FakeTerminologyClient())

    observation = result.bundle["entry"][0]["resource"]
    assert observation["code"]["coding"][0]["code"] == "8867-4"
    assert observation["code"]["text"] == "Heart rate"
    assert observation["valueQuantity"]["system"] == "http://unitsofmeasure.org"
    assert observation["valueQuantity"]["code"] == "/min"
    assert result.coverage.bound == 2
    assert any(item["code"] == MACHINE_CODED for item in observation["meta"]["tag"])


@pytest.mark.asyncio
async def test_refuses_invalid_candidate_and_preserves_text() -> None:
    terminology = FakeTerminologyClient(membership={"8867-4": False})
    result = await bind_bundle(_bundle(), client=terminology)

    observation = result.bundle["entry"][0]["resource"]
    assert "coding" not in observation["code"]
    assert observation["code"]["text"] == "Heart rate"
    assert any(note.action is BindingAction.UNBOUND for note in result.notes)


@pytest.mark.asyncio
async def test_terminology_outage_fails_closed() -> None:
    with pytest.raises(TerminologyUnavailableError):
        await bind_bundle(_bundle(), client=FakeTerminologyClient(unavailable=True))


def test_concept_table_version_must_match_the_build(tmp_path) -> None:
    source = tmp_path / "concepts.yaml"
    source.write_text("version: 999\nconcepts: []\nunits: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_concepts(str(source))
