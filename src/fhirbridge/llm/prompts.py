"""Service-side composition of the reviewed extraction rule pack onto the core prompts.

The published ``fhiratwill`` prompt is the catalog and the general extraction
contract. The rule pack that used to be rendered into that prompt did not move
with the core (see GitHub issue #1), so this module composes it back on and pins
the result. A verdict names :data:`~fhirbridge.version.PROMPT_SET_VERSION`;
:func:`prompt_set_fingerprint` is the machine check that the composed templates
have not drifted from the version they claim to be.
"""

from __future__ import annotations

import hashlib
from typing import Final

from fhiratwill.conversion.prompts import DICTATION_TRANSCRIBE, PromptTemplate
from fhiratwill.conversion.prompts import NARRATIVE_TO_ENTITIES as CORE_NARRATIVE_TO_ENTITIES

from fhirbridge.llm.extraction_rules import extraction_rules_text
from fhirbridge.version import PROMPT_SET_VERSION

_CATALOG_HEADING = "\n\nCatalog:\n"
_RULES_HEADING = (
    "Extraction rules. These override the general guidance above where they conflict:\n\n"
)


def compose_extraction_system(core_system: str, rules: str) -> str:
    """Insert the reviewed pack before the catalog, not after it.

    The catalog is long; guidance buried after it is easy for a model to lose.
    If the core prompt no longer has a catalog heading, fail closed rather than
    send an uncomposed prompt that silently drops the safety rules.
    """
    preamble, separator, catalog = core_system.partition(_CATALOG_HEADING)
    if not separator:
        raise RuntimeError(
            "The core extraction prompt no longer contains a Catalog heading; "
            "refusing to send an uncomposed prompt."
        )
    return f"{preamble}\n\n{_RULES_HEADING}{rules}{separator}{catalog}"


NARRATIVE_TO_ENTITIES: Final[PromptTemplate] = PromptTemplate(
    id=CORE_NARRATIVE_TO_ENTITIES.id,
    system=compose_extraction_system(CORE_NARRATIVE_TO_ENTITIES.system, extraction_rules_text()),
    user_template=CORE_NARRATIVE_TO_ENTITIES.user_template,
)

PROMPT_SET: Final[dict[str, PromptTemplate]] = {
    NARRATIVE_TO_ENTITIES.id: NARRATIVE_TO_ENTITIES,
    DICTATION_TRANSCRIBE.id: DICTATION_TRANSCRIBE,
}


def prompt_set_fingerprint() -> str:
    """A stable content hash over every template the service actually sends."""
    digest = hashlib.sha256()
    for template in sorted(PROMPT_SET.values(), key=lambda item: item.id):
        digest.update(template.id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(template.system.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(template.user_template.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


__all__ = [
    "DICTATION_TRANSCRIBE",
    "NARRATIVE_TO_ENTITIES",
    "PROMPT_SET",
    "PROMPT_SET_VERSION",
    "PromptTemplate",
    "compose_extraction_system",
    "prompt_set_fingerprint",
]
