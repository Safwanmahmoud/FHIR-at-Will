"""API service adapter for the :mod:`fhiratwill` validation cascade."""

from __future__ import annotations

from collections.abc import Mapping

from fhiratwill import (
    TerminologyClient,
    ValidationConfig,
    ValidationReport,
    ValidationSpec,
    ValidatorClient,
    validate,
)

from fhirbridge.config import Settings
from fhirbridge.observability.metrics import (
    VALIDATION_ISSUES,
    VALIDATION_LAYER_DURATION,
    VALIDATION_LAYER_SKIPPED,
    VALIDATION_RUNS,
)
from fhirbridge.version import (
    BINDING_TABLE_VERSION,
    CODE_VERSION,
    TYPED_MODEL_FHIR_VERSION,
    VALIDATION_REPORT_SCHEMA_VERSION,
)


class ValidationCascade:
    """Bind service configuration and metrics to the reusable core cascade."""

    def __init__(
        self,
        *,
        validator: ValidatorClient,
        terminology: TerminologyClient,
        settings: Settings,
        terminology_versions: Mapping[str, str | None] | None = None,
    ) -> None:
        self._validator = validator
        self._terminology = terminology
        self._config = ValidationConfig(
            fhir_version=settings.default_fhir_version,
            typed_model_fhir_version=TYPED_MODEL_FHIR_VERSION,
            code_version=CODE_VERSION,
            report_schema_version=VALIDATION_REPORT_SCHEMA_VERSION,
            binding_table_version=int(BINDING_TABLE_VERSION),
            validator_version=settings.validator_version,
            terminology_versions=dict(terminology_versions or {}),
        )

    async def run(
        self,
        payload: object,
        spec: ValidationSpec | None = None,
    ) -> ValidationReport:
        report = await validate(
            payload,
            validator=self._validator,
            terminology=self._terminology,
            config=self._config,
            spec=spec,
        )
        _observe(report)
        return report


def _observe(report: ValidationReport) -> None:
    VALIDATION_RUNS.labels(outcome=str(report.status)).inc()
    for layer in report.layers:
        VALIDATION_LAYER_DURATION.labels(layer=str(layer.layer)).observe(layer.duration_ms / 1000)
        if layer.skipped_reason:
            VALIDATION_LAYER_SKIPPED.labels(
                layer=str(layer.layer),
                reason="core_skipped",
            ).inc()
        for issue in layer.issues:
            VALIDATION_ISSUES.labels(
                layer=str(layer.layer),
                severity=str(issue.severity),
            ).inc()


__all__ = ["ValidationCascade", "ValidationSpec"]
