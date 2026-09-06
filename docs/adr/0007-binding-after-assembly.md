# ADR 0007: Bind terminology after deterministic assembly

- Status: Accepted
- Date: 2026-09-06

## Context

Narrative conversion must preserve the boundary between model-derived meaning and
verified clinical coding. Asking the model to emit a terminology system and code
would turn a plausible completion into an unverified clinical assertion. Deterministic
assembly can construct the correct FHIR shape, but it cannot establish that a term is
a valid member of a terminology or value set.

## Decision

Terminology binding is a separate, networked stage after deterministic assembly.
The model emits text and values, never clinical codes. The binding stage proposes
candidates from a reviewed, versioned concept table or an exact terminology
expansion, then applies a code only after the configured terminology service verifies
it.

Binding is limited to a single verified candidate. If no candidate can be verified,
or more than one exact candidate remains, binding refuses to code the element and
preserves its original text. The response records PHI-free binding evidence and the
version of the binding table.

## Consequences

Conversion now depends on terminology availability and fails closed when a required
terminology operation is unavailable. Common vital-sign concepts and UCUM units can
be coded reproducibly without granting the model authority to invent codes.
Unsupported, ambiguous, and unverifiable concepts remain useful as text while making
the absence of verified coding explicit.
