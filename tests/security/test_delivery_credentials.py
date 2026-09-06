from __future__ import annotations

from fhirbridge.delivery.invocation import TargetInvocation


def test_target_token_is_secret_in_representations() -> None:
    token = "synthetic-target-token-do-not-log"
    invocation = TargetInvocation.from_headers(
        target_id="generic-fhir-r4",
        base_url="https://fhir.example",
        token=token,
    )
    assert token not in repr(invocation)
    assert invocation.token.get_secret_value() == token
