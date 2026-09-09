"""The validator and terminology sidecars, for real.

The unit tests mock the HTTP transport, which proves we *parse* the sidecars'
answers correctly. It cannot prove we *ask* correctly: whether the real
``validator_cli.jar`` accepts the query parameters we send, whether US Core
9.0.0 actually resolved inside the image, whether HAPI returns
``$validate-code`` in the shape we flatten. Those only come out against the real
processes, which is what this file is for. It is also where the M1 acceptance
criterion is discharged: score a bundle against ``hl7.fhir.us.core#9.0.0``, and
fail closed when a dependency is down.

Skipped unless the sidecars are reachable, so `pytest` still passes without
`docker compose up`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from fhiratwill import RoutingDecision, ValidationLayer, ValidationSpec

from fhirbridge.config import Environment, Settings
from fhirbridge.domain.errors import (
    IgNotLoadedError,
    TerminologyUnavailableError,
    ValidatorUnavailableError,
)
from fhirbridge.fhir.validator_client import ValidatorClient
from fhirbridge.terminology.client import FhirTerminologyClient
from fhirbridge.validation.cascade import ValidationCascade

pytestmark = pytest.mark.integration

US_CORE = "hl7.fhir.us.core#9.0.0"
US_CORE_PATIENT = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"

# The synthetic CodeSystem loaded by docker/terminology. Licensed content
# (SNOMED CT, LOINC, RxNorm) is never committed and never fetched by CI, so the
# terminology assertions here have to stand on a fixture we own (AGENTS.md 8.3).
SYNTHETIC_SYSTEM = "http://fhirbridge.example/CodeSystem/synthetic-vitals"
SYNTHETIC_CODE = "hr"

PATIENT = {
    "resourceType": "Patient",
    "id": "example",
    "identifier": [{"system": "http://example.org/mrn", "value": "SYNTH-1"}],
    "name": [{"family": "Synthetic", "given": ["Test"]}],
    "gender": "female",
    "birthDate": "1980-01-01",
}


@pytest.fixture(scope="session")
def validator_url() -> str:
    """The validator sidecar, or a skip.

    Probed with a real ``/validateResource`` call rather than a GET: the sidecar
    exposes no health endpoint, and a TCP connect succeeds long before the JVM
    has finished loading the IG packages.
    """
    url = (os.environ.get("VALIDATOR_URL") or "").rstrip("/")
    if not url:
        pytest.skip("VALIDATOR_URL is not set; run `just up` first")
    try:
        with httpx.Client(timeout=180.0) as probe:
            response = probe.post(
                f"{url}/validateResource",
                json=PATIENT,
                headers={"Content-Type": "application/fhir+json"},
            )
    except httpx.HTTPError as exc:
        pytest.skip(f"validator at {url} is unreachable: {type(exc).__name__}")
    if response.status_code >= 400:
        pytest.skip(f"validator at {url} answered {response.status_code}")
    return url


@pytest.fixture(scope="session")
def terminology_url() -> str:
    """The terminology server, or a skip."""
    url = (os.environ.get("TERMINOLOGY_URL") or "").rstrip("/")
    if not url:
        pytest.skip("TERMINOLOGY_URL is not set; run `just up` first")
    try:
        with httpx.Client(timeout=60.0) as probe:
            response = probe.get(f"{url}/metadata", params={"_summary": "true"})
    except httpx.HTTPError as exc:
        pytest.skip(f"terminology server at {url} is unreachable: {type(exc).__name__}")
    if response.status_code >= 400:
        pytest.skip(f"terminology server at {url} answered {response.status_code}")
    return url


@pytest.fixture
def settings(validator_url: str, terminology_url: str) -> Settings:
    return Settings.model_validate(
        {
            "FHIRBRIDGE_ENV": Environment.DEVELOPMENT,
            "DATABASE_URL": "postgresql+asyncpg://fhirbridge:fhirbridge@localhost:5432/test",
            "REDIS_URL": "redis://localhost:6379/0",
            "VALIDATOR_URL": validator_url,
            "TERMINOLOGY_URL": terminology_url,
            "VALIDATOR_VERSION": os.environ.get("VALIDATOR_VERSION", "6.9.8"),
            "LLM_EGRESS_ALLOWLIST": "",
        }
    )


@pytest.fixture
async def validator(validator_url: str) -> AsyncIterator[ValidatorClient]:
    client = ValidatorClient(base_url=validator_url, timeout_s=180.0)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def terminology(terminology_url: str) -> AsyncIterator[FhirTerminologyClient]:
    client = FhirTerminologyClient(base_url=terminology_url, timeout_s=60.0)
    try:
        yield client
    finally:
        await client.aclose()


class TestValidatorSidecar:
    async def test_a_conformant_patient_produces_no_errors(
        self, validator: ValidatorClient
    ) -> None:
        outcome = await validator.validate_resource(PATIENT)

        assert outcome.errors == (), [issue.message for issue in outcome.errors]

    async def test_a_missing_required_element_is_an_error(self, validator: ValidatorClient) -> None:
        outcome = await validator.validate_resource(
            {"resourceType": "Observation", "code": {"text": "heart rate"}}
        )

        assert outcome.errors, "Observation without status must not validate"

    async def test_us_core_9_is_actually_loaded(self, validator: ValidatorClient) -> None:
        """The M1 acceptance criterion, stated as a readiness probe.

        A validator missing the IG does not error — it downgrades the profile to
        an unresolved reference and reports the resource as fine, which would let
        us publish a conformance claim we never checked.
        """
        health = await validator.health(required_profiles=(US_CORE_PATIENT,))

        assert health.reachable is True
        assert health.profiles_missing == (), health.detail
        assert health.ready is True

    async def test_a_us_core_violation_is_reported_against_the_profile(
        self, validator: ValidatorClient
    ) -> None:
        """US Core Patient requires an identifier; base FHIR Patient does not.

        Two calls, because the interesting assertion is the *difference*: the same
        resource is clean against base FHIR and dirty against the profile. One
        call alone would also pass if the validator were rejecting it for an
        unrelated reason.
        """
        without_identifier = {key: value for key, value in PATIENT.items() if key != "identifier"}

        base = await validator.validate_resource(without_identifier)
        profiled = await validator.validate_resource(
            without_identifier, profiles=(US_CORE_PATIENT,)
        )

        assert base.errors == ()
        assert profiled.errors

    async def test_an_unknown_profile_fails_closed(self, validator: ValidatorClient) -> None:
        """Principle 2.4, at the layer where it is easiest to get wrong."""
        with pytest.raises(IgNotLoadedError):
            await validator.validate_resource(
                PATIENT,
                profiles=("http://example.org/StructureDefinition/no-such-profile",),
            )

    async def test_fhirpath_is_evaluated_by_the_sidecar(self, validator: ValidatorClient) -> None:
        satisfied = await validator.evaluate_fhirpath(PATIENT, "Patient.gender.exists()")
        unsatisfied = await validator.evaluate_fhirpath(PATIENT, "Patient.deceased.exists()")

        assert satisfied.is_true is True
        assert unsatisfied.is_true is False

    async def test_an_unreachable_validator_raises_rather_than_passing(self) -> None:
        client = ValidatorClient(base_url="http://127.0.0.1:1", timeout_s=2.0)
        try:
            with pytest.raises(ValidatorUnavailableError):
                await client.validate_resource(PATIENT)
        finally:
            await client.aclose()


class TestTerminologySidecar:
    async def test_a_known_code_is_confirmed(self, terminology: FhirTerminologyClient) -> None:
        result = await terminology.validate_code(system=SYNTHETIC_SYSTEM, code=SYNTHETIC_CODE)

        assert result.result is True

    async def test_an_unknown_code_in_a_known_system_is_rejected(
        self, terminology: FhirTerminologyClient
    ) -> None:
        result = await terminology.validate_code(
            system=SYNTHETIC_SYSTEM, code="definitely-not-a-code"
        )

        assert result.result is False

    async def test_lookup_returns_the_servers_own_display(
        self, terminology: FhirTerminologyClient
    ) -> None:
        """The display must come from the server, never from our own guess."""
        result = await terminology.lookup(system=SYNTHETIC_SYSTEM, code=SYNTHETIC_CODE)

        assert result.display

    async def test_the_code_never_appears_in_the_request_url(self, terminology_url: str) -> None:
        """Principle 2.6, verified against the wire and not just against intent.

        A code is clinical content, so it travels in a POSTed ``Parameters`` body
        where a reverse proxy's access log cannot capture it. Observed through an
        injected transport rather than a mock, so it is the real server that has
        to accept the shape we send.
        """
        sent: list[httpx.Request] = []

        async def record(request: httpx.Request) -> None:
            sent.append(request)

        async with httpx.AsyncClient(timeout=60.0, event_hooks={"request": [record]}) as transport:
            client = FhirTerminologyClient(base_url=terminology_url, client=transport)
            result = await client.validate_code(system=SYNTHETIC_SYSTEM, code=SYNTHETIC_CODE)

        assert result.result is True
        assert sent, "the call never reached the transport"
        for request in sent:
            assert request.method == "POST"
            assert SYNTHETIC_CODE not in str(request.url)
            assert SYNTHETIC_SYSTEM not in str(request.url)

    async def test_an_unreachable_server_raises_rather_than_answering_false(self) -> None:
        """ "I could not check" must never be recorded as "checked and invalid"."""
        client = FhirTerminologyClient(base_url="http://127.0.0.1:1/fhir", timeout_s=2.0)
        try:
            with pytest.raises(TerminologyUnavailableError):
                await client.validate_code(system=SYNTHETIC_SYSTEM, code=SYNTHETIC_CODE)
        finally:
            await client.aclose()

    async def test_health_reports_the_software_it_found(
        self, terminology: FhirTerminologyClient
    ) -> None:
        health = await terminology.health(code_systems=(SYNTHETIC_SYSTEM,))

        assert health.reachable is True
        assert health.fhir_version
        assert [entry.system for entry in health.code_systems] == [SYNTHETIC_SYSTEM]


class TestTheCascadeAgainstRealDependencies:
    async def test_it_scores_a_bundle_against_us_core(
        self,
        validator: ValidatorClient,
        terminology: FhirTerminologyClient,
        settings: Settings,
    ) -> None:
        """The headline M1 criterion: an end-to-end score, every layer accounted for."""
        cascade = ValidationCascade(validator=validator, terminology=terminology, settings=settings)
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"fullUrl": "urn:uuid:1", "resource": PATIENT}],
        }

        report = await cascade.run(
            bundle, ValidationSpec(profiles=(US_CORE_PATIENT,), ig_packages=(US_CORE,))
        )

        ran = {result.layer for result in report.layers}
        assert ran >= {
            ValidationLayer.STRUCTURAL,
            ValidationLayer.PROFILE,
            ValidationLayer.TERMINOLOGY,
            ValidationLayer.INVARIANTS,
            ValidationLayer.PLAUSIBILITY,
        }
        assert report.status is not RoutingDecision.REJECT
        assert report.conformant is True
        assert report.versions.ig == [US_CORE]
        assert report.versions.validator

    async def test_a_nonconformant_bundle_is_rejected(
        self,
        validator: ValidatorClient,
        terminology: FhirTerminologyClient,
        settings: Settings,
    ) -> None:
        cascade = ValidationCascade(validator=validator, terminology=terminology, settings=settings)

        report = await cascade.run(
            {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": {"resourceType": "Observation", "status": "bogus-status"}}],
            },
            ValidationSpec(ig_packages=(US_CORE,)),
        )

        assert report.status is RoutingDecision.REJECT
        assert report.conformant is False

    async def test_it_fails_closed_when_terminology_is_down(
        self, validator: ValidatorClient, settings: Settings
    ) -> None:
        """A real validator, a dead terminology server: the run must not complete.

        This is the half of the acceptance criterion that is easy to fake with
        mocks and hard to fake here — the validator genuinely succeeds, so the
        only thing that can stop the cascade is L3 refusing to guess.
        """
        broken = FhirTerminologyClient(base_url="http://127.0.0.1:1/fhir", timeout_s=2.0)
        cascade = ValidationCascade(validator=validator, terminology=broken, settings=settings)
        try:
            with pytest.raises(TerminologyUnavailableError):
                await cascade.run(
                    {
                        "resourceType": "Observation",
                        "status": "preliminary",
                        "code": {"coding": [{"system": SYNTHETIC_SYSTEM, "code": SYNTHETIC_CODE}]},
                        "subject": {"reference": "Patient/example"},
                    },
                    ValidationSpec(ig_packages=(US_CORE,)),
                )
        finally:
            await broken.aclose()

    async def test_it_fails_closed_when_the_validator_is_down(
        self, terminology: FhirTerminologyClient, settings: Settings
    ) -> None:
        broken = ValidatorClient(base_url="http://127.0.0.1:1", timeout_s=2.0)
        cascade = ValidationCascade(validator=broken, terminology=terminology, settings=settings)
        try:
            with pytest.raises(ValidatorUnavailableError):
                await cascade.run(PATIENT, ValidationSpec(ig_packages=(US_CORE,)))
        finally:
            await broken.aclose()
