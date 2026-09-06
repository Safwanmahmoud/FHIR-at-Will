"""Deterministically propose, verify, and apply clinical codes."""

from __future__ import annotations

import copy
from typing import Any

from fhirbridge.binding.models import (
    BindingAction,
    BindingCoverage,
    BindingNote,
    BoundBundle,
)
from fhirbridge.binding.rules import ConceptPack, ConceptRule, load_concepts, normalize_designation
from fhirbridge.domain.errors import DomainError, ErrorCode
from fhirbridge.fhir.tags import MACHINE_CODED, tag
from fhirbridge.terminology.interface import TerminologyClient

_UCUM = "http://unitsofmeasure.org"


async def bind_bundle(
    bundle: dict[str, Any],
    *,
    client: TerminologyClient,
    pack: ConceptPack | None = None,
) -> BoundBundle:
    """Return a coded copy; ambiguous or unverifiable concepts remain text-only."""
    rules = pack or load_concepts()
    output = copy.deepcopy(bundle)
    notes: list[BindingNote] = []
    eligible = bound = unbound = ambiguous = 0

    for entry_index, entry in enumerate(output.get("entry", [])):
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        resource_type = str(resource.get("resourceType", ""))
        resource_bound = False
        for element, value in list(resource.items()):
            path = f"{resource_type}.{element}"
            if (
                isinstance(value, dict)
                and isinstance(value.get("text"), str)
                and not value.get("coding")
            ):
                eligible += 1
                action, candidate_count = await _bind_concept(
                    value,
                    path=path,
                    client=client,
                    pack=rules,
                )
                notes.append(
                    BindingNote(
                        entry_index=entry_index,
                        resource_type=resource_type,
                        element=element,
                        action=action,
                        value_set=_value_set_for(path, rules),
                        candidate_count=candidate_count,
                        detail=_detail(action),
                    )
                )
                if action is BindingAction.BOUND:
                    bound += 1
                    resource_bound = True
                elif action is BindingAction.AMBIGUOUS:
                    ambiguous += 1
                else:
                    unbound += 1
            if element.startswith("value") and isinstance(value, dict):
                unit = value.get("unit")
                if isinstance(unit, str) and not value.get("code"):
                    eligible += 1
                    action = await _bind_unit(value, unit=unit, client=client, pack=rules)
                    notes.append(
                        BindingNote(
                            entry_index=entry_index,
                            resource_type=resource_type,
                            element=element,
                            action=action,
                            value_set=None,
                            candidate_count=1 if action is BindingAction.BOUND else 0,
                            detail=_detail(action),
                        )
                    )
                    if action is BindingAction.BOUND:
                        bound += 1
                        resource_bound = True
                    else:
                        unbound += 1
        if resource_bound:
            meta = resource.setdefault("meta", {})
            tags = meta.setdefault("tag", [])
            tags.append(tag(MACHINE_CODED, "Terminology-verified deterministic coding"))

    return BoundBundle(
        bundle=output,
        notes=notes,
        coverage=BindingCoverage(
            eligible=eligible,
            bound=bound,
            unbound=unbound,
            ambiguous=ambiguous,
        ),
        table_version=rules.version,
    )


async def _bind_concept(
    concept: dict[str, Any],
    *,
    path: str,
    client: TerminologyClient,
    pack: ConceptPack,
) -> tuple[BindingAction, int]:
    text = normalize_designation(str(concept["text"]))
    table_candidates = [
        rule for rule in pack.concepts if rule.path == path and text in rule.aliases
    ]
    candidates = table_candidates
    value_set = _value_set_for(path, pack)
    if not candidates and value_set:
        try:
            expansion = await client.expand(value_set=value_set, filter_text=text, count=10)
        except DomainError as exc:
            if exc.code is ErrorCode.UNKNOWN_VALUE_SET:
                return BindingAction.UNBOUND, 0
            raise
        exact = [
            coding
            for coding in expansion.contains
            if coding.display and normalize_designation(coding.display) == text
        ]
        if len(exact) > 1:
            return BindingAction.AMBIGUOUS, len(exact)
        if len(exact) == 1 and exact[0].system and exact[0].code:
            candidates = [
                ConceptRule(
                    path=path,
                    system=exact[0].system,
                    code=exact[0].code,
                    display=exact[0].display or text,
                    value_set=value_set,
                    aliases=frozenset({text}),
                )
            ]
    if len(candidates) > 1:
        return BindingAction.AMBIGUOUS, len(candidates)
    if not candidates:
        return BindingAction.UNBOUND, 0
    candidate = candidates[0]
    try:
        verified = await client.validate_code(
            system=candidate.system,
            code=candidate.code,
            display=candidate.display,
            value_set=candidate.value_set,
        )
    except DomainError as exc:
        if exc.code is ErrorCode.UNKNOWN_VALUE_SET:
            return BindingAction.UNBOUND, 1
        raise
    if not verified.result:
        return BindingAction.UNBOUND, 1
    concept["coding"] = [
        {
            "system": candidate.system,
            "code": candidate.code,
            "display": verified.display or candidate.display,
            "userSelected": False,
        }
    ]
    return BindingAction.BOUND, 1


async def _bind_unit(
    quantity: dict[str, Any],
    *,
    unit: str,
    client: TerminologyClient,
    pack: ConceptPack,
) -> BindingAction:
    normalized = normalize_designation(unit)
    matches = [rule for rule in pack.units if normalized in rule.aliases]
    if len(matches) != 1:
        return BindingAction.AMBIGUOUS if len(matches) > 1 else BindingAction.UNBOUND
    candidate = matches[0]
    verified = await client.validate_code(system=_UCUM, code=candidate.code)
    if not verified.result:
        return BindingAction.UNBOUND
    quantity["system"] = _UCUM
    quantity["code"] = candidate.code
    quantity["unit"] = candidate.display
    return BindingAction.BOUND


def _value_set_for(path: str, pack: ConceptPack) -> str | None:
    return next((rule.value_set for rule in pack.concepts if rule.path == path), None)


def _detail(action: BindingAction) -> str:
    return {
        BindingAction.BOUND: "candidate verified and applied",
        BindingAction.UNBOUND: "no single verified candidate",
        BindingAction.AMBIGUOUS: "multiple exact candidates; coding refused",
    }[action]


__all__ = ["bind_bundle"]
