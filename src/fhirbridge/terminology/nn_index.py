"""Process-wide nearest-neighbor terminology index (ICD-10-CM by default)."""

from __future__ import annotations

import logging
import threading

from fhiratwill.terminology_binder import TerminologyIndex

from fhirbridge.domain.errors import TerminologyUnavailableError

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INDEX: TerminologyIndex | None = None


def get_index() -> TerminologyIndex:
    """Return the shared index, building ICD-10-CM embeddings on first use."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _LOCK:
        if _INDEX is None:
            logger.info("terminology_index_building")
            try:
                _INDEX = TerminologyIndex.load(include_default=True, show_progress=False)
            except Exception as exc:
                logger.exception("terminology_index_failed")
                raise TerminologyUnavailableError(
                    "The nearest-neighbor terminology index could not be loaded.",
                    safe_context={"reason": type(exc).__name__},
                ) from exc
            logger.info(
                "terminology_index_ready",
                extra={"dictionaries": ",".join(item.name for item in _INDEX.dictionaries)},
            )
    return _INDEX


def set_index(index: TerminologyIndex | None) -> None:
    """Replace the shared index. Tests inject a tiny in-memory catalog."""
    global _INDEX
    with _LOCK:
        _INDEX = index


__all__ = ["get_index", "set_index"]
