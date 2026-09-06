from __future__ import annotations

from typing import Any

from fhirbridge.delivery.context import SubjectContext
from fhirbridge.delivery.models import PlanStepStatus
from fhirbridge.delivery.plan import compile_write_plan
from fhirbridge.delivery.targets.generic_fhir import GENERIC_FHIR_TARGET


def test_compiler_excludes_narrative_identity_and_rebinds_subject() -> None:
    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "fullUrl": "urn:uuid:patient",
                "resource": {"resourceType": "Patient", "gender": "female"},
            },
            {
                "fullUrl": "urn:uuid:observation",
                "resource": {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"text": "Heart rate"},
                    "subject": {"reference": "urn:uuid:patient"},
                },
            },
        ],
    }

    plan = compile_write_plan(
        bundle=bundle,
        context=SubjectContext(patient_ref="Patient/target"),
        conversion_id="cnv_test",
        tenant_id="ten_test",
        descriptor=GENERIC_FHIR_TARGET,
    )

    assert plan.ready is True
    assert plan.steps[0].status is PlanStepStatus.EXCLUDED
    assert plan.steps[1].resource["subject"]["reference"] == "Patient/target"
    assert plan.transaction is not None
    assert len(plan.transaction["entry"]) == 1
    request = plan.transaction["entry"][0]["request"]
    assert request["method"] == "POST"
    assert request["ifNoneExist"].startswith("identifier=")


def test_idempotency_keys_are_deterministic() -> None:
    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": {"resourceType": "Observation"}}],
    }
    kwargs = {
        "bundle": bundle,
        "context": SubjectContext(patient_ref="Patient/target"),
        "conversion_id": "cnv_test",
        "tenant_id": "ten_test",
        "descriptor": GENERIC_FHIR_TARGET,
    }
    first = compile_write_plan(**kwargs)
    second = compile_write_plan(**kwargs)
    assert first.steps[0].idempotency_key == second.steps[0].idempotency_key
