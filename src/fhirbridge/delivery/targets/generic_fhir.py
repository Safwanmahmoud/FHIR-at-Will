"""Generic FHIR R4 transaction target."""

from __future__ import annotations

from dataclasses import dataclass, field

from fhirbridge.delivery.targets.base import FailurePolicy
from fhirbridge.version import TARGET_DESCRIPTOR_VERSION


@dataclass(frozen=True, slots=True)
class GenericFhirTarget:
    target_id: str = "generic-fhir-r4"
    version: str = TARGET_DESCRIPTOR_VERSION
    supports_transaction: bool = True
    failure_policy: FailurePolicy = FailurePolicy.ATOMIC_TRANSACTION
    accepted_resource_types: frozenset[str] | None = None
    forbidden_elements: dict[str, frozenset[str]] = field(default_factory=dict)

    def accepts(self, resource_type: str) -> bool:
        return self.accepted_resource_types is None or resource_type in self.accepted_resource_types


GENERIC_FHIR_TARGET = GenericFhirTarget()

__all__ = ["GENERIC_FHIR_TARGET", "GenericFhirTarget"]
