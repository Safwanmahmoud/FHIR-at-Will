"""Compile a rebound collection Bundle into a destination write plan."""

from __future__ import annotations

import copy
import hashlib
from typing import Any
from urllib.parse import quote

from fhirbridge.delivery.context import SubjectContext
from fhirbridge.delivery.models import PlanNote, PlanStepStatus, WritePlan, WriteStep
from fhirbridge.delivery.rebind import rebind_bundle
from fhirbridge.delivery.targets.base import TargetDescriptor

_IDENTIFIER_SYSTEM = "https://fhirbridge.org/identifier/delivery"


def compile_write_plan(
    *,
    bundle: dict[str, Any],
    context: SubjectContext,
    conversion_id: str,
    tenant_id: str,
    descriptor: TargetDescriptor,
) -> WritePlan:
    rebound, rebind_notes = rebind_bundle(bundle, context)
    notes = list(rebind_notes)
    entries = rebound.get("entry", [])
    full_url_to_step: dict[str, int] = {}
    valid_entry_indexes = [
        index
        for index, entry in enumerate(entries)
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]
    step_index_by_entry = {
        entry_index: step_index for step_index, entry_index in enumerate(valid_entry_indexes)
    }
    for entry_index, entry in enumerate(entries):
        if isinstance(entry, dict) and not entry.get("_deliveryExcluded"):
            full_url = entry.get("fullUrl")
            if isinstance(full_url, str):
                full_url_to_step[full_url] = step_index_by_entry[entry_index]

    steps: list[WriteStep] = []
    transaction_entries: list[dict[str, Any]] = []
    for entry_index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("resource"), dict):
            continue
        resource = copy.deepcopy(raw_entry["resource"])
        resource_type = str(resource.get("resourceType", ""))
        excluded = bool(raw_entry.get("_deliveryExcluded"))
        accepted = descriptor.accepts(resource_type)
        status = (
            PlanStepStatus.EXCLUDED
            if excluded
            else PlanStepStatus.READY
            if accepted
            else PlanStepStatus.BLOCKED
        )
        if not accepted and not excluded:
            notes.append(
                PlanNote(
                    entry_index=entry_index,
                    resource_type=resource_type,
                    element="resourceType",
                    action="blocked",
                    detail="resource type is not accepted by the target descriptor",
                )
            )

        for element in descriptor.forbidden_elements.get(resource_type, frozenset()):
            if element in resource:
                resource.pop(element)
                notes.append(
                    PlanNote(
                        entry_index=entry_index,
                        resource_type=resource_type,
                        element=element,
                        action="stripped",
                        detail="element is forbidden by the target descriptor",
                    )
                )

        key = _idempotency_key(tenant_id, conversion_id, entry_index, descriptor.target_id)
        dependencies = tuple(sorted(_referenced_steps(resource, full_url_to_step)))
        step = WriteStep(
            step_index=len(steps),
            entry_index=entry_index,
            target_api=f"{resource_type}.create",
            method="POST",
            url=resource_type,
            resource=resource,
            depends_on=dependencies,
            idempotency_key=key,
            status=status,
        )
        steps.append(step)
        if status is PlanStepStatus.READY:
            identifier = {"system": _IDENTIFIER_SYSTEM, "value": key}
            existing = resource.get("identifier")
            if isinstance(existing, list):
                existing.append(identifier)
            elif existing is None:
                resource["identifier"] = [identifier]
            else:
                resource["identifier"] = [existing, identifier]
            transaction_entries.append(
                {
                    "fullUrl": raw_entry.get("fullUrl"),
                    "resource": resource,
                    "request": {
                        "method": "POST",
                        "url": resource_type,
                        "ifNoneExist": (
                            "identifier="
                            f"{quote(_IDENTIFIER_SYSTEM, safe='')}%7C{quote(key, safe='')}"
                        ),
                    },
                }
            )

    ready = not any(step.status is PlanStepStatus.BLOCKED for step in steps)
    transaction = (
        {"resourceType": "Bundle", "type": "transaction", "entry": transaction_entries}
        if descriptor.supports_transaction and ready
        else None
    )
    return WritePlan(
        conversion_id=conversion_id,
        target_id=descriptor.target_id,
        descriptor_version=descriptor.version,
        ready=ready,
        transaction=transaction,
        steps=steps,
        notes=notes,
    )


def _idempotency_key(tenant_id: str, conversion_id: str, index: int, target_id: str) -> str:
    material = f"{tenant_id}:{conversion_id}:{index}:{target_id}".encode()
    return hashlib.sha256(material).hexdigest()


def _referenced_steps(resource: Any, full_urls: dict[str, int]) -> set[int]:
    found: set[int] = set()
    if isinstance(resource, dict):
        reference = resource.get("reference")
        if isinstance(reference, str) and reference in full_urls:
            found.add(full_urls[reference])
        for value in resource.values():
            found.update(_referenced_steps(value, full_urls))
    elif isinstance(resource, list):
        for value in resource:
            found.update(_referenced_steps(value, full_urls))
    return found


__all__ = ["compile_write_plan"]
