"""The ``meta.tag`` vocabulary (AGENTS.md 9.1, 18).

These codes are a promise to downstream consumers: a resource carrying
``ai-derived`` was proposed by a model, and one carrying ``unqualified-model``
was produced with a model the deployment does not vouch for. ``docs/clinical-
safety.md`` documents those semantics, so the codes are API surface — renaming
one silently changes what a receiving system believes about a chart.
"""

from __future__ import annotations

import pytest

from fhirbridge.fhir.tags import (
    AI_DERIVED,
    ALL_TAGS,
    HUMAN_REVIEWED,
    MACHINE_CODED,
    MACHINE_INFERRED,
    NONDETERMINISM_RISK,
    PROVENANCE_TAG_SYSTEM,
    UNQUALIFIED_MODEL,
    tag,
)


def test_the_required_tags_exist_with_their_published_codes() -> None:
    assert AI_DERIVED == "ai-derived"
    assert HUMAN_REVIEWED == "human-reviewed"
    assert UNQUALIFIED_MODEL == "unqualified-model"
    assert NONDETERMINISM_RISK == "nondeterminism-risk"
    assert MACHINE_INFERRED == "machine-inferred"
    assert MACHINE_CODED == "machine-coded"


def test_all_tags_lists_every_code_exactly_once() -> None:
    assert len(ALL_TAGS) == len(set(ALL_TAGS))
    assert set(ALL_TAGS) == {
        AI_DERIVED,
        HUMAN_REVIEWED,
        UNQUALIFIED_MODEL,
        NONDETERMINISM_RISK,
        MACHINE_INFERRED,
        MACHINE_CODED,
    }


@pytest.mark.parametrize("code", ALL_TAGS)
def test_a_tag_is_a_complete_coding(code: str) -> None:
    """A bare code with no system is ambiguous to a receiving system."""
    coding = tag(code)

    assert coding == {"system": PROVENANCE_TAG_SYSTEM, "code": code}


def test_a_display_is_included_when_given() -> None:
    coding = tag(AI_DERIVED, "Proposed by a language model")

    assert coding["display"] == "Proposed by a language model"


def test_an_empty_display_is_omitted_rather_than_emitted_blank() -> None:
    assert "display" not in tag(AI_DERIVED, "")


def test_each_call_returns_a_fresh_dict() -> None:
    """Builders mutate what they are handed; a shared dict would leak across resources."""
    first = tag(AI_DERIVED)
    first["code"] = "tampered"

    assert tag(AI_DERIVED)["code"] == AI_DERIVED
