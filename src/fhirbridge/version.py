"""Version pins that are stamped onto every artifact.

Principle 2.8 requires every artifact to record the full version set. These
constants are the single source of truth for the non-runtime half of that set;
runtime-discovered versions (validator build, terminology CodeSystem versions)
are read from the sidecars at request time.
"""

from __future__ import annotations

from typing import Final

from fhirbridge import __version__

CODE_VERSION: Final[str] = __version__
"""Version of this service's code, stamped into every validation report."""

PROMPT_SET_VERSION: Final[str] = "v5.6.0"
"""Version of the hash-pinned prompt template set, stamped into conversion reports.

``v5.6.0`` composes the service rule pack onto fhiratwill 0.4.0's fill-in-the-blanks
schema (all catalog resource types) instead of the retired ``Catalog:`` heading.
The number is this service's pin; it is not the core's ``PROMPT_SET_VERSION``.
"""

DEID_RULESET_VERSION: Final[str] = "v1"
"""Version of the deterministic PHI de-identification rules and data set."""

FACT_SCHEMA_VERSION: Final[str] = "v1"
"""Version of the canonical `Fact` schema (AGENTS.md 9.2), unused until M3."""

VALIDATION_REPORT_SCHEMA_VERSION: Final[str] = "v1"
"""Version of the validation report envelope returned by ``POST /v1/validate``."""

BINDING_TABLE_VERSION: Final[str] = "1"
"""Version of the reviewed deterministic terminology concept table."""

TARGET_DESCRIPTOR_VERSION: Final[str] = "v1"
"""Version shared by the built-in delivery target descriptors."""

DELIVERY_PLAN_SCHEMA_VERSION: Final[str] = "v1"
"""Version of the write-plan and delivery-receipt public contracts."""

SUPPORTED_FHIR_VERSIONS: Final[tuple[str, ...]] = ("4.0.1",)
"""FHIR versions this build accepts. R4 only; see docs/adr/0004-r4-typed-models.md."""

TYPED_MODEL_FHIR_VERSION: Final[str] = "4.3.0"
"""FHIR version of the ``fhir.resources`` typed models used for L1.

``fhir.resources`` 8.x ships R4B (4.3.0) and R5, but not R4 (4.0.1). L1 uses the
R4B models constrained to the R4 resource-type allowlist; L2 (the HAPI validator
sidecar, run with ``-version 4.0.1``) is the authoritative R4 conformance check.
See docs/adr/0004-r4-typed-models.md and OPEN_QUESTIONS.md#Q1.
"""

__all__ = [
    "BINDING_TABLE_VERSION",
    "CODE_VERSION",
    "DEID_RULESET_VERSION",
    "DELIVERY_PLAN_SCHEMA_VERSION",
    "FACT_SCHEMA_VERSION",
    "PROMPT_SET_VERSION",
    "SUPPORTED_FHIR_VERSIONS",
    "TARGET_DESCRIPTOR_VERSION",
    "TYPED_MODEL_FHIR_VERSION",
    "VALIDATION_REPORT_SCHEMA_VERSION",
]
