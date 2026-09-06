"""Bind collection-local references to caller-supplied chart context."""

from __future__ import annotations

import copy
from typing import Any

from fhirbridge.delivery.context import SubjectContext
from fhirbridge.delivery.models import PlanNote


def rebind_bundle(
    bundle: dict[str, Any],
    context: SubjectContext,
) -> tuple[dict[str, Any], tuple[PlanNote, ...]]:
    """Return a rebound copy and notes for resources excluded from delivery."""
    rebound = copy.deepcopy(bundle)
    entries = rebound.get("entry")
    if not isinstance(entries, list):
        return rebound, ()

    replacements: dict[str, str] = {}
    notes: list[PlanNote] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
            continue
        resource = entry["resource"]
        full_url = entry.get("fullUrl")
        resource_type = resource.get("resourceType")
        target = {
            "Patient": context.patient_ref,
            "Encounter": context.encounter_ref,
            "Practitioner": context.author_ref,
        }.get(str(resource_type))
        if isinstance(full_url, str) and target:
            replacements[full_url] = target
        if resource_type in {"Patient", "Encounter"}:
            entry["_deliveryExcluded"] = True
            notes.append(
                PlanNote(
                    entry_index=index,
                    resource_type=str(resource_type),
                    element="resource",
                    action="excluded",
                    detail="destination identity is supplied by caller context; never create it",
                )
            )

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("reference")
            if isinstance(reference, str) and reference in replacements:
                value["reference"] = replacements[reference]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(rebound)
    return rebound, tuple(notes)


__all__ = ["rebind_bundle"]
