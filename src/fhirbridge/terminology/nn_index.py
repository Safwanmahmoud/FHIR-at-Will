"""Process-wide nearest-neighbor terminology index (ICD-10-CM by default)."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from fhiratwill.terminology_binder import TerminologyIndex

from fhirbridge.domain.errors import TerminologyUnavailableError

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INDEX: TerminologyIndex | None = None


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _cache_dir() -> Path:
    """Prefer a writable cache. The API image's HOME is not writable."""
    candidates: list[Path] = []
    if override := os.environ.get("FHIRATWILL_BINDER_CACHE"):
        candidates.append(Path(override))
    if xdg := os.environ.get("XDG_CACHE_HOME"):
        candidates.append(Path(xdg) / "fhiratwill" / "terminology_binder")
    candidates.append(Path(tempfile.gettempdir()) / "fhiratwill" / "terminology_binder")
    for path in candidates:
        if _writable_dir(path):
            return path
    raise TerminologyUnavailableError(
        "No writable cache directory is available for terminology embeddings.",
        safe_context={"tried": ",".join(str(item) for item in candidates)},
    )


def _ensure_hf_home() -> None:
    """Sentence-transformers writes the SapBERT weights under HF_HOME."""
    current = os.environ.get("HF_HOME")
    if current and _writable_dir(Path(current)):
        return
    fallback = Path(tempfile.gettempdir()) / "huggingface"
    if _writable_dir(fallback):
        os.environ["HF_HOME"] = str(fallback)


def get_index() -> TerminologyIndex:
    """Return the shared index, building ICD-10-CM embeddings on first use."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _LOCK:
        if _INDEX is None:
            logger.info("terminology_index_building")
            try:
                _ensure_hf_home()
                _INDEX = TerminologyIndex.load(
                    include_default=True,
                    show_progress=False,
                    cache_dir=_cache_dir(),
                )
            except TerminologyUnavailableError:
                raise
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
