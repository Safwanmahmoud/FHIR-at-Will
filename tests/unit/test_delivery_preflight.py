from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fhiratwill import PreflightStatus, SubjectContext

from fhirbridge.delivery.invocation import TargetInvocation
from fhirbridge.delivery.preflight import run_preflight


class Reader:
    def __init__(self, resources: dict[str, dict[str, Any]]) -> None:
        self.resources = resources

    async def read(self, invocation: TargetInvocation, reference: str) -> dict[str, Any]:
        del invocation
        return self.resources[reference]


@pytest.mark.asyncio
async def test_preflight_matches_context_without_reporting_values() -> None:
    year = datetime.now(UTC).year - 62
    bundle = {
        "entry": [
            {"resource": {"resourceType": "Patient", "gender": "male"}},
            {
                "resource": {
                    "resourceType": "Observation",
                    "code": {"text": "Age"},
                    "valueQuantity": {"value": 62, "unit": "years"},
                }
            },
        ]
    }
    invocation = TargetInvocation.from_headers(
        target_id="generic-fhir-r4",
        base_url="https://fhir.example",
        token="synthetic-token",
    )
    report = await run_preflight(
        bundle=bundle,
        context=SubjectContext(patient_ref="Patient/1"),
        invocation=invocation,
        reader=Reader(
            {
                "Patient/1": {
                    "resourceType": "Patient",
                    "gender": "male",
                    "birthDate": f"{year}-01-01",
                }
            }
        ),
    )
    assert report.status is PreflightStatus.PASSED
    assert all(str(year) not in check.detail for check in report.checks)


@pytest.mark.asyncio
async def test_preflight_rejects_decisive_gender_mismatch() -> None:
    invocation = TargetInvocation.from_headers(
        target_id="generic-fhir-r4",
        base_url="https://fhir.example",
        token="synthetic-token",
    )
    report = await run_preflight(
        bundle={"entry": [{"resource": {"resourceType": "Patient", "gender": "female"}}]},
        context=SubjectContext(patient_ref="Patient/1"),
        invocation=invocation,
        reader=Reader(
            {
                "Patient/1": {
                    "resourceType": "Patient",
                    "gender": "male",
                    "birthDate": "2000-01-01",
                }
            }
        ),
    )
    assert report.status is PreflightStatus.FAILED
