"""HTTP middleware: correlation, access logging, metrics, size limits.

Two things here are load-bearing for the safety properties rather than merely
operational:

* The route **template** (``/v1/conversions/{conversion_id}``) is what reaches
  logs and metric labels, never the concrete path. Concrete paths carry ids, and
  ids in metric labels are a cardinality bomb; more importantly the habit of
  logging raw paths is how PHI reaches logs the day someone adds a query
  parameter (principle 2.6).
* ``Content-Length`` is enforced before the body is read, so a declared
  multi-gigabyte upload is refused rather than buffered.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Route

from fhirbridge.api.errors import render_error
from fhirbridge.domain.errors import InsecureTransportError, PayloadTooLargeError
from fhirbridge.observability import context
from fhirbridge.observability.context import REQUEST_ID_HEADER, TRACE_ID_HEADER
from fhirbridge.observability.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("fhirbridge.access")

_UNMATCHED_ROUTE: Final[str] = "unmatched"
"""One bucket for every 404 path, so scanners cannot inflate the metric space."""

Dispatch = Callable[[Request], Awaitable[Response]]


def _sanitize_request_id(value: str | None) -> str:
    """Accept a client-supplied request id only if it is short and printable.

    Clients set ``X-Request-Id`` and we echo it into every log line, so it is
    untrusted input reaching the log. Anything unusual is replaced rather than
    escaped.
    """
    if not value:
        return uuid.uuid4().hex
    candidate = value.strip()
    if not (0 < len(candidate) <= 128):
        return uuid.uuid4().hex
    if not all(char.isalnum() or char in "-_.:" for char in candidate):
        return uuid.uuid4().hex
    return candidate


def route_template(request: Request) -> str:
    """Resolve the matched route's path template, or ``unmatched``.

    Starlette records the matched route in the scope once routing has run, so
    call this *after* the handler where possible; the fallback match loop covers
    the case where the request failed before routing.
    """
    matched = request.scope.get("route")
    if isinstance(matched, Route):
        return matched.path
    for route in request.app.routes:
        if isinstance(route, Route) and route.matches(request.scope)[0] is Match.FULL:
            return route.path
    return _UNMATCHED_ROUTE


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind ``trace_id``/``request_id`` for the request and echo them back."""

    async def dispatch(self, request: Request, call_next: Dispatch) -> Response:
        request_id = _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        trace_id = uuid.uuid4().hex

        with context.bind(trace_id=trace_id, request_id=request_id):
            request.state.trace_id = trace_id
            request.state.request_id = request_id
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers[TRACE_ID_HEADER] = trace_id
            return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized payloads on the declared ``Content-Length``."""

    def __init__(self, app: object, *, max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]  # Starlette types this as ASGIApp
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Dispatch) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return render_error(
                PayloadTooLargeError(
                    "The request body exceeds the configured maximum size.",
                    safe_context={"max_bytes": self.max_bytes},
                ),
                request=request,
            )
        return await call_next(request)


class LlmTransportGuardMiddleware(BaseHTTPMiddleware):
    """Refuse caller-supplied credentials over plaintext HTTP (AGENTS.md 7.1).

    This is middleware rather than a route dependency deliberately: the check
    must hold for every endpoint that exists now and every endpoint added later,
    including the FHIR facade, and a per-route dependency is something a future
    handler can forget.

    ``X-Forwarded-Proto`` is honoured because the common deployment terminates
    TLS at an ingress and speaks plain HTTP to this process. That header is only
    trustworthy behind a proxy that sets it, which is the same trust assumption
    ``--proxy-headers`` already makes.
    """

    def __init__(self, app: object, *, allow_insecure: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]  # Starlette types this as ASGIApp
        self.allow_insecure = allow_insecure

    async def dispatch(self, request: Request, call_next: Dispatch) -> Response:
        if not self.allow_insecure and _carries_credential(request):
            forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
            scheme = forwarded or request.url.scheme
            if scheme != "https":
                logger.warning("insecure_transport_rejected", extra={"scheme": scheme})
                return render_error(
                    InsecureTransportError(
                        "A request credential was supplied over plaintext HTTP. Use HTTPS, or "
                        "set ALLOW_INSECURE_TRANSPORT=true for local development only.",
                        safe_context={"scheme": scheme},
                    ),
                    request=request,
                )
        return await call_next(request)


_CREDENTIAL_HEADERS: Final[tuple[str, ...]] = (
    "x-llm-api-key",
    "x-llm-extra-headers",
    "x-stt-api-key",
    "x-stt-extra-headers",
    "x-target-token",
)


def _carries_credential(request: Request) -> bool:
    return any(header in request.headers for header in _CREDENTIAL_HEADERS)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured access record and the HTTP metrics per request."""

    async def dispatch(self, request: Request, call_next: Dispatch) -> Response:
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            template = route_template(request)
            HTTP_DURATION.labels(method=request.method, route=template).observe(elapsed)
            HTTP_REQUESTS.labels(
                method=request.method, route=template, status=f"{status // 100}xx"
            ).inc()
            logger.info(
                "http_request",
                extra={
                    # `route` not `path`: the template never contains an id.
                    "route": template,
                    "method": request.method,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )


__all__ = [
    "REQUEST_ID_HEADER",
    "TRACE_ID_HEADER",
    "AccessLogMiddleware",
    "BodySizeLimitMiddleware",
    "CorrelationMiddleware",
    "LlmTransportGuardMiddleware",
    "route_template",
]
