"""Per-request destination credentials.

Delivery is BYOT (bring your own token): credentials are wrapped as
``SecretStr`` immediately, used for one request, and never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from pydantic import SecretStr

from fhirbridge.domain.errors import InvalidRequestError

HEADER_TARGET_ID: Final[str] = "X-Target-Id"
HEADER_TARGET_BASE_URL: Final[str] = "X-Target-Base-Url"
HEADER_TARGET_TOKEN: Final[str] = "X-Target-Token"  # noqa: S105 - header name, not a secret
HEADER_PHI_ACK: Final[str] = "X-PHI-Egress-Acknowledged"
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})


@dataclass(frozen=True, slots=True)
class TargetInvocation:
    """A single caller-supplied FHIR destination."""

    target_id: str
    base_url: str
    token: SecretStr
    phi_egress_acknowledged: bool = False

    @property
    def egress_host(self) -> str:
        return (urlparse(self.base_url).hostname or "").lower()

    @classmethod
    def from_headers(
        cls,
        *,
        target_id: str | None,
        base_url: str | None,
        token: str | None,
        phi_ack: str | None = None,
    ) -> TargetInvocation:
        resolved_id = (target_id or "").strip().lower()
        resolved_url = (base_url or "").strip().rstrip("/")
        resolved_token = (token or "").strip()
        if not resolved_id:
            raise InvalidRequestError(
                f"{HEADER_TARGET_ID} is required.",
                safe_context={"header": HEADER_TARGET_ID},
            )
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidRequestError(
                f"{HEADER_TARGET_BASE_URL} must be an absolute HTTP(S) URL.",
                safe_context={"header": HEADER_TARGET_BASE_URL},
            )
        if not resolved_token:
            raise InvalidRequestError(
                f"{HEADER_TARGET_TOKEN} is required.",
                safe_context={"header": HEADER_TARGET_TOKEN},
            )
        return cls(
            target_id=resolved_id,
            base_url=resolved_url,
            token=SecretStr(resolved_token),
            phi_egress_acknowledged=(
                phi_ack is not None and phi_ack.strip().lower() in _TRUE_TOKENS
            ),
        )


__all__ = [
    "HEADER_PHI_ACK",
    "HEADER_TARGET_BASE_URL",
    "HEADER_TARGET_ID",
    "HEADER_TARGET_TOKEN",
    "TargetInvocation",
]
