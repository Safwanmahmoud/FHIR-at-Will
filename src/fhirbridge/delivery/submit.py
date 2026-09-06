"""Fail-closed FHIR target client and write-plan executor."""

from __future__ import annotations

import re
from typing import Any, Final

import httpx

from fhirbridge.config import DeliveryMode, Settings
from fhirbridge.delivery.invocation import TargetInvocation
from fhirbridge.delivery.models import DeliveryReceipt, WritePlan
from fhirbridge.domain.errors import (
    EgressBlockedError,
    InvalidRequestError,
    PhiEgressNotAcknowledgedError,
    TargetUnavailableError,
)
from fhirbridge.domain.ids import IdPrefix, new_id

_LOOPBACK: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
_REFERENCE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9]+/[A-Za-z0-9\-.]{1,64}$")


class FhirTargetClient:
    """Request-scoped client; provider responses never enter logs or exceptions."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    def authorize(self, invocation: TargetInvocation, *, submit: bool = False) -> None:
        if submit and self.settings.delivery_mode is not DeliveryMode.SUBMIT:
            raise EgressBlockedError("Delivery submission is disabled by DELIVERY_MODE.")
        if not submit and self.settings.delivery_mode is DeliveryMode.OFF:
            raise EgressBlockedError("Delivery planning is disabled by DELIVERY_MODE.")
        host = invocation.egress_host
        if self.settings.local_only_mode:
            if host not in _LOOPBACK:
                raise EgressBlockedError(
                    "The delivery target is not loopback while LOCAL_ONLY_MODE=true.",
                    safe_context={"target_id": invocation.target_id},
                )
            return
        allowed = {entry.strip().lower() for entry in self.settings.target_egress_allowlist}
        if not host or host not in allowed:
            raise EgressBlockedError(
                "The target host is not in TARGET_EGRESS_ALLOWLIST.",
                safe_context={"target_id": invocation.target_id},
            )
        if self.settings.require_phi_egress_ack and not invocation.phi_egress_acknowledged:
            raise PhiEgressNotAcknowledgedError(
                "External FHIR delivery requires X-PHI-Egress-Acknowledged: true.",
                safe_context={"target_id": invocation.target_id},
            )

    async def read(self, invocation: TargetInvocation, reference: str) -> dict[str, Any]:
        self.authorize(invocation)
        if not _REFERENCE.fullmatch(reference):
            raise InvalidRequestError("A target context reference is malformed.")
        response = await self._request("GET", f"{invocation.base_url}/{reference}", invocation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TargetUnavailableError(
                "The target returned an unreadable FHIR response.",
                safe_context={"target_id": invocation.target_id},
            ) from exc
        if not isinstance(payload, dict):
            raise TargetUnavailableError(
                "The target returned an invalid FHIR response.",
                safe_context={"target_id": invocation.target_id},
            )
        return payload

    async def submit(self, invocation: TargetInvocation, plan: WritePlan) -> DeliveryReceipt:
        self.authorize(invocation, submit=True)
        if not plan.ready or plan.transaction is None:
            raise InvalidRequestError("The write plan is not ready for submission.")
        response = await self._request(
            "POST",
            invocation.base_url,
            invocation,
            json=plan.transaction,
        )
        return DeliveryReceipt(
            delivery_id=new_id(IdPrefix.DELIVERY),
            target_id=invocation.target_id,
            status="submitted",
            response_status=response.status_code,
            resource_count=len(plan.transaction.get("entry", [])),
        )

    async def _request(
        self,
        method: str,
        url: str,
        invocation: TargetInvocation,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {invocation.token.get_secret_value()}",
                    "Accept": "application/fhir+json",
                    "Content-Type": "application/fhir+json",
                },
                json=json,
            )
        except httpx.HTTPError as exc:
            raise TargetUnavailableError(
                "The target request failed.",
                safe_context={"target_id": invocation.target_id},
            ) from exc
        if response.status_code >= 400:
            raise TargetUnavailableError(
                "The target rejected the request.",
                safe_context={
                    "target_id": invocation.target_id,
                    "status": response.status_code,
                },
            )
        return response

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["FhirTargetClient"]
