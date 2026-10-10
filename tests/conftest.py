"""Shared fixtures.

Design rules for this suite (AGENTS.md 16):

* **No network.** Outbound HTTP is intercepted with ``respx``, so the real
  ``ValidatorClient`` and ``FhirTerminologyClient`` code paths are exercised —
  request construction, response parsing, and the fail-closed branches — rather
  than replaced by stubs that agree with our assumptions.
* **No database.** The M0/M1 endpoints under test reach Postgres only through
  authentication, so the principal dependency is overridden here. Storage gets
  its own integration tests against a real Postgres, where RLS and the
  append-only triggers can actually be asserted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from fhirbridge.api.app import create_app
from fhirbridge.api.auth import Principal, Scope
from fhirbridge.api.deps import AppServices, get_principal, get_services
from fhirbridge.config import Environment, Settings
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.llm.gateway import LlmGateway
from fhirbridge.storage.models import TENANT_SCOPED_TABLES
from fhirbridge.terminology.client import FhirTerminologyClient
from tests.helpers import TERMINOLOGY_URL, VALIDATOR_URL, fhir_json, operation_outcome, parameters

TEST_TENANT = "ten_01JTESTTENANT0000000000"
TEST_KEY_ID = "key_01JTESTKEY000000000000"


@pytest.fixture
def settings() -> Settings:
    """Settings with every required variable supplied explicitly.

    Built directly rather than from the environment so a developer's local
    ``.env`` cannot change test behaviour.
    """
    return Settings.model_validate(
        {
            "FHIRBRIDGE_ENV": Environment.DEVELOPMENT,
            "DATABASE_URL": "postgresql+asyncpg://fhirbridge:fhirbridge@localhost:5432/test",
            "REDIS_URL": "redis://localhost:6379/0",
            "VALIDATOR_URL": VALIDATOR_URL,
            "TERMINOLOGY_URL": TERMINOLOGY_URL,
            "VALIDATOR_VERSION": "6.9.8",
            "JSON_LOGS": True,
            "LLM_EGRESS_ALLOWLIST": "",
        }
    )


@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        actor_type="api_key",
        actor_id=TEST_KEY_ID,
        scopes=frozenset({Scope.CONVERSIONS_WRITE, Scope.FACTS_READ}),
        label="test-key",
    )


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    """Intercept every outbound HTTP call. An unmocked call fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def services(settings: Settings) -> AppServices:
    """Real clients, pointed at the intercepted hosts, with no engine in play."""
    return AppServices(
        settings=settings,
        engine=None,  # type: ignore[arg-type]  # unused: no test here opens a session
        session_factory=None,  # type: ignore[arg-type]
        validator=ValidatorClient(base_url=VALIDATOR_URL, timeout_s=5.0),
        terminology=FhirTerminologyClient(base_url=TERMINOLOGY_URL, timeout_s=5.0),
        terminology_versions={"snomed": "INT-20260501", "loinc": "2.79"},
        gateway=LlmGateway(settings=settings),
    )


@pytest.fixture
def app(settings: Settings, services: AppServices) -> Iterator[FastAPI]:
    application = create_app(settings)
    application.dependency_overrides[get_services] = lambda: services
    yield application
    application.dependency_overrides.clear()


def _asgi_client(app: FastAPI, base_url: str = "https://testserver") -> httpx.AsyncClient:
    """An in-process client.

    ``ASGITransport`` rather than ``TestClient``: it keeps requests inside the
    running event loop, and it does not go through httpx's network transport, so
    the ``respx`` interception of *outbound* calls cannot swallow the request
    under test.
    """
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


@pytest.fixture
async def anon_client(
    app: FastAPI, mock_http: respx.MockRouter
) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no credential, for authn/authz tests."""
    del mock_http
    async with _asgi_client(app) as client:
        yield client


@pytest.fixture
async def client(
    app: FastAPI, principal: Principal, mock_http: respx.MockRouter
) -> AsyncIterator[httpx.AsyncClient]:
    """An authenticated client over HTTPS."""
    del mock_http
    app.dependency_overrides[get_principal] = lambda: principal
    async with _asgi_client(app) as test_client:
        yield test_client


@pytest.fixture
async def plaintext_client(
    app: FastAPI, principal: Principal, mock_http: respx.MockRouter
) -> AsyncIterator[httpx.AsyncClient]:
    """An authenticated client speaking plaintext HTTP, for transport-guard tests."""
    del mock_http
    app.dependency_overrides[get_principal] = lambda: principal
    async with _asgi_client(app, base_url="http://testserver") as test_client:
        yield test_client


# --- Dependency behaviour fixtures -----------------------------------------


@pytest.fixture
def validator_clean(mock_http: respx.MockRouter) -> respx.Route:
    """The validator answering "no issues" for everything."""
    return mock_http.post(f"{VALIDATOR_URL}/validateResource").mock(
        return_value=fhir_json(operation_outcome())
    )


@pytest.fixture
def fhirpath_true(mock_http: respx.MockRouter) -> respx.Route:
    """Every FHIRPath invariant evaluating to true."""
    return mock_http.post(f"{VALIDATOR_URL}/fhirpath").mock(
        return_value=httpx.Response(200, json=[True])
    )


@pytest.fixture
def terminology_valid(mock_http: respx.MockRouter) -> list[respx.Route]:
    """Every code confirmed by the terminology server."""
    return [
        mock_http.post(f"{TERMINOLOGY_URL}{path}").mock(
            return_value=fhir_json(parameters(result=True, display="Heart rate"))
        )
        for path in ("/CodeSystem/$validate-code", "/ValueSet/$validate-code")
    ]


class _FakeResult:
    """The two shapes the readiness probe reads from a result."""

    def __init__(self, scalar: object, rows: list[Any]) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one(self) -> object:
        return self._scalar

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Just enough session for the readiness query and the RLS check.

    The RLS answer is hard-coded to "enforced" because these tests are about the
    validator and terminology probes. The real behaviour of that check — including
    what it reports for a role that bypasses policies — is asserted against a real
    Postgres in ``tests/integration/test_row_level_security.py``, which is the
    only place it can be asserted honestly.
    """

    def __init__(self) -> None:
        self._rls_rows = [
            SimpleNamespace(table_name=table, active=True, enabled=True, forced=True)
            for table in TENANT_SCOPED_TABLES
        ]

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, statement: object = None, *_: object, **__: object) -> _FakeResult:
        return _FakeResult(scalar="fhirbridge_app", rows=self._rls_rows)


@pytest.fixture
def database_up(services: AppServices) -> None:
    """Make the Postgres readiness probe succeed without a real database."""
    services.session_factory = _FakeSession  # type: ignore[assignment]  # structural stub


@pytest.fixture
def all_dependencies_healthy(
    validator_clean: respx.Route,
    fhirpath_true: respx.Route,
    terminology_valid: list[respx.Route],
) -> None:
    """Every dependency answering successfully. Use when the outage is not the point."""
    del validator_clean, fhirpath_true, terminology_valid


@pytest.fixture(autouse=True)
def nn_index() -> Iterator[None]:
    """Keep unit tests off SapBERT. Maps the conversion fixture phrase to LOINC."""
    import numpy as np
    from fhiratwill.terminology_binder import Dictionary, TerminologyIndex

    from fhirbridge.terminology.nn_index import set_index

    class _Encoder:
        def encode(
            self,
            sentences: list[str],
            *,
            batch_size: int = 64,
            convert_to_numpy: bool = True,
            normalize_embeddings: bool = True,
            show_progress_bar: bool = False,
        ) -> np.ndarray:
            del batch_size, convert_to_numpy, show_progress_bar
            rows = []
            for sentence in sentences:
                key = sentence.casefold()
                if "heart rate" in key or key == "pulse":
                    vector = np.array([1.0, 0.0], dtype=np.float32)
                else:
                    vector = np.array([0.0, 1.0], dtype=np.float32)
                if normalize_embeddings:
                    vector = vector / (float(np.linalg.norm(vector)) or 1.0)
                rows.append(vector)
            return np.stack(rows)

    catalog = Dictionary.from_payload(
        [
            {
                "code": "8867-4",
                "display": "Heart rate",
                "aliases": ["pulse"],
                "leaf": True,
            }
        ],
        name="loinc",
        system="http://loinc.org",
    )
    set_index(TerminologyIndex.load(catalog, include_default=False, encoder=_Encoder()))
    try:
        yield
    finally:
        set_index(None)
