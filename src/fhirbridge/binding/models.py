"""Terminology binding evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BindingAction(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"
    AMBIGUOUS = "ambiguous"


class BindingNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_index: int = Field(ge=0)
    resource_type: str
    element: str
    action: BindingAction
    value_set: str | None = None
    candidate_count: int = Field(ge=0)
    detail: str = Field(description="PHI-free stage outcome; never source text.")


class BindingCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: int = Field(ge=0)
    bound: int = Field(ge=0)
    unbound: int = Field(ge=0)
    ambiguous: int = Field(ge=0)


class BoundBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle: dict[str, Any]
    notes: list[BindingNote] = Field(default_factory=list)
    coverage: BindingCoverage
    table_version: str


__all__ = [
    "BindingAction",
    "BindingCoverage",
    "BindingNote",
    "BoundBundle",
]
