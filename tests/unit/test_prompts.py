"""The composed prompt set is a pinned artifact (principle 2.8).

A verdict names the prompt set that produced it by version. If the composition
can be edited without the version moving, that name is a lie. This test pins
the content hash: any edit to a prompt or a model-facing rule fails here until
the author bumps both the hash and ``PROMPT_SET_VERSION``.
"""

from __future__ import annotations

import pytest
from fhiratwill.conversion.prompts import NARRATIVE_TO_ENTITIES as CORE_NARRATIVE_TO_ENTITIES

from fhirbridge.llm.extraction_rules import extraction_rules_text
from fhirbridge.llm.prompts import (
    DICTATION_TRANSCRIBE,
    NARRATIVE_TO_ENTITIES,
    PROMPT_SET,
    PROMPT_SET_VERSION,
    compose_extraction_system,
    prompt_set_fingerprint,
)

PINNED_FINGERPRINT = "61aaa24b2b7cfa548902a7588d9c45da91c9ea3b302a68773eff45951883bfe2"


def test_the_prompt_set_has_not_drifted_from_its_pinned_hash() -> None:
    assert prompt_set_fingerprint() == PINNED_FINGERPRINT, (
        "A prompt template or extraction rule changed. Bump PROMPT_SET_VERSION "
        "and update PINNED_FINGERPRINT to the value printed by prompt_set_fingerprint()."
    )


def test_the_fingerprint_is_deterministic() -> None:
    assert prompt_set_fingerprint() == prompt_set_fingerprint()


def test_the_version_is_stamped_and_the_set_is_populated() -> None:
    assert PROMPT_SET_VERSION == "v5.4.0"
    assert NARRATIVE_TO_ENTITIES.id in PROMPT_SET
    assert DICTATION_TRANSCRIBE.id in PROMPT_SET


def test_the_user_template_renders_the_narrative() -> None:
    rendered = NARRATIVE_TO_ENTITIES.render_user(narrative="chest pain")

    assert "chest pain" in rendered


def test_the_extraction_rule_pack_is_embedded() -> None:
    assert "Extraction rules" in NARRATIVE_TO_ENTITIES.system
    assert extraction_rules_text() in NARRATIVE_TO_ENTITIES.system


def test_the_rules_come_before_the_catalog() -> None:
    system = NARRATIVE_TO_ENTITIES.system

    assert system.index("Extraction rules") < system.index("Catalog:")


def test_composition_fails_closed_if_the_core_catalog_heading_disappears() -> None:
    with pytest.raises(RuntimeError, match="Catalog heading"):
        compose_extraction_system("no catalog here", extraction_rules_text())


def test_the_composed_prompt_is_not_the_bare_core_prompt() -> None:
    assert NARRATIVE_TO_ENTITIES.system != CORE_NARRATIVE_TO_ENTITIES.system
    assert extraction_rules_text() not in CORE_NARRATIVE_TO_ENTITIES.system


def test_every_prompt_is_ascii() -> None:
    for template in PROMPT_SET.values():
        offenders = sorted({char for char in template.system if ord(char) > 127})
        assert not offenders, f"{template.id} system prompt contains {offenders}"
        offenders = sorted({char for char in template.user_template if ord(char) > 127})
        assert not offenders, f"{template.id} user template contains {offenders}"
