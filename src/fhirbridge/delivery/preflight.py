"""Service adapter for :mod:`fhiratwill` wrong-patient preflight checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from fhiratwill import (
    PreflightReport,
    SubjectContext,
)
from fhiratwill import (
    run_preflight as run_core_preflight,
)
from fhiratwill.errors import TargetUnavailableError as CoreTargetUnavailableError

from fhirbridge.delivery.invocation import TargetInvocation
from fhirbridge.domain.errors import DomainError, TargetUnavailableError


class ResourceReader(Protocol):
    async def read(self, invocation: TargetInvocation, reference: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class _BoundReader:
    reader: ResourceReader
    invocation: TargetInvocation

    async def read(self, reference: str) -> dict[str, Any]:
        return await self.reader.read(self.invocation, reference)


async def run_preflight(
    *,
    bundle: dict[str, Any],
    context: SubjectContext,
    invocation: TargetInvocation,
    reader: ResourceReader,
) -> PreflightReport:
    try:
        return await run_core_preflight(
            bundle=bundle,
            context=context,
            reader=_BoundReader(reader, invocation),
        )
    except DomainError:
        raise
    except CoreTargetUnavailableError as exc:
        raise TargetUnavailableError(
            "The target could not be read for identity preflight.",
            safe_context={"target_id": invocation.target_id},
        ) from exc


__all__ = ["ResourceReader", "run_preflight"]
