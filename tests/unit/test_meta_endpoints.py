"""Self-description endpoints: capabilities, IGs, the error CodeSystem.

The point of ``GET /v1/capabilities`` is that a client can tell "not built yet"
from "misconfigured deployment" without probing for 404s. That only works if the
lists it publishes are true, so the tests here cross-check them against the
router table rather than against a copy of the same literals.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fhiratwill import CASCADE_ORDER

from fhirbridge.api.routers.meta import IMPLEMENTED_ENDPOINTS, NOT_IMPLEMENTED_ENDPOINTS
from fhirbridge.domain.errors import ERROR_SPECS, ErrorCode
from fhirbridge.version import CODE_VERSION
from tests.helpers import api_routes


def routed(app: FastAPI) -> set[str]:
    """``METHOD /path`` for every route in the app."""
    return {
        f"{method} {route.path}"
        for route in api_routes(app)
        for method in route.methods or ()
        if method not in {"HEAD", "OPTIONS"}
    }


class TestCapabilities:
    async def test_it_lists_every_validation_layer_with_its_availability(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/v1/capabilities")

        assert response.status_code == 200
        layers = response.json()["validation_layers"]
        assert len(layers) == len(CASCADE_ORDER)
        assert [layer["number"] for layer in layers] == list(range(1, len(CASCADE_ORDER) + 1))

    async def test_the_llm_layers_are_declared_unavailable_in_this_build(
        self, client: httpx.AsyncClient
    ) -> None:
        """M1 ships L1-L5. Advertising L6/L7 would be a false conformance claim."""
        layers = (await client.get("/v1/capabilities")).json()["validation_layers"]
        by_name = {layer["layer"]: layer for layer in layers}

        assert by_name["fidelity"]["available"] is False
        assert by_name["fidelity"]["requires_llm"] is True
        assert by_name["coverage"]["available"] is False
        assert by_name["plausibility"]["available"] is True
        assert by_name["plausibility"]["requires_llm"] is False

    async def test_it_declares_that_no_llm_is_required(self, client: httpx.AsyncClient) -> None:
        """The adoption on-ramp: /v1/validate needs no key. Say so."""
        assert (await client.get("/v1/capabilities")).json()["llm_required"] is False

    async def test_every_endpoint_it_claims_to_implement_is_routable(
        self, client: httpx.AsyncClient, app: FastAPI
    ) -> None:
        available = routed(app)
        claimed = set(IMPLEMENTED_ENDPOINTS)

        assert claimed
        assert claimed <= available, f"claimed but not routed: {sorted(claimed - available)}"

    async def test_nothing_it_calls_unimplemented_returns_a_success(
        self, client: httpx.AsyncClient
    ) -> None:
        """A 501-stub may be routed; a 200 would make the list a lie."""
        assert NOT_IMPLEMENTED_ENDPOINTS
        for entry in NOT_IMPLEMENTED_ENDPOINTS:
            method, path = entry.split(" ")[:2]
            response = await client.request(method, path, json={})
            assert response.status_code in {404, 501}, f"{entry} answered {response.status_code}"

    async def test_it_reports_the_deployment_posture(self, client: httpx.AsyncClient) -> None:
        """An operator needs to confirm LOCAL_ONLY_MODE took effect."""
        body = (await client.get("/v1/capabilities")).json()

        assert body["local_only_mode"] is False
        assert body["credential_storage"] == "disabled"
        assert body["version"] == CODE_VERSION

    async def test_an_unauthenticated_caller_is_refused(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        assert (await anon_client.get("/v1/capabilities")).status_code == 401


class TestImplementationGuides:
    async def test_it_reports_the_configured_packages(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/igs")

        assert response.status_code == 200
        packages = response.json()["ig_packages"]
        assert {"name": "hl7.fhir.us.core", "version": "9.0.0"}.items() <= packages[0].items()
        assert packages[0]["coordinate"] == "hl7.fhir.us.core#9.0.0"

    async def test_an_unauthenticated_caller_is_refused(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        assert (await anon_client.get("/v1/igs")).status_code == 401


class TestErrorCodeSystem:
    async def test_it_is_reachable_without_credentials(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        """A client should be able to read the error catalogue before it has a key."""
        response = await anon_client.get("/v1/error-codes")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/fhir+json")

    async def test_it_publishes_every_code_in_the_catalogue(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        body = (await anon_client.get("/v1/error-codes")).json()

        assert body["resourceType"] == "CodeSystem"
        published = {concept["code"] for concept in body["concept"]}
        assert published == {str(code) for code in ErrorCode}
        assert body["count"] == len(ERROR_SPECS)

    @pytest.mark.parametrize(
        "code",
        [
            "llm-credentials-required",
            "llm-auth-failed",
            "llm-quota-exhausted",
            "llm-rate-limited",
            "llm-context-exceeded",
            "llm-schema-violation",
            "llm-content-filtered",
            "model-not-qualified",
            "budget-exceeded",
            "egress-blocked",
            "phi-egress-not-acknowledged",
            "insecure-transport",
            "credential-expired",
            "terminology-unavailable",
            "validator-unavailable",
            "unreadable-document",
            "no-clinical-content",
            "profile-impossible",
            "span-verification-failed",
            "review-required",
        ],
    )
    async def test_the_codes_agents_md_requires_are_present(
        self, anon_client: httpx.AsyncClient, code: str
    ) -> None:
        """AGENTS.md 12 names these explicitly. They are API surface, not internals."""
        body = (await anon_client.get("/v1/error-codes")).json()

        assert code in {concept["code"] for concept in body["concept"]}

    async def test_each_concept_carries_its_status_and_retryability(
        self, anon_client: httpx.AsyncClient
    ) -> None:
        body = (await anon_client.get("/v1/error-codes")).json()
        concepts = {concept["code"]: concept for concept in body["concept"]}

        properties = {
            item["code"]: item for item in concepts["terminology-unavailable"]["property"]
        }
        assert properties["http-status"]["valueInteger"] == 503
        assert properties["retryable"]["valueBoolean"] is True

        properties = {item["code"]: item for item in concepts["insecure-transport"]["property"]}
        assert properties["http-status"]["valueInteger"] == 400
        assert properties["retryable"]["valueBoolean"] is False

    async def test_the_concept_order_is_stable(self, anon_client: httpx.AsyncClient) -> None:
        """The CodeSystem is a published artifact; a reordering is a spurious diff."""
        codes = [
            concept["code"]
            for concept in (await anon_client.get("/v1/error-codes")).json()["concept"]
        ]

        assert codes == sorted(codes)
