"""PHI-free planning and preflight evidence models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanStepStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"


class PreflightStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_index: int = Field(ge=0)
    resource_type: str
    element: str
    action: str
    detail: str = Field(description="PHI-free reason; never an element value.")


class PreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str
    status: PreflightStatus
    detail: str


class PreflightReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PreflightStatus
    checks: list[PreflightCheck] = Field(default_factory=list)


class WriteStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_index: int = Field(ge=0)
    entry_index: int = Field(ge=0)
    target_api: str
    method: str
    url: str
    resource: dict[str, Any]
    depends_on: tuple[int, ...] = ()
    idempotency_key: str
    status: PlanStepStatus


class WritePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversion_id: str
    target_id: str
    descriptor_version: str
    ready: bool
    transaction: dict[str, Any] | None = None
    steps: list[WriteStep] = Field(default_factory=list)
    notes: list[PlanNote] = Field(default_factory=list)
    preflight: PreflightReport | None = None


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str
    target_id: str
    status: str
    response_status: int
    resource_count: int


__all__ = [
    "DeliveryReceipt",
    "PlanNote",
    "PlanStepStatus",
    "PreflightCheck",
    "PreflightReport",
    "PreflightStatus",
    "WritePlan",
    "WriteStep",
]
