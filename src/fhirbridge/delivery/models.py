"""Service delivery values plus re-exported :mod:`fhiratwill` planning models."""

from __future__ import annotations

from fhiratwill import (
    PlanNote,
    PlanStepStatus,
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
    WritePlan,
    WriteStep,
)
from pydantic import BaseModel, ConfigDict


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
