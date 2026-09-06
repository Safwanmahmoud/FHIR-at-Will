"""Caller-supplied chart identity for delivery.

Identity is input context, never an inference from clinical narrative.  The
references in this object must come from the destination session (for example
a SMART launch or the operator's HIS), not from demographic search.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """Destination-native references used to bind a generated resource."""

    patient_ref: str
    encounter_ref: str | None = None
    author_ref: str | None = None
    encounter_start: str | None = None


__all__ = ["SubjectContext"]
