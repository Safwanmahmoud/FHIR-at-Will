"""Built-in delivery target descriptors."""

from fhirbridge.delivery.targets.generic_fhir import GENERIC_FHIR_TARGET

TARGETS = {GENERIC_FHIR_TARGET.target_id: GENERIC_FHIR_TARGET}

__all__ = ["GENERIC_FHIR_TARGET", "TARGETS"]
