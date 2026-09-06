"""The FastAPI application factory.

Startup order matters and is deliberate:

1. Configuration is validated first. A misconfigured process must die at startup
   with a report naming every bad variable, not fail its first request.
2. Logging (with the secret-redaction filter) is installed before anything else
   can log, so no library gets a chance to print a key first.
3. Unsafe-but-allowed settings emit loud warnings.
4. Dependency clients are constructed, but **not** probed as a startup gate. A
   terminology outage must not prevent the process from starting and reporting
   its own unreadiness through ``/readyz``; requests fail closed individually.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fhirbridge.api.deps import AppServices
from fhirbridge.api.errors import install_error_handlers
from fhirbridge.api.middleware import (
    AccessLogMiddleware,
    BodySizeLimitMiddleware,
    CorrelationMiddleware,
    LlmTransportGuardMiddleware,
)
from fhirbridge.api.openapi import install_openapi
from fhirbridge.api.routers import (
    convert,
    deidentify,
    deliver,
    fhir_facade,
    health,
    meta,
    validate,
    voice,
)
from fhirbridge.config import Settings, get_settings
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.llm.gateway import LlmGateway
from fhirbridge.observability.logging import configure_logging
from fhirbridge.observability.metrics import set_build_info
from fhirbridge.observability.tracing import configure_tracing, instrument_app
from fhirbridge.storage.session import create_engine, create_session_factory
from fhirbridge.terminology.client import FhirTerminologyClient
from fhirbridge.version import (
    CODE_VERSION,
    DEID_RULESET_VERSION,
    PROMPT_SET_VERSION,
    TYPED_MODEL_FHIR_VERSION,
)

logger = logging.getLogger(__name__)


def build_services(settings: Settings) -> AppServices:
    """Construct the process-lifetime collaborators."""
    engine = create_engine(settings)
    return AppServices(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        validator=ValidatorClient(
            base_url=settings.validator_base_url, timeout_s=settings.validator_timeout_s
        ),
        terminology=FhirTerminologyClient.from_settings(settings),
        terminology_versions={},
        gateway=LlmGateway(settings=settings),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Pass ``settings`` in tests; otherwise read the env."""
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_logs=resolved.json_logs)
    configure_tracing(resolved)

    set_build_info(
        version=CODE_VERSION,
        prompt_set=PROMPT_SET_VERSION,
        deid_ruleset=DEID_RULESET_VERSION,
        fhir=resolved.default_fhir_version,
        typed_models=TYPED_MODEL_FHIR_VERSION,
        environment=str(resolved.environment),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.emit_startup_warnings()
        services = build_services(resolved)
        app.state.services = services
        logger.info(
            "service_started",
            extra={
                "version": CODE_VERSION,
                "environment": str(resolved.environment),
                "ig_packages": ",".join(resolved.ig_coordinates),
            },
        )
        try:
            yield
        finally:
            await services.validator.aclose()
            await services.terminology.aclose()
            await services.engine.dispose()
            logger.info("service_stopped")

    app = FastAPI(
        title="fhirbridge",
        version=CODE_VERSION,
        lifespan=lifespan,
        root_path=resolved.api_root_path,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # Middleware runs bottom-up on the way in: correlation is added last so it
    # wraps everything and every log line inside has a trace_id.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(
        LlmTransportGuardMiddleware, allow_insecure=resolved.allow_insecure_transport
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=resolved.max_request_bytes)
    app.add_middleware(CorrelationMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(meta.router)
    app.include_router(validate.router)
    app.include_router(convert.router)
    app.include_router(deidentify.router)
    app.include_router(deliver.router)
    app.include_router(voice.router)
    app.include_router(fhir_facade.router)

    install_openapi(app)
    instrument_app(app, resolved)
    return app


__all__ = ["build_services", "create_app"]
