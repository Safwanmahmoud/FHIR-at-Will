"""Load the reviewed deterministic terminology concept table."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

from fhirbridge.version import BINDING_TABLE_VERSION

_RULES_PATH: Final = Path(__file__).parent / "rules" / "concepts.yaml"


@dataclass(frozen=True, slots=True)
class ConceptRule:
    path: str
    system: str
    code: str
    display: str
    value_set: str
    aliases: frozenset[str]


@dataclass(frozen=True, slots=True)
class UnitRule:
    code: str
    display: str
    aliases: frozenset[str]


@dataclass(frozen=True, slots=True)
class ConceptPack:
    version: str
    concepts: tuple[ConceptRule, ...]
    units: tuple[UnitRule, ...]


def normalize_designation(value: str) -> str:
    return " ".join(value.casefold().strip().split())


@lru_cache(maxsize=1)
def load_concepts(path: str | None = None) -> ConceptPack:
    source = Path(path) if path else _RULES_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    version = str(raw.get("version", ""))
    if version != BINDING_TABLE_VERSION:
        raise ValueError(
            f"concept table version {version!r} does not match build "
            f"version {BINDING_TABLE_VERSION!r}"
        )
    concepts = tuple(
        ConceptRule(
            path=str(item["path"]),
            system=str(item["system"]),
            code=str(item["code"]),
            display=str(item["display"]),
            value_set=str(item["value_set"]),
            aliases=frozenset(normalize_designation(str(v)) for v in item["aliases"]),
        )
        for item in raw.get("concepts", [])
    )
    units = tuple(
        UnitRule(
            code=str(item["code"]),
            display=str(item["display"]),
            aliases=frozenset(normalize_designation(str(v)) for v in item["aliases"]),
        )
        for item in raw.get("units", [])
    )
    return ConceptPack(version=version, concepts=concepts, units=units)


__all__ = [
    "ConceptPack",
    "ConceptRule",
    "UnitRule",
    "load_concepts",
    "normalize_designation",
]
