# ADR 0008: Keep delivery outside the validation layers

- Status: Accepted
- Date: 2026-09-06

## Context

The public validation contract always reports eight layers. Delivery adds destination
context preflight, reference rebinding, target-specific write planning, and
submission checks. Calling these activities a ninth validation layer would change
the meaning and shape of the stable validation report even though they answer
destination-specific questions rather than whether a resource is valid FHIR.

## Decision

Delivery, context preflight, and write-plan compilation are not a ninth validation
layer. The eight-layer validation report remains stable and is returned unchanged as
the `validation` part of delivery responses.

Destination identity checks are reported in the write plan's separate `preflight`
report. Rebinding, target compatibility, exclusions, dependencies, and write
operations are reported in the separate write-plan report. Submission produces a
separate delivery receipt and append-only ledger record.

## Consequences

Clients can continue to interpret the public eight-layer report without a schema or
semantic migration. Delivery decisions remain inspectable without conflating FHIR
validity with target availability, chart identity, adapter policy, or write
eligibility. A resource may pass validation and still be blocked by preflight or
write planning.
