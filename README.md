<p align="center">
  <img src="assets/fhir-at-will-logo.png" alt="FHIR at Will logo" width="720">
</p>

<p align="center">
  <strong>Generate FHIR. Verify every result.</strong><br>
  Open source · self-hostable · FHIR R4 · BYOK/BYOM
</p>

# FHIR at Will

This repository is the self-hostable **production API** for FHIR at Will. It is a
verification-first HTTP service for FHIR R4: it validates existing resources and
can turn clinical narrative into a FHIR Bundle using a caller-supplied model and
provider key.

The conversion, de-identification, validation, terminology binding, and
write-planning algorithms are **not** implemented here. They live in the Python
library [`fhiratwill`](https://pypi.org/project/fhiratwill/)
([source: fhirbridge](https://github.com/Safwanmahmoud/fhirbridge)). This service
depends on that package and wraps it with FastAPI, API-key authentication,
tenant PostgreSQL, the HL7 validator sidecar, BYOK policy, observability, and
container images.

| Name | What it is |
|---|---|
| **FHIR at Will** | Public product |
| **This repository** ([FHIR-It-Will](https://github.com/Safwanmahmoud/FHIR-It-Will)) | Production HTTP API, Docker/Railway images, and deployment adapters |
| **`fhirbridge`** | This service's Python package, container, and API title |
| **[`fhiratwill`](https://github.com/Safwanmahmoud/fhirbridge)** | The installable library that actually processes narrative, FHIR, and plans |

If you want to call the pipeline from your own Python code, install
[`fhiratwill`](https://pypi.org/project/fhiratwill/) and read the
[library README](https://github.com/Safwanmahmoud/fhirbridge#readme). This
document is about deploying and calling the HTTP API.

The generated Bundle is never presented as trusted output. It is returned beside a
structured report covering conformance, terminology, clinical plausibility, skipped
checks, version provenance, and the final routing decision.

> [!WARNING]
> This project is alpha software and is not a medical device. Use synthetic data in
> public demos. Self-hosting alone does not make a deployment HIPAA or GDPR compliant.

## Try it

- [Interactive playground](https://fhiratwill.com/playground.html)
- [Website and API guide](https://fhiratwill.com/docs.html)
- [This API (source)](https://github.com/Safwanmahmoud/FHIR-It-Will)
- [Python library `fhiratwill` (source)](https://github.com/Safwanmahmoud/fhirbridge)

The hosted playground supports:

- **Narrative → FHIR** — bring an OpenRouter key and use `POST /v1/NAR2FHIR` to
   generate a FHIR Bundle.

## What works today

| Capability | Status |
|---|---|
| Validate a FHIR R4 resource or Bundle | Implemented |
| Profile and invariant validation with the HL7 validator | Implemented |
| Terminology validation within the validation cascade | Implemented |
| Clinical plausibility rules | Implemented |
| Grounded BYOK narrative extraction with deterministic FHIR assembly (`/v1/NAR2FHIR`) | Implemented |
| Nearest-neighbor terminology binding (ICD-10-CM by default; `/v1/bind`) | Implemented |
| Dictated-audio conversion via speech-to-text (`/v1/VOICE2FHIR`) | Implemented |
| Generic FHIR R4 write planning, context preflight, and transaction delivery | Implemented |
| FHIR `OperationOutcome` validation response | Implemented |
| API-key authentication and tenant-aware PostgreSQL RLS | Implemented |
| JSON logs, Prometheus metrics, and opt-in OpenTelemetry tracing | Implemented |


The current build targets:

- FHIR `4.0.1`;
- US Core `hl7.fhir.us.core#9.0.0`;
- HL7 validator CLI `6.10.2`; and
- a configurable terminology server (`https://tx.fhir.org/r4` by default).

`GET /v1/capabilities` reports implemented and unavailable functionality at runtime.

This service's HTTP contract, policy gates, and sidecar adapters sit on top of
[`fhiratwill`](https://pypi.org/project/fhiratwill/). How the cascade, assembly,
binding, and write-planning actually work is documented in the
[library README](https://github.com/Safwanmahmoud/fhirbridge#readme).

## How validation works

The eight-layer cascade is `fhiratwill.validate`. This service injects the HL7
validator sidecar and the terminology-server adapter so profile, terminology, and
invariant layers can run, then returns the library's report over HTTP.

Every report contains all eight layers. A check that could not run is marked
`skipped` or `not_applicable`; it is never allowed to look like a pass.

| Layer | Check | Current state |
|---:|---|---|
| L1 | Structural FHIR R4 parsing and resource allowlist | Implemented |
| L2 | Declared and requested profile conformance | Implemented |
| L3 | Code validity and ValueSet bindings | Implemented |
| L4 | FHIRPath invariants | Implemented |
| L5 | Physiological, temporal, and dose plausibility | Implemented |
| L6 | Source-to-output fidelity | Not applicable until M3 |
| L7 | Omitted clinical mention coverage | Not applicable until M3 |
| L8 | Auto-accept, review, or reject routing | Implemented from available signals |

The response separates two related decisions:

- `conformant` is `true` when no blocking issue was found by a layer that ran.
- `status` is `auto`, `needs_review`, or `reject`.

A conformant resource can still need review because of warnings. A non-conformant
resource is returned as a successful HTTP `200` validation report; HTTP errors are
reserved for invalid requests, policy failures, and unavailable dependencies.

FHIR conformance also does not prove clinical correctness. For example, a heart rate
of `44000 /min` may be structurally valid but is rejected by L5 as physiologically
impossible.

L1 uses the `fhir.resources` R4B typed models for parsing and round-tripping. The
pinned HL7 validator remains the conformance authority for FHIR R4 `4.0.1` in L2/L4.

## Architecture

```mermaid
flowchart LR
    Client[API client] -->|Bearer API key| API[FastAPI]
    Playground[Hosted playground] -->|server-held FHIR key| API

    API --> Cascade[Validation cascade]
    Cascade --> Models[L1 typed FHIR models]
    Cascade --> Validator[L2 + L4<br>HL7 validator sidecar]
    Cascade --> Terminology[L3<br>FHIR terminology server]
    Cascade --> Rules[L5<br>versioned rule pack]
    Cascade --> Routing[L8 routing]

    API --> Convert[Grounded narrative conversion]
    Convert -->|X-LLM-* / X-STT-* BYOK headers| Gateway[LLM policy gateway]
    Gateway -->|caller's key| Provider[LLM provider]
    Convert --> Extract[One structured extraction call]
    Extract --> Assemble[Deterministic Bundle assembly]
    Assemble --> Bind[Terminology binding]
    Bind -->|verified codes only| Cascade
    Cascade -->|delivery requests only| Preflight[Destination context preflight]
    Preflight --> Plan[Write-plan compiler]
    Plan --> Submit[Generic FHIR R4 transaction]

    API --> DB[(PostgreSQL<br>tenant RLS)]
    API --> Observability[Logs / metrics / traces]
```

Boxes labeled Cascade, Convert, Assemble, Bind, and Plan are
[`fhiratwill`](https://github.com/Safwanmahmoud/fhirbridge) running inside this
process. The API process supplies HTTP, auth, BYOK policy, sidecar clients, and
delivery submission.

The validator has no authentication and can fetch external resources, so it must stay
on a private network. PostgreSQL migrations run as the database owner, while the API
runs as a separate least-privileged role. `/readyz` refuses readiness if row-level
security does not apply to that role.

## Deploy on Railway

The repository includes a four-service Railway definition for the API, private
validator sidecar, PostgreSQL, and Redis. It defaults to a staging sandbox with
OpenRouter egress enabled and unqualified models allowed. On first deploy, save the
one-time API key from the API service's pre-deploy logs. Applying the infrastructure
definition requires Railway CLI `5.42.1` or newer. See the
[Railway template guide](docs/railway-template.md) for generated-secret configuration,
the sandbox safety boundary, and marketplace setup.

The one-click marketplace button will be added here after the public template
is published.

## Quick start with Docker

### Prerequisites

- Docker Engine or Docker Desktop with Compose v2;
- at least 4 GB of memory for the validator build/runtime; and
- network access while the validator image caches its pinned IG package.

### 1. Configure

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Change both development passwords in `.env`:

```dotenv
POSTGRES_PASSWORD=choose-an-owner-password
APP_DB_PASSWORD=choose-a-different-app-password
```

For Docker Compose, edit the variables under `.env.example`'s **Compose only**
heading. Compose supplies its own internal `DATABASE_URL`, `REDIS_URL`, and
`VALIDATOR_URL`; their host-oriented values in `.env` are for processes run directly
with `uv`. The API connects as `APP_DB_USER` with `APP_DB_PASSWORD`, never as the
PostgreSQL owner.

### 2. Provision the database and first API key

```bash
docker compose --profile setup run --rm bootstrap
```

This applies migrations, creates the least-privileged application role, provisions a
tenant, and prints an API key. The key is shown once; only its Argon2id hash is stored.
The bootstrap key receives `documents:write`, `conversions:write`, `facts:read`, and
`reviews:write`; it deliberately excludes `phi:read`, `reviews:submit`,
`deliveries:write`, `credentials:write`, and `admin`.

If that plaintext key is lost, issue a replacement for the existing tenant instead of
running bootstrap again:

```bash
docker compose --profile setup run --rm --entrypoint python bootstrap \
  scripts/issue_api_key.py --only-tenant --scope conversions:write
```

Without `--scope`, a replacement is unscoped and can call validation only. The
`conversions:write` scope is required for NAR2FHIR and VOICE2FHIR; repeat `--scope`
for additional permissions. The command uses the bootstrap service's schema-owner
connection, prints the new key once, and does not revoke old keys.

### 3. Start the stack

```bash
docker compose up -d
docker compose ps
```

The API is available at `http://localhost:8000`. PostgreSQL, Redis, and the validator
remain private to the Compose network. Redis is not used by the current synchronous
M0–M2 request path, but Compose still requires it to become healthy before starting
the API.

### 4. Check the deployment

```bash
curl http://localhost:8000/livez
curl http://localhost:8000/readyz
curl http://localhost:8000/version
```

- `/livez` checks the API process.
- `/readyz` checks PostgreSQL isolation, the validator, and terminology.
- `/version` returns the code, FHIR and typed-model versions, prompt/report/fact schema
  versions, IG packages, validator version, and environment.

OpenAPI is available at `http://localhost:8000/docs`.

## Validate a resource

All compute endpoints require a Bearer API key:

```http
Authorization: Bearer fhirb_...
```

The example below is a conformant heart-rate Observation. It uses the UCUM display
`/min`; `"beats/minute"` is not the canonical display for that code and may be rejected
by terminology validation.

```python
import httpx

base_url = "http://localhost:8000"
api_key = "fhirb_..."  # value printed by bootstrap

observation = {
    "resourceType": "Observation",
    "text": {
        "status": "generated",
        "div": '<div xmlns="http://www.w3.org/1999/xhtml">Heart rate 72/min</div>',
    },
    "status": "final",
    "category": [
        {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                    "code": "vital-signs",
                    "display": "Vital Signs",
                }
            ]
        }
    ],
    "code": {
        "coding": [
            {
                "system": "http://loinc.org",
                "code": "8867-4",
                "display": "Heart rate",
            }
        ]
    },
    "subject": {"reference": "Patient/example"},
    "performer": [{"reference": "Practitioner/example"}],
    "effectiveDateTime": "2024-01-15T09:30:00Z",
    "valueQuantity": {
        "value": 72,
        "unit": "/min",
        "system": "http://unitsofmeasure.org",
        "code": "/min",
    },
}

response = httpx.post(
    f"{base_url}/v1/validate",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"resource": observation},
    timeout=180,
)
response.raise_for_status()

report = response.json()
print(report["status"])  # auto
print(report["conformant"])  # True
for layer in report["layers"]:
    print(layer["layer_number"], layer["layer"], layer["status"])
```

Request options:

```json
{
  "resource": {"resourceType": "Patient"},
  "profiles": ["http://example.org/StructureDefinition/my-profile"],
  "layers": ["structural", "profile", "terminology"],
  "severity_overrides": {"fb-plaus-heart-rate": "warning"},
  "max_terminology_checks": 500
}
```

A bare FHIR resource is also accepted with
`Content-Type: application/fhir+json`.

## NAR2FHIR: convert narrative to FHIR

`POST /v1/NAR2FHIR` is the HTTP front door for `fhiratwill.text2fhir`:
synchronous, stateless, and BYOK. It makes **one** model call, which extracts
catalog-constrained resource types, keys, and values, each tagged with an
`instance` key identifying which real-world thing it describes. Assembly into
typed FHIR is then deterministic Python in the library: no model sees the
Bundle, so the same entities always produce the same Bundle.

This service adds the Bearer key, `X-LLM-*` headers, egress and qualification
gates, and a reviewed extraction-rule overlay composed onto the published
library prompt. For the library call shape (`text2fhir`, `LlmClient`,
`assemble_bundle`), see the
[fhirbridge README](https://github.com/Safwanmahmoud/fhirbridge#readme).

The model-versus-assembly boundary is deliberate. Choosing a FHIR datatype has one correct answer and
does not need a model, while a model asked to do it may invent a
`Coding.system`/`code` pair or nest a string where an object belongs. Assembly
therefore refuses rather than approximates — `"62-year-old"` does not become a
`birthDate`, and `"128/82 mmHg"` does not become a `Quantity` of 128 — and coded
concepts leave assembly as text only. A separate nearest-neighbor binding stage
then codes `CodeableConcept.text` against the loaded dictionaries (ICD-10-CM by
default). Ambiguous or low-confidence matches stay as text. `POST /v1/bind`
exposes the same catalog search for a free-text phrase.

It does not validate the generated Bundle. The response returns:

- `bundle` — the generated FHIR R4 Bundle;
- `validated` — always `false`;
- `assembly` — every element dropped, inferred, wired, or in conflict, with a
  reason. PHI-free: it names entry indexes and element names, never values;
- `binding` — PHI-free terminology coverage, per-element actions, candidate counts,
  and the versioned binding table used to produce verified coding;
- `llm` — model, token, cost, latency, and qualification metadata; and
- `conversion_id` — an opaque correlation identifier, not a persisted job.

Read `assembly` and `binding` before the Bundle. FHIR requires elements a narrative
rarely states — `Observation.status`, `Encounter.class`, `MedicationRequest.intent` —
and assembly fills those from a reviewed constant table, marking the resource
`machine-inferred` and listing each one as an `inferred` note. Such a value is
reproducible and auditable but is not evidence about the patient.

There is no `profiles` field on the request. Assembly validates nothing, so it
cannot honor a profile; pass profiles to `POST /v1/validate` instead.

### Extraction rules

Where the narrative's shape and FHIR's shape disagree, a reviewed rule pack in
[`src/fhirbridge/llm/extraction_rules.py`](src/fhirbridge/llm/extraction_rules.py)
tells the model what to do. That pack is **this service's overlay** on the
published `fhiratwill` extraction prompt; it did not move with the core split.
The composed prompt is pinned by this service's prompt fingerprint
(`PROMPT_SET_VERSION` `v5.6.0`), so adding a rule means appending to
`EXTRACTION_RULES` and bumping that version.

| Rule | Effect |
|---|---|
| An age is not a birth date | `62-year-old` becomes an `Age` Observation of `62 years`; `Patient.birthDate` is never computed from an age |
| One measurement per value | `128/82 mmHg` becomes separate systolic and diastolic Observations |
| Resolve dates only against a stated anchor | Relative dates resolve only when the narrative states the anchor, at the precision the phrase supports |
| Never turn a denial or a relative's history into a diagnosis | A denied condition becomes `verificationStatus: refuted`; a family member's condition becomes `FamilyMemberHistory` |
| Split a medication phrase | `metformin` and `500 mg by mouth twice daily` land in separate elements |
| One instance per real-world thing | Each distinct measurement, condition, encounter, and medication gets its own `instance` |

A rule may not license a guess. Where a fact cannot be represented, the rule says to
leave the source wording so coercion refuses it and `assembly` names the gap — a
reported gap being a better outcome than a plausible fabrication.

Submit the returned `bundle` separately to `POST /v1/validate` before trusting it.

The service does not hold an LLM key. Supply invocation details on every request:

| Header | Purpose |
|---|---|
| `X-LLM-Provider` | Provider id; defaults to `openrouter` |
| `X-LLM-Model` | Provider model id; required |
| `X-LLM-API-Key` | Caller-owned provider key; required |
| `X-LLM-Base-Url` | Optional endpoint override |
| `X-LLM-Extra-Headers` | Optional JSON object of provider headers |
| `X-PHI-Egress-Acknowledged` | Must be `true` for external clinical-data egress |

Before using an external provider in local development, explicitly enable that egress.
For Compose, add a `docker-compose.override.yml`:

```yaml
services:
  api:
    environment:
      # Local HTTP only. Never enable insecure transport with real PHI or keys.
      ALLOW_INSECURE_TRANSPORT: "true"
      LLM_EGRESS_ALLOWLIST: openrouter.ai
      # Unknown models resolve to "unqualified".
      MIN_QUALIFICATION_TIER: unqualified
```

Then recreate the API:

```bash
docker compose up -d --force-recreate api
```

Example:

```python
response = httpx.post(
    f"{base_url}/v1/NAR2FHIR",
    headers={
        "Authorization": f"Bearer {api_key}",
        "X-LLM-Provider": "openrouter",
        "X-LLM-Model": "openai/gpt-4.1-nano",
        "X-LLM-API-Key": "sk-or-...",  # your key; never commit it
        "X-PHI-Egress-Acknowledged": "true",
    },
    json={
        "text": (
            "62-year-old male seen for follow-up. "
            "Blood pressure 128/82 mmHg. Takes metformin 500 mg twice daily."
        )
    },
    timeout=300,
)
response.raise_for_status()

result = response.json()
assert result["validated"] is False
print(result["llm"]["model"], result["llm"]["cost_usd"])

# Read what could not be grounded before reading the Bundle.
for note in result["assembly"]:
    print(note["action"], note["resource_type"], note["element"], note["detail"])

validation = httpx.post(
    f"{base_url}/v1/validate",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"resource": result["bundle"]},
    timeout=300,
)
validation.raise_for_status()
print(validation.json()["status"])
```

The model must support structured JSON output. Prose, truncated JSON, or output outside
the required schema returns `422 llm-schema-violation`. Model availability and
capabilities vary by provider.

## VOICE2FHIR: convert dictated audio to FHIR

`POST /v1/VOICE2FHIR` is the HTTP front door for `fhiratwill.voice2fhir`:
`NAR2FHIR` with a transcription step in front. It transcribes dictated clinical
audio verbatim, then runs the transcript through the exact same grounded
extraction and deterministic assembly, so nothing about the conversion changes
because the narrative arrived as speech. It returns everything `NAR2FHIR` does, plus:

- `transcript` — the verbatim text the model heard, and the input to extraction; and
- `transcription` — the dictation call's provider, model, token, cost, and latency.

The transcript is returned on purpose. Dictation can mishear a clinically decisive word
(`no chest pain` becoming `chest pain`), and a reviewer cannot catch that from the Bundle
alone. Read it against the audio before trusting the result.

Dictation is a **separate** BYOK call. litellm cannot transcribe through OpenRouter, so
audio goes to a speech-to-text provider (Gemini by default, also OpenAI, Groq, WatsonX,
...) on its own key, supplied in `X-STT-*` headers alongside the `X-LLM-*` extraction
headers. Both calls pass the same provider, egress-allowlist, and PHI-acknowledgement
gates; the qualification tier is not applied to dictation, because that gate ranks models
that reason over clinical meaning, not ones that transcribe. Add the dictation provider's
host to `LLM_EGRESS_ALLOWLIST` (Gemini is `generativelanguage.googleapis.com`).

| Header | Purpose |
|---|---|
| `X-STT-Provider` | Speech-to-text provider id; defaults to `gemini` |
| `X-STT-Model` | Provider transcription model id; required |
| `X-STT-API-Key` | Caller-owned provider key; required |
| `X-STT-Base-Url` | Optional endpoint override |
| `X-STT-Extra-Headers` | Optional JSON object of provider headers |
| `X-STT-Language` | Optional spoken-language hint |

The audio is uploaded as `multipart/form-data` (never a query parameter) and, like the
transcript, never reaches a log. Example:

```python
with open("dictation.wav", "rb") as audio:
    response = httpx.post(
        f"{base_url}/v1/VOICE2FHIR",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-LLM-Provider": "openrouter",
            "X-LLM-Model": "openai/gpt-4.1-nano",
            "X-LLM-API-Key": "sk-or-...",  # your extraction key
            "X-STT-Provider": "gemini",
            "X-STT-Model": "gemini-2.5-flash",
            "X-STT-API-Key": "...",  # your dictation key; never commit it
            "X-PHI-Egress-Acknowledged": "true",
        },
        files={"audio": ("dictation.wav", audio, "audio/wav")},
        timeout=300,
    )
response.raise_for_status()
result = response.json()

# Verify the dictation before trusting anything built from it.
print(result["transcript"])
```

Supported audio formats are wav, mp3, m4a/mp4, aac, flac, ogg/opus, aiff, and webm; other
uploads return `415`. Audio above `MAX_UPLOAD_BYTES` (25 MiB by default) returns `413`,
and audio with no discernible speech returns `422`. `/v1/VOICE2FHIR` requires the
`conversions:write` scope.

## API surface

### Public

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/livez` | Process liveness |
| `GET` | `/readyz` | Dependency and RLS readiness |
| `GET` | `/version` | Reproducibility pins |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/v1/error-codes` | Error catalogue as a FHIR `CodeSystem` |
| `GET` | `/fhir/R4/metadata` | FHIR `CapabilityStatement` |
| `GET` | `/docs` | Interactive OpenAPI documentation |
| `GET` | `/openapi.json` | OpenAPI contract |

### Authenticated

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health/dependencies` | Detailed dependency health |
| `GET` | `/v1/capabilities` | Implemented and planned capabilities |
| `GET` | `/v1/igs` | Preloaded implementation guides |
| `POST` | `/v1/validate` | Structured validation report |
| `POST` | `/v1/validate/outcome` | Validation as `OperationOutcome` |
| `POST` | `/v1/deidentify` | Replace detected narrative identifiers using the enforced de-identification profile |
| `POST` | `/v1/NAR2FHIR` | Grounded BYOK extraction, deterministic FHIR assembly, ICD-10-CM binding  |
| `POST` | `/v1/bind` | Bind a free-text phrase to the nearest catalog code (ICD-10-CM by default) |
| `POST` | `/v1/VOICE2FHIR` | Transcribe dictated audio, then convert as `/v1/NAR2FHIR`  |
| `GET` | `/v1/targets` | List implemented delivery targets |
| `POST` | `/v1/write-plan` | Verify destination context and compile a target write plan |
| `POST` | `/v1/deliver` | Submit an eligible plan with request and resource idempotency |

Validation endpoints require authentication but no specific scope. `/v1/NAR2FHIR`,
`/v1/VOICE2FHIR`, and `/v1/bind` require `conversions:write`; a missing required
scope returns `403 forbidden`. Delivery endpoints require `deliveries:write`; see the
[delivery guide](docs/delivery.md) for target headers and safeguards.

## Fail-closed behavior

The API does not silently downgrade verification:

- unavailable validator or terminology dependencies return `503`;
- binding refuses ambiguous or unverifiable codes and preserves the original text;
- an unknown profile returns `422 ig-not-loaded`;
- blocked LLM egress returns `451`;
- delivery is disabled unless `DELIVERY_MODE` permits the requested operation;
- target reads and writes are blocked unless target egress policy permits the host;
- failed wrong-patient preflight or an ineligible write plan prevents submission;
- a fully skipped preflight or `needs_review` validation requires human attestation;
- missing or rejected provider credentials return `400`;
- credentials sent over disallowed plaintext transport return `400`;
- unacknowledged external PHI egress returns `422`;
- unqualified, over-budget, or malformed model output returns `422`;
- provider rate limits return `429` and may include `Retry-After`; and
- every report names skipped or not-applicable layers.

Platform errors use a JSON `error` envelope with `code`, `message`, `trace_id`, and
optional `details`. Clinical, dependency, and BYOK policy failures use a FHIR
`OperationOutcome`; `issue.details.coding` carries a stable code listed by
`GET /v1/error-codes`. Every response includes `X-Request-Id` and `X-Trace-Id`.

## Important configuration

See [`.env.example`](.env.example) for the complete development configuration.

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Least-privileged PostgreSQL connection | Required |
| `REDIS_URL` | Required configuration reserved for future M3 jobs | Required |
| `VALIDATOR_URL` | Private validator sidecar | Required; `.env.example` uses `http://localhost:8081` |
| `TERMINOLOGY_URL` | FHIR terminology server | Required; `.env.example` uses `https://tx.fhir.org/r4` |
| `DEFAULT_IG_PACKAGES` | IGs named in reports | `hl7.fhir.us.core#9.0.0` |
| `VALIDATOR_VERSION` | Validator version named in reports | Unset in code; Compose sets `6.10.2` |
| `FHIRBRIDGE_ENV` | `development`, `staging`, or `production` | `development` |
| `FHIRBRIDGE_EPHEMERAL_KEY` | Ephemeral encryption key required in production | Unset |
| `REQUIRE_RLS_ENFORCEMENT` | Fail readiness if RLS is bypassed | `true` |
| `LLM_MODE` | Credential mode; this build supports BYOK | `byok` |
| `ALLOW_INSECURE_TRANSPORT` | Permit credentials over HTTP | `false` |
| `LLM_EGRESS_ALLOWLIST` | Permitted external LLM hosts | Empty; blocks all |
| `DELIVERY_MODE` | Delivery plane: `off`, `plan_only`, or `submit` | `off` |
| `TARGET_EGRESS_ALLOWLIST` | Permitted exact target hostnames | Empty; blocks all |
| `LLM_ALLOWED_PROVIDERS` | Permitted provider ids | `*` |
| `LOCAL_ONLY_MODE` | Restrict LLM calls to loopback hosts | `false` |
| `REQUIRE_PHI_EGRESS_ACK` | Require explicit external PHI acknowledgement | `true` |
| `DEID_MODE` | Narrative de-identification: `off`, `advisory`, or `enforced` | `off` |
| `DEID_PROFILE` | HIPAA minimization profile | `hipaa_safe_harbor` |
| `DEID_ALLOW_AUDIO_EGRESS` | Permit identifying audio egress while de-identification is enforced | `false` |
| `MIN_QUALIFICATION_TIER` | Minimum model tier | `bronze` |
| `MAX_COST_USD_PER_CONVERSION` | Worst-case model cost cap | `1.00` USD |
| `MAX_UPLOAD_BYTES` | Maximum VOICE2FHIR audio size | `26214400` (25 MiB) |
| `CREDENTIAL_STORAGE` | Provider credential persistence | `disabled` |
| `DEBUG_CAPTURE_LLM_IO` | Capture prompts/completions in development | `false` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Enable OpenTelemetry export | Unset |

The public `tx.fhir.org` service has no SLA. It receives codes rather than complete
resources, but those codes may still be sensitive in context. Production deployments
should use an appropriately licensed terminology service with suitable availability
and privacy guarantees.

Production mode additionally requires `FHIRBRIDGE_EPHEMERAL_KEY` containing exactly
32 random bytes encoded with URL-safe base64, rejects the public `tx.fhir.org` default,
forbids insecure transport and LLM I/O capture, and requires HTTPS for a non-loopback
validator URL.

## Security and privacy

- API keys are stored as Argon2id hashes.
- Provider keys are held as in-memory secrets for the request lifetime and are not
  stored, logged, or returned. Persistent credential storage is disabled in this build.
- Optional enforced narrative de-identification replaces detected identifiers with
  random request-local tokens before model egress and restores them locally before
  assembly. See [the de-identification guide](docs/deidentification.md).
- Validation resources and conversion narratives are processed and dropped.
- Validation and conversion responses use `Cache-Control: no-store`.
- Tenant tables use PostgreSQL row-level security.
- Readiness checks that the API role is actually subject to RLS.
- Terminology requests use POST bodies rather than query strings.
- Logs record decisions and counts, not resource bodies or validator messages that may
  quote clinical values.
- The validator is designed for a private network only.

Operators remain responsible for TLS, access control, backups, retention, terminology
licensing, provider agreements, incident response, and all infrastructure compliance.
The de-identification report is evidence about processing, not a HIPAA compliance
determination.

## Development

Python `3.12` and [`uv`](https://docs.astral.sh/uv/) are the supported path:

```bash
uv sync --group dev
uv run pytest -q -m "not integration"
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs.py
uv run python scripts/check_notebooks.py
uv run pip-audit
```

Integration tests require Docker and start PostgreSQL through testcontainers. Validator
and terminology sidecar tests run only when `VALIDATOR_URL` and `TERMINOLOGY_URL` are
supplied:

```bash
uv run pytest -q -m integration
```

The quick text-to-FHIR notebook is at
[`quick-test/text2fhir_quick_test.ipynb`](quick-test/text2fhir_quick_test.ipynb):

```bash
python -m pip install -r quick-test/requirements.txt
python -m jupyter lab quick-test/text2fhir_quick_test.ipynb
```

The notebook prompts without echo for the OpenRouter key and, unless
`FHIRBRIDGE_API_KEY` is set, the FHIR API key. That FHIR key must include
`conversions:write`. It starts the local Compose stack, calls `/v1/NAR2FHIR`, and
submits the generated Bundle to `/v1/validate`. Use synthetic data only and clear
outputs before committing.

`requirements.txt`, `requirements-dev.txt`, and `requirements-notebook.txt` are
committed exports of `uv.lock`. Regenerate them with the command in each file's header
after changing dependencies.

## Repository layout

The processing core (`validate`, `text2fhir`, `assemble_bundle`, `bind_bundle`,
`compile_write_plan`) is the `fhiratwill` dependency. Paths below are this
service's HTTP, policy, persistence, and sidecar adapters.

| Path | Contents |
|---|---|
| `src/fhirbridge/api/` | FastAPI app, auth, schemas, middleware, and routers |
| `src/fhirbridge/llm/` | BYOK gateway, policy gates, qualification, service extraction-rule overlay, and the adapter that calls `fhiratwill` |
| `src/fhirbridge/validation/` | Service adapter around `fhiratwill.validate`, plus metrics |
| `src/fhirbridge/fhir/` | HL7 validator HTTP client, `OperationOutcome`, resource allowlist |
| `src/fhirbridge/terminology/` | FHIR terminology-server HTTP client |
| `src/fhirbridge/delivery/` | Destination preflight, write-plan headers, and transaction submission |
| `src/fhirbridge/storage/` | SQLAlchemy models, tenant sessions, and RLS checks |
| `src/fhirbridge/observability/` | Logging, redaction, metrics, and tracing |
| `src/fhirbridge/domain/` | Service error codes and identifiers |
| `docker/` | API and validator images |
| `alembic/` | Database migrations |
| `scripts/bootstrap.py` | App-role, tenant, and API-key provisioning |
| `tests/` | Unit, contract, security, and integration suites |
| `quick-test/` | Self-contained notebook smoke test for `/v1/NAR2FHIR` and validation |

## Roadmap

| Milestone | Goal | Status |
|---|---|---|
| M0 | Platform, auth, storage, health, containers | Implemented |
| M1 | Validation cascade, terminology, plausibility | Implemented |
| M2 | BYOK gateway, synchronous NAR2FHIR/VOICE2FHIR conversion, qualification gates | Implemented |
| M3 | Documents, facts, staged generation, fidelity, coverage, normalize | Planned |
| M4 | Human review workflow | Planned |
| M5 | Goldset-based model qualification and calibrated routing | Planned |
| M6 | Initial generic FHIR target implemented; vendor and GCC adapters planned | In progress |

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Keep changes
small and verification-first:

1. add or update tests;
2. run the commands in [CONTRIBUTING.md](CONTRIBUTING.md) and the CI quality checks;
3. update OpenAPI snapshots when the public contract changes;
4. never log resource bodies, issue messages, prompts, or credentials; and
5. never turn an unavailable verifier into a successful response.

Community participation follows the [Code of Conduct](CODE_OF_CONDUCT.md).
Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party standards,
terminology, and asset notices are documented in [NOTICE](NOTICE).
