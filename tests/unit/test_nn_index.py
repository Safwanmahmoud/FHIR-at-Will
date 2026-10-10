"""Shared nearest-neighbor index cache selection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fhirbridge.terminology.nn_index import _cache_dir, _ensure_hf_home


def test_cache_dir_uses_a_writable_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FHIRATWILL_BINDER_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(
        "fhirbridge.terminology.nn_index.tempfile.gettempdir",
        lambda: str(tmp_path),
    )

    chosen = _cache_dir()

    assert chosen == tmp_path / "fhiratwill" / "terminology_binder"
    assert chosen.is_dir()


def test_cache_dir_prefers_the_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv("FHIRATWILL_BINDER_CACHE", str(override))

    assert _cache_dir() == override


def test_hf_home_falls_back_to_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "fhirbridge.terminology.nn_index.tempfile.gettempdir",
        lambda: str(tmp_path),
    )

    _ensure_hf_home()

    root = tmp_path / "huggingface"
    assert Path(os.environ["HF_HOME"]) == root
    assert Path(os.environ["HF_HUB_CACHE"]) == root / "hub"
