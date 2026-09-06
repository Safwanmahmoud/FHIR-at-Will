"""Destination capability descriptor protocol."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable


class FailurePolicy(StrEnum):
    ATOMIC_TRANSACTION = "atomic_transaction"
    STOP_ON_FIRST_ERROR = "stop_on_first_error"


@runtime_checkable
class TargetDescriptor(Protocol):
    @property
    def target_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def supports_transaction(self) -> bool: ...

    @property
    def failure_policy(self) -> FailurePolicy: ...

    @property
    def accepted_resource_types(self) -> frozenset[str] | None: ...

    @property
    def forbidden_elements(self) -> Mapping[str, frozenset[str]]: ...

    def accepts(self, resource_type: str) -> bool: ...


__all__ = ["FailurePolicy", "TargetDescriptor"]
