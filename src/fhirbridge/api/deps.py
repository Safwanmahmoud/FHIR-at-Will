"""Application services and FastAPI dependencies.

Long-lived objects (engine, session factory, the validator and terminology
clients with their connection pools) are built once in the lifespan and held in
:class:`AppServices` on ``app.state``. Handlers receive them through
dependencies, which is what lets tests substitute a fake client without
monkeypatching module globals.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fhiratwill import TerminologyClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fhirbridge.api.auth import Principal, Scope, authenticate_api_key, extract_bearer
from fhirbridge.config import Settings
from fhirbridge.delivery.invocation import (
    HEADER_TARGET_BASE_URL,
    HEADER_TARGET_ID,
    HEADER_TARGET_TOKEN,
    TargetInvocation,
)
from fhirbridge.domain.errors import UnauthenticatedError
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.llm.gateway import LlmGateway
from fhirbridge.llm.invocation import (
    HEADER_API_KEY,
    HEADER_BASE_URL,
    HEADER_EXTRA_HEADERS,
    HEADER_MODEL,
    HEADER_PHI_ACK,
    HEADER_PROVIDER,
    HEADER_STT_API_KEY,
    HEADER_STT_BASE_URL,
    HEADER_STT_EXTRA_HEADERS,
    HEADER_STT_LANGUAGE,
    HEADER_STT_MODEL,
    HEADER_STT_PROVIDER,
    LlmInvocation,
    SttInvocation,
)
from fhirbridge.observability import context
from fhirbridge.storage.session import privileged_session, tenant_session
from fhirbridge.validation.cascade import ValidationCascade

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppServices:
    """Process-lifetime collaborators, built once in the lifespan."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    validator: ValidatorClient
    terminology: TerminologyClient
    terminology_versions: dict[str, str | None]
    gateway: LlmGateway

    def cascade(self) -> ValidationCascade:
        return ValidationCascade(
            validator=self.validator,
            terminology=self.terminology,
            settings=self.settings,
            terminology_versions=self.terminology_versions,
        )


def get_services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, AppServices):  # pragma: no cover - startup invariant
        raise RuntimeError("application services are not initialized")
    return services


Services = Annotated[AppServices, Depends(get_services)]


def get_settings_dep(services: Services) -> Settings:
    return services.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_validator(services: Services) -> ValidatorClient:
    return services.validator


ValidatorDep = Annotated[ValidatorClient, Depends(get_validator)]


def get_terminology(services: Services) -> TerminologyClient:
    return services.terminology


TerminologyDep = Annotated[TerminologyClient, Depends(get_terminology)]


def get_cascade(services: Services) -> ValidationCascade:
    return services.cascade()


CascadeDep = Annotated[ValidationCascade, Depends(get_cascade)]


def get_llm_gateway(services: Services) -> LlmGateway:
    return services.gateway


LlmGatewayDep = Annotated[LlmGateway, Depends(get_llm_gateway)]


async def get_llm_invocation(
    x_llm_provider: Annotated[str | None, Header(alias=HEADER_PROVIDER)] = None,
    x_llm_model: Annotated[str | None, Header(alias=HEADER_MODEL)] = None,
    x_llm_api_key: Annotated[str | None, Header(alias=HEADER_API_KEY)] = None,
    x_llm_base_url: Annotated[str | None, Header(alias=HEADER_BASE_URL)] = None,
    x_llm_extra_headers: Annotated[str | None, Header(alias=HEADER_EXTRA_HEADERS)] = None,
    x_phi_egress_ack: Annotated[str | None, Header(alias=HEADER_PHI_ACK)] = None,
) -> LlmInvocation:
    """Parse the caller's BYOK credentials out of the ``X-LLM-*`` headers.

    The headers are read here, in one place, so no handler touches raw credential
    strings; the transport guard (see ``LlmTransportGuardMiddleware``) has already
    refused them over plaintext HTTP before this runs.
    """
    return LlmInvocation.from_headers(
        provider=x_llm_provider,
        model=x_llm_model,
        api_key=x_llm_api_key,
        base_url=x_llm_base_url,
        extra_headers=x_llm_extra_headers,
        phi_ack=x_phi_egress_ack,
    )


LlmInvocationDep = Annotated[LlmInvocation, Depends(get_llm_invocation)]


async def get_stt_invocation(
    x_stt_provider: Annotated[str | None, Header(alias=HEADER_STT_PROVIDER)] = None,
    x_stt_model: Annotated[str | None, Header(alias=HEADER_STT_MODEL)] = None,
    x_stt_api_key: Annotated[str | None, Header(alias=HEADER_STT_API_KEY)] = None,
    x_stt_base_url: Annotated[str | None, Header(alias=HEADER_STT_BASE_URL)] = None,
    x_stt_extra_headers: Annotated[str | None, Header(alias=HEADER_STT_EXTRA_HEADERS)] = None,
    x_stt_language: Annotated[str | None, Header(alias=HEADER_STT_LANGUAGE)] = None,
    x_phi_egress_ack: Annotated[str | None, Header(alias=HEADER_PHI_ACK)] = None,
) -> SttInvocation:
    """Parse the caller's dictation BYOK credentials out of the ``X-STT-*`` headers.

    Separate from the extraction credentials because voice conversion sends audio to
    a speech-to-text provider (Gemini, OpenAI, Groq, ...) that is typically not the
    same provider extraction uses. The single ``X-PHI-Egress-Acknowledged`` header
    covers both external hops.
    """
    return SttInvocation.from_headers(
        provider=x_stt_provider,
        model=x_stt_model,
        api_key=x_stt_api_key,
        base_url=x_stt_base_url,
        extra_headers=x_stt_extra_headers,
        phi_ack=x_phi_egress_ack,
        language=x_stt_language,
    )


SttInvocationDep = Annotated[SttInvocation, Depends(get_stt_invocation)]


async def get_target_invocation(
    x_target_id: Annotated[str | None, Header(alias=HEADER_TARGET_ID)] = None,
    x_target_base_url: Annotated[str | None, Header(alias=HEADER_TARGET_BASE_URL)] = None,
    x_target_token: Annotated[str | None, Header(alias=HEADER_TARGET_TOKEN)] = None,
    x_phi_egress_ack: Annotated[str | None, Header(alias=HEADER_PHI_ACK)] = None,
) -> TargetInvocation:
    """Parse request-scoped target details without exposing raw credentials."""
    return TargetInvocation.from_headers(
        target_id=x_target_id,
        base_url=x_target_base_url,
        token=x_target_token,
        phi_ack=x_phi_egress_ack,
    )


TargetInvocationDep = Annotated[TargetInvocation, Depends(get_target_invocation)]


async def get_principal(
    services: Services,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Principal:
    """Authenticate the caller.

    The lookup runs in a privileged session because the tenant is unknown until
    the key is found — that is the one read that legitimately precedes RLS
    binding. Everything after this point uses :func:`get_session`, which is
    tenant-bound.
    """
    presented = extract_bearer(authorization)
    if not presented:
        raise UnauthenticatedError(
            "Supply a credential as 'Authorization: Bearer <api-key>'.",
        )

    async with privileged_session(services.session_factory, reason="api_key_authentication") as db:
        principal = await authenticate_api_key(db, presented)

    context.set_context(tenant_id=principal.tenant_id)
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_session(services: Services, principal: PrincipalDep) -> AsyncIterator[AsyncSession]:
    """Yield a session with RLS bound to the caller's tenant."""
    async with tenant_session(services.session_factory, principal.tenant_id) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def require_scopes(*scopes: Scope) -> object:
    """Build a dependency that enforces ``scopes`` on a route."""

    async def dependency(principal: PrincipalDep) -> Principal:
        principal.require(*scopes)
        return principal

    return Depends(dependency)


__all__ = [
    "AppServices",
    "CascadeDep",
    "LlmGatewayDep",
    "LlmInvocationDep",
    "PrincipalDep",
    "Services",
    "SessionDep",
    "SettingsDep",
    "SttInvocationDep",
    "TargetInvocationDep",
    "TerminologyDep",
    "ValidatorDep",
    "get_llm_gateway",
    "get_llm_invocation",
    "get_principal",
    "get_services",
    "get_session",
    "get_stt_invocation",
    "get_target_invocation",
    "require_scopes",
]
