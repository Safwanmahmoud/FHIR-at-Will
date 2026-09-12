# Changelog

All notable changes to FHIR at Will will be documented here. The project uses
semantic versioning after its first public release.

## Unreleased

- Restore the reviewed extraction rule pack on top of `fhiratwill==0.2.0`
  (`PROMPT_SET_VERSION` `v5.4.0`). The core split in `fed4303` dropped the pack
  from the shipped prompt; conversion now composes it back on before the catalog
  so denials become `verificationStatus: refuted` and a family member's history
  is suppressed rather than recorded as a patient `Condition`.
- Replace the duplicated framework-neutral core with the published
  `fhiratwill==0.2.0` package. The API now keeps only HTTP, policy, observability,
  persistence, sidecar transport, and delivery-submission integration code.
- Prepare the repository for public contribution and coordinated disclosure.
- Add grounded two-stage `/v1/NAR2FHIR` conversion.
- Consolidate narrative-to-FHIR conversion on `/v1/NAR2FHIR`.
- Return NAR2FHIR Bundles unvalidated; callers use `/v1/validate` separately.
- Assemble NAR2FHIR Bundles deterministically instead of with a second model
  call, so identical entities always produce an identical Bundle and no coded
  concept, unit system, or date is ever synthesized. **Breaking:** extraction
  entities now require an `instance` grouping key (`PROMPT_SET_VERSION` `v5.0.0`),
  and `ConvertRequest.profiles` is removed because deterministic assembly
  validates nothing and so cannot honor a profile request.
- Report every element NAR2FHIR dropped, inferred, wired, or found in conflict in
  a new PHI-free `assembly` list on the response.
- Add a reviewed extraction rule pack (`fhirbridge.llm.extraction_rules`) rendered
  into the extraction prompt and pinned by `prompt_set_fingerprint()`
  (`PROMPT_SET_VERSION` `v5.1.0`). The initial six rules route a stated age to an
  `Age` Observation rather than fabricating `Patient.birthDate`, split compound
  readings such as `128/82 mmHg` into one Observation each, resolve relative dates
  only against an anchor the narrative states, record a denied condition as
  `verificationStatus: refuted` while suppressing a family member's history
  entirely, split medication phrases into drug and dosing, and tighten `instance`
  grouping.
- Add the `machine-inferred` provenance tag for resources carrying a
  FHIR-required value that the source did not state.
- Add `POST /v1/VOICE2FHIR`: transcribe dictated clinical audio, then run it
  through the same grounded extraction and deterministic assembly as
  `/v1/NAR2FHIR`. Dictation is a separate BYOK call carrying `X-STT-*`
  credentials (Gemini by default; litellm cannot transcribe through OpenRouter),
  routed through the same provider, egress-allowlist, and PHI-acknowledgement
  gates but not the qualification tier, which ranks reasoning models rather than
  transcribers. Audio is uploaded as multipart and never logged; the transcript
  is returned in the response body so a reviewer can catch a mishearing before
  trusting the Bundle. The transport guard now also refuses `X-STT-API-Key` over
  plaintext HTTP, and the verbatim dictation prompt is pinned
  (`PROMPT_SET_VERSION` `v5.2.0`).
- Add authenticated terminology search.

## 0.1.0 - 2026-08-28

Initial alpha:

- FHIR R4 validation cascade with structural, profile, terminology, invariant,
  plausibility, and routing layers.
- FHIR validator and terminology sidecar integration with fail-closed errors.
- BYOK LLM gateway, qualification, egress, and cost controls.
- API-key authentication, PostgreSQL tenant isolation, append-only audit data,
  health endpoints, metrics, tracing, and container builds.

This alpha is not a medical device and is not suitable for unsupervised
clinical use.
