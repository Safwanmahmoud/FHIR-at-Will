"""The terminology transport (AGENTS.md 4, principles 2.4 and 2.6).

Driven through ``respx`` rather than a stub, so the assertions cover what
actually goes on the wire: operations are POSTed with a ``Parameters`` body so
codes stay out of URLs, and every way the server can fail to answer becomes a
fail-closed error rather than a "not valid" verdict.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fhiratwill import SubsumptionOutcome

from fhirbridge.config import TerminologyAuthMode
from fhirbridge.domain.errors import (
    DomainError,
    ErrorCode,
    TerminologyUnavailableError,
)
from fhirbridge.terminology.client import FhirTerminologyClient
from tests.helpers import TERMINOLOGY_URL, fhir_json, parameters

LOINC = "http://loinc.org"


@pytest.fixture
def client() -> FhirTerminologyClient:
    return FhirTerminologyClient(base_url=TERMINOLOGY_URL, timeout_s=5.0)


def body_of(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls[0].request.content)


def params_of(route: respx.Route) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for parameter in body_of(route)["parameter"]:
        value = next(v for k, v in parameter.items() if k.startswith("value"))
        flattened[parameter["name"]] = value
    return flattened


# --- $validate-code -------------------------------------------------------


async def test_validate_code_posts_the_code_in_the_body_not_the_url(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    """Codes describe a patient's clinical state, so principle 2.6 keeps them
    out of URLs and therefore out of proxy and access logs."""
    route = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )

    await client.validate_code(system=LOINC, code="8867-4")

    request = route.calls[0].request
    assert "8867-4" not in str(request.url)
    assert request.url.query == b""
    assert params_of(route) == {"code": "8867-4", "system": LOINC}


async def test_a_value_set_scoped_check_uses_the_value_set_endpoint(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )

    result = await client.validate_code(
        system=LOINC, code="8867-4", value_set="http://example.org/vs"
    )

    assert params_of(route)["url"] == "http://example.org/vs"
    assert result.in_value_set is True


class TestSystemlessCodesAgainstAValueSet:
    """A primitive `code` element carries no system, and most bindings in
    bindings.yaml are exactly that (Patient.gender, Bundle.type, ...).

    $validate-code accepts coding | codeableConcept | code+system |
    code+inferSystem, so a bare code without inferSystem is a 422 on a
    conformant server. Because a 4xx is treated as a fail-closed outage, the
    omission did not degrade L3 quietly — it made every primitive-code binding
    return 503.
    """

    async def test_a_systemless_code_asks_the_server_to_infer_the_system(
        self, client: FhirTerminologyClient, mock_http: respx.MockRouter
    ) -> None:
        route = mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$validate-code").mock(
            return_value=fhir_json(parameters(result=True))
        )

        await client.validate_code(
            system=None,
            code="female",
            value_set="http://hl7.org/fhir/ValueSet/administrative-gender",
        )

        assert params_of(route)["inferSystem"] is True
        assert "system" not in params_of(route)

    async def test_an_explicit_system_is_not_second_guessed(
        self, client: FhirTerminologyClient, mock_http: respx.MockRouter
    ) -> None:
        route = mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$validate-code").mock(
            return_value=fhir_json(parameters(result=True))
        )

        await client.validate_code(system=LOINC, code="8867-4", value_set="http://example.org/vs")

        assert "inferSystem" not in params_of(route)
        assert params_of(route)["system"] == LOINC

    async def test_inference_is_not_requested_without_a_value_set(
        self, client: FhirTerminologyClient, mock_http: respx.MockRouter
    ) -> None:
        """There is nothing to infer from: CodeSystem/$validate-code needs the
        system in the URL parameter, so a systemless call there is simply wrong
        and should not be papered over."""
        route = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
            return_value=fhir_json(parameters(result=True))
        )

        await client.validate_code(system=None, code="female")

        assert "inferSystem" not in params_of(route)


async def test_membership_is_none_when_the_check_was_not_value_set_scoped(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )

    result = await client.validate_code(system=LOINC, code="8867-4")

    assert result.in_value_set is None


async def test_the_servers_display_and_message_are_carried_back(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(
            parameters(result=False, display="Heart rate", message="retired", version="2.79")
        )
    )

    result = await client.validate_code(system=LOINC, code="8867-4")

    assert result.result is False
    assert result.display == "Heart rate"
    assert result.message == "retired"
    assert result.code_system_version == "2.79"


async def test_operation_outcome_diagnostics_are_extracted(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "code-invalid", "diagnostics": "no such code"}],
    }
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=False, issues=outcome))
    )

    result = await client.validate_code(system=LOINC, code="nope")

    assert result.issues == ("no such code",)


async def test_identical_checks_are_served_from_cache(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )

    for _ in range(3):
        await client.validate_code(system=LOINC, code="8867-4")

    assert route.call_count == 1


async def test_a_different_value_set_is_a_different_cache_entry(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    """Same code, different question. Sharing a cache entry would answer the
    membership question with the existence answer."""
    code_system = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )
    value_set = mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$validate-code").mock(
        return_value=fhir_json(parameters(result=False))
    )

    first = await client.validate_code(system=LOINC, code="8867-4")
    second = await client.validate_code(system=LOINC, code="8867-4", value_set="http://vs")

    assert code_system.call_count == 1
    assert value_set.call_count == 1
    assert first.result is True
    assert second.result is False


# --- Fail closed ----------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_fails_closed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter, status: int
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=httpx.Response(status)
    )

    with pytest.raises(TerminologyUnavailableError):
        await client.validate_code(system=LOINC, code="8867-4")


async def test_a_connection_failure_fails_closed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(TerminologyUnavailableError) as caught:
        await client.validate_code(system=LOINC, code="8867-4")

    assert caught.value.safe_context["reason"] == "connect"


async def test_a_timeout_fails_closed_and_is_labelled_as_such(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        side_effect=httpx.ReadTimeout("slow")
    )

    with pytest.raises(TerminologyUnavailableError) as caught:
        await client.validate_code(system=LOINC, code="8867-4")

    assert caught.value.safe_context["reason"] == "timeout"


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_fail_closed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter, status: int
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=httpx.Response(status)
    )

    with pytest.raises(TerminologyUnavailableError) as caught:
        await client.validate_code(system=LOINC, code="8867-4")

    assert caught.value.safe_context["status"] == status


async def test_unparseable_json_fails_closed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=httpx.Response(200, content=b"<html>nope</html>")
    )

    with pytest.raises(TerminologyUnavailableError):
        await client.validate_code(system=LOINC, code="8867-4")


async def test_an_operation_outcome_in_place_of_a_result_fails_closed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    """The server answered, but not the question we asked. That is not a verdict."""
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json({"resourceType": "OperationOutcome", "issue": []})
    )

    with pytest.raises(TerminologyUnavailableError):
        await client.validate_code(system=LOINC, code="8867-4")


async def test_a_wrong_resource_type_fails_closed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json({"resourceType": "Patient"})
    )

    with pytest.raises(TerminologyUnavailableError):
        await client.validate_code(system=LOINC, code="8867-4")


async def test_a_404_is_an_unknown_value_set_not_an_outage(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    """ "I do not have that ValueSet" is a different, actionable answer: the
    operator needs to load a code system, not restart a server."""
    mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$validate-code").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(DomainError) as caught:
        await client.validate_code(system=LOINC, code="8867-4", value_set="http://vs/unknown")

    assert caught.value.code is ErrorCode.UNKNOWN_VALUE_SET
    assert not isinstance(caught.value, TerminologyUnavailableError)


# --- The other operations -------------------------------------------------


async def test_lookup_returns_designations_and_properties(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "name", "valueString": "LOINC"},
            {"name": "display", "valueString": "Heart rate"},
            {"name": "version", "valueString": "2.79"},
            {"name": "inactive", "valueBoolean": False},
            {
                "name": "designation",
                "part": [{"name": "value", "valueString": "Pulse"}],
            },
            {
                "name": "property",
                "part": [
                    {"name": "code", "valueCode": "CLASS"},
                    {"name": "value", "valueString": "HRTRATE"},
                ],
            },
        ],
    }
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$lookup").mock(return_value=fhir_json(payload))

    result = await client.lookup(system=LOINC, code="8867-4")

    assert result.display == "Heart rate"
    assert result.code_system_version == "2.79"
    assert result.designations == ("Pulse",)
    assert result.properties == {"CLASS": "HRTRATE"}
    assert result.inactive is False


async def test_lookup_is_cached(client: FhirTerminologyClient, mock_http: respx.MockRouter) -> None:
    route = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$lookup").mock(
        return_value=fhir_json(parameters(display="Heart rate"))
    )

    await client.lookup(system=LOINC, code="8867-4")
    await client.lookup(system=LOINC, code="8867-4")

    assert route.call_count == 1


async def test_expand_returns_the_expansion_contents(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    payload = {
        "resourceType": "ValueSet",
        "expansion": {
            "total": 2,
            "offset": 0,
            "contains": [
                {"system": LOINC, "code": "8867-4", "display": "Heart rate"},
                {"system": LOINC, "code": "8480-6", "display": "Systolic BP"},
            ],
        },
    }
    route = mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$expand").mock(
        return_value=fhir_json(payload)
    )

    result = await client.expand(value_set="http://vs", filter_text="heart", count=10, offset=0)

    assert [coding.code for coding in result.contains] == ["8867-4", "8480-6"]
    assert result.total == 2
    assert result.incomplete is False
    assert params_of(route)["filter"] == "heart"


async def test_a_truncated_expansion_is_reported_as_incomplete(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    payload = {
        "resourceType": "ValueSet",
        "expansion": {"total": 500, "contains": [{"system": LOINC, "code": "8867-4"}]},
    }
    mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$expand").mock(return_value=fhir_json(payload))

    result = await client.expand(value_set="http://vs")

    assert result.incomplete is True


async def test_expand_fails_closed_when_the_server_returns_something_else(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/ValueSet/$expand").mock(
        return_value=fhir_json({"resourceType": "Parameters", "parameter": []})
    )

    with pytest.raises(TerminologyUnavailableError):
        await client.expand(value_set="http://vs")


async def test_subsumes_parses_the_outcome(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$subsumes").mock(
        return_value=fhir_json(parameters(outcome="subsumes"))
    )

    result = await client.subsumes(system=LOINC, code_a="a", code_b="b")

    assert result.outcome is SubsumptionOutcome.SUBSUMES


async def test_an_unrecognized_subsumption_outcome_degrades_to_not_subsumed(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    """The conservative reading: do not claim a hierarchy relationship we cannot parse."""
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$subsumes").mock(
        return_value=fhir_json(parameters(outcome="sort-of"))
    )

    result = await client.subsumes(system=LOINC, code_a="a", code_b="b")

    assert result.outcome is SubsumptionOutcome.NOT_SUBSUMED


async def test_translate_parses_matches(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    payload = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "result", "valueBoolean": True},
            {
                "name": "match",
                "part": [
                    {"name": "equivalence", "valueCode": "equivalent"},
                    {
                        "name": "concept",
                        "valueCoding": {"system": "http://snomed.info/sct", "code": "364075005"},
                    },
                    {"name": "source", "valueUri": "http://cm"},
                ],
            },
        ],
    }
    mock_http.post(f"{TERMINOLOGY_URL}/ConceptMap/$translate").mock(return_value=fhir_json(payload))

    result = await client.translate(system=LOINC, code="8867-4")

    assert result.result is True
    assert result.matches[0].equivalence == "equivalent"
    assert result.matches[0].concept.code == "364075005"
    assert result.matches[0].source == "http://cm"


# --- Health ---------------------------------------------------------------


async def test_health_reports_software_and_code_system_versions(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.get(f"{TERMINOLOGY_URL}/metadata").mock(
        return_value=fhir_json(
            {
                "resourceType": "CapabilityStatement",
                "fhirVersion": "4.0.1",
                "software": {"name": "HAPI FHIR Server", "version": "8.4.0"},
            }
        )
    )
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$lookup").mock(
        return_value=fhir_json(parameters(version="2.79"))
    )

    health = await client.health(code_systems=[LOINC])

    assert health.reachable is True
    assert health.software == "HAPI FHIR Server 8.4.0"
    assert health.fhir_version == "4.0.1"
    assert health.code_systems[0].version == "2.79"


async def test_health_reports_unreachable_rather_than_raising(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    """Readiness probes need an answer, not an exception."""
    mock_http.get(f"{TERMINOLOGY_URL}/metadata").mock(side_effect=httpx.ConnectError("down"))

    health = await client.health()

    assert health.reachable is False
    assert health.ready is False


async def test_health_reports_an_unknown_code_system_version_as_none(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    mock_http.get(f"{TERMINOLOGY_URL}/metadata").mock(
        return_value=fhir_json({"resourceType": "CapabilityStatement"})
    )
    mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$lookup").mock(return_value=httpx.Response(404))

    health = await client.health(code_systems=[LOINC])

    assert health.reachable is True
    assert health.code_systems[0].version is None


@pytest.mark.parametrize("status", [500, 404])
async def test_a_failing_metadata_endpoint_is_reported_unreachable(
    client: FhirTerminologyClient, mock_http: respx.MockRouter, status: int
) -> None:
    mock_http.get(f"{TERMINOLOGY_URL}/metadata").mock(return_value=httpx.Response(status))

    assert (await client.health()).reachable is False


# --- Credentials never leak ----------------------------------------------


def test_the_repr_does_not_carry_the_authorization_header() -> None:
    client = FhirTerminologyClient(
        base_url=TERMINOLOGY_URL,
        auth_mode=TerminologyAuthMode.BEARER,
        token="sk-super-secret-terminology-token",
    )

    assert "sk-super-secret" not in repr(client)
    assert "sk-super-secret" not in str(client)
    assert repr(client) == f"FhirTerminologyClient(base_url={TERMINOLOGY_URL!r})"


async def test_basic_auth_is_sent_when_configured(mock_http: respx.MockRouter) -> None:
    client = FhirTerminologyClient(
        base_url=TERMINOLOGY_URL,
        auth_mode=TerminologyAuthMode.BASIC,
        username="tx",
        password="secret",
    )
    route = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )

    await client.validate_code(system=LOINC, code="8867-4")

    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")


async def test_no_authorization_header_is_sent_when_no_auth_is_configured(
    client: FhirTerminologyClient, mock_http: respx.MockRouter
) -> None:
    route = mock_http.post(f"{TERMINOLOGY_URL}/CodeSystem/$validate-code").mock(
        return_value=fhir_json(parameters(result=True))
    )

    await client.validate_code(system=LOINC, code="8867-4")

    assert "Authorization" not in route.calls[0].request.headers
