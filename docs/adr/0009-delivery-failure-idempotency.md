# ADR 0009: Define delivery failure and idempotency semantics

- Status: Accepted
- Date: 2026-09-06

## Context

Clinical writes must not be repeated accidentally, and a partial multi-resource
delivery must never be described as rolled back unless the destination guarantees
that behavior. The current generic FHIR target supports a transaction Bundle, while
future vendor and EHR adapters may expose only sequential write APIs.

## Decision

The generic FHIR target submits one FHIR transaction Bundle. Its multi-resource write
is atomic according to FHIR transaction semantics: the target accepts all transaction
entries or rejects the transaction.

Future sequential EHR adapters must execute their dependency-ordered steps until the
first failure and then stop. They must report which steps were attempted and must not
claim that earlier successful writes were rolled back unless the target provides and
the adapter invokes a documented atomic or compensating operation.

`POST /v1/deliver` requires a request `Idempotency-Key`. Reusing the same key with a
different request is a conflict. Every planned resource also receives a deterministic
identifier derived from the tenant, conversion, entry, and target. Generic FHIR
transaction entries use that identifier in conditional creates so request retries
and resource writes have separate idempotency defenses.

Every submitted delivery records an append-only, tenant-scoped, PHI-free ledger
entry. The ledger stores identifiers, target, status, response status, resource
count, duration, and timestamps, but no resource body, target response body, token,
or clinical value.

## Consequences

Clients can safely retry a request with its original idempotency key and can detect
conflicting reuse. The current target has atomic failure behavior, but future
sequential adapters will expose partial-success risk honestly. Delivery evidence is
durable and auditable without turning the ledger into a second store of clinical
content or credentials.
