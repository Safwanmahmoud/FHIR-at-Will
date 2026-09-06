from __future__ import annotations

import httpx
import pytest

from fhirbridge.config import DeliveryMode, Settings
from fhirbridge.delivery.invocation import TargetInvocation
from fhirbridge.delivery.submit import FhirTargetClient
from fhirbridge.domain.errors import EgressBlockedError, TargetUnavailableError


def _invocation() -> TargetInvocation:
    return TargetInvocation.from_headers(
        target_id="generic-fhir-r4",
        base_url="https://blocked.example/fhir",
        token="synthetic-token",
        phi_ack="true",
    )


def test_target_must_be_allowlisted(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "delivery_mode": DeliveryMode.PLAN_ONLY,
            "target_egress_allowlist": [],
        }
    )
    with pytest.raises(EgressBlockedError):
        FhirTargetClient(configured).authorize(_invocation())


@pytest.mark.asyncio
async def test_target_error_body_is_not_echoed(settings: Settings) -> None:
    secret_clinical_text = "synthetic patient has a sensitive diagnosis"

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, text=secret_clinical_text)

    configured = settings.model_copy(
        update={
            "delivery_mode": DeliveryMode.PLAN_ONLY,
            "target_egress_allowlist": ["blocked.example"],
        }
    )
    client = FhirTargetClient(
        configured,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TargetUnavailableError) as caught:
        await client.read(_invocation(), "Patient/example")
    assert secret_clinical_text not in str(caught.value)
