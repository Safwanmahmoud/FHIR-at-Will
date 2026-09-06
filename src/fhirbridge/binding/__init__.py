"""Deterministic terminology binding for generated FHIR."""

from fhirbridge.binding.bind import bind_bundle
from fhirbridge.binding.models import BoundBundle

__all__ = ["BoundBundle", "bind_bundle"]
