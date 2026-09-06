# Delivery

FHIR at Will can compile a validated, destination-bound write plan and submit it to a
FHIR R4 server. Delivery is disabled by default. The only target implemented in this
release is `generic-fhir-r4`; vendor-specific and GCC EHR adapters remain planned.

Delivery is a separate plane after terminology binding and validation. Preflight and
write-plan results do not add a ninth layer to the public eight-layer validation
report. See [ADR 0008](adr/0008-delivery-not-validation-layer.md).

## Endpoints and authorization

All delivery endpoints require a Bearer API key with the `deliveries:write` scope:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/targets` | List target descriptors implemented by this build |
| `POST` | `/v1/write-plan` | Bind, validate, preflight, and compile without submitting |
| `POST` | `/v1/deliver` | Rebuild the verified plan and submit it to the target |

`POST /v1/write-plan` and `POST /v1/deliver` require these request headers:

| Header | Purpose |
|---|---|
| `X-Target-Id` | Target descriptor id; currently only `generic-fhir-r4` |
| `X-Target-Base-Url` | Absolute base URL of the destination FHIR R4 server |
| `X-Target-Token` | Request-scoped Bearer token for the destination |
| `X-PHI-Egress-Acknowledged` | Must be `true` for an external destination by default |

The target token is wrapped as a secret, used only for the request, and never stored,
logged, or returned. Target details and clinical resources belong in headers and
request bodies, never URLs or query parameters.

Both POST endpoints accept:

```json
{
  "conversion_id": "conv_opaque",
  "bundle": {"resourceType": "Bundle", "type": "collection", "entry": []},
  "context": {
    "patient_ref": "Patient/123",
    "encounter_ref": "Encounter/456",
    "author_ref": "Practitioner/789",
    "encounter_start": "2026-09-06T08:00:00Z"
  },
  "profiles": [],
  "max_terminology_checks": 500
}
```

The clinical Bundle and destination context are PHI and must be protected
accordingly. `POST /v1/deliver` additionally accepts `human_attested` and
`reviewer_id`, and requires an `Idempotency-Key` header. `reviewer_id` is an
opaque identifier (letters, digits, `.`, `_`, or `-`), never a name or email.

## SubjectContext and wrong-patient preflight

`SubjectContext` supplies destination-native chart references. These references must
come from a trusted destination session, such as a SMART launch or an operator's HIS.
The service never infers identity from the narrative and does not search by
demographics.

Before planning a write, the service reads the referenced Patient and, when supplied,
Encounter from the target. It compares available narrative evidence against the
destination context:

- stated gender must match the destination Patient;
- a stated age must be within one year of the age computed from `birthDate`; and
- a stated Encounter start must match the destination Encounter period start.

Checks with no narrative evidence are marked `skipped`, not passed. A mismatch fails
preflight and makes the plan ineligible for delivery. A fully skipped preflight
requires human attestation before submission.

During write planning, collection-local Patient, Encounter, and Practitioner
references are rebound to `SubjectContext`. Patient and Encounter resources from the
generated collection are excluded rather than created in the destination chart.

## Binding, validation, and write planning

Each delivery request rebuilds its evidence in this order:

1. bind text-only concepts and units, applying only terminology-verified codes;
2. run the public eight-layer validation cascade;
3. run destination context preflight;
4. rebind destination references and compile target-specific write operations; and
5. for `/v1/deliver`, submit only if the report and plan are eligible.

The response keeps the reports separate:

- `validation` is the unchanged eight-layer validation report;
- `plan.preflight` records destination identity checks;
- `plan.notes` records exclusions, stripped elements, and blocked resources;
- `plan.steps` records dependencies and deterministic per-resource identifiers; and
- `/v1/deliver` adds a receipt with the delivery id and bounded result metadata.

A rejected validation report, failed preflight, blocked write step, or unavailable
required dependency prevents submission. A `needs_review` validation decision, or a
preflight in which every check was skipped, requires both `human_attested: true` and
a non-empty `reviewer_id`. Attestation records a human decision; it does not turn
automated processing into clinical validation.

## Delivery modes and egress

Delivery has an independent default-deny switch:

| `DELIVERY_MODE` | Behavior |
|---|---|
| `off` | Blocks target reads, write planning, and submission |
| `plan_only` | Allows target preflight and write-plan compilation; blocks submission |
| `submit` | Allows planning and submission |

`TARGET_EGRESS_ALLOWLIST` is a comma-separated list of exact destination hostnames.
Every target read and write is blocked unless its hostname is listed, or
`LOCAL_ONLY_MODE=true` and the hostname is loopback. `DELIVERY_MODE=submit` cannot
start without one of those egress controls.

Example:

```dotenv
DELIVERY_MODE=plan_only
TARGET_EGRESS_ALLOWLIST=ehr-sandbox.example.org
```

Use `submit` only after reviewing the plan, target authorization, TLS, network
boundaries, retention, and incident procedures. Production target URLs and all
non-loopback endpoints should use HTTPS.

## Generic FHIR R4 target

`generic-fhir-r4` compiles ready resources into one FHIR transaction Bundle and sends
it to the target base URL. FHIR transaction processing is atomic: the target accepts
all entries or rejects the transaction.

Every resource receives a deterministic delivery identifier and a conditional-create
request. `POST /v1/deliver` also requires a request-level `Idempotency-Key`. Reusing
the same key with different request content is rejected. These controls defend
separately against replaying the whole delivery and duplicating an individual
resource.

Future sequential adapters will stop on the first failed step and report partial
progress. They will not claim rollback unless the destination provides an explicit
atomic or compensating operation. See
[ADR 0009](adr/0009-delivery-failure-idempotency.md).

For the opt-in real-target integration test, start the pinned disposable HAPI
server with `docker compose --profile integration up -d hapi`, set
`FHIR_TARGET_URL=http://localhost:8090/fhir`, and run
`uv run pytest tests/integration/test_delivery_target.py`.

## Security and privacy boundaries

- Use least-privileged target tokens limited to the destination and permitted writes.
- Protect API and target credentials with TLS; never place them in logs or durable
  request records.
- Responses use `Cache-Control: no-store`; clients must apply equivalent handling.
- The append-only, tenant-scoped delivery ledger is PHI-free. It records opaque ids,
  target id, status, response status, counts, duration, and time, not resources or
  target response bodies.
- Logs and metrics contain bounded decisions, identifiers, counts, and durations,
  never resource bodies, target responses, headers, tokens, or clinical values.
- Terminology and target outages, blocked egress, failed preflight, and unsupported
  target ids fail closed.

Operators remain responsible for destination authorization, TLS, access control,
retention, backups, terminology licensing, provider agreements, incident response,
and applicable regulatory obligations. Enabling delivery is not a compliance
determination.
