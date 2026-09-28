"""Investment thesis YAML (DESIGN.md §9.1): validated with clear errors, hashed for traceability.

Discovery and the labeling export use the sector (NAICS) and geography parts now; scoring uses
the rest later.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dealsource.resolve.normalize import US_STATES, normalize_state

_STATE_CODES = set(US_STATES.values())


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Range(_Strict):
    min: float | None = None
    max: float | None = None


class Sectors(_Strict):
    naics_prefixes: list[str] = Field(min_length=1)
    include_keywords: list[str] = Field(default_factory=list)
    end_markets: list[str] = Field(default_factory=list)

    @field_validator("naics_prefixes")
    @classmethod
    def _digits(cls, v: list[str]) -> list[str]:
        out = []
        for code in v:
            code = str(code).strip()
            if not re.fullmatch(r"\d{2,6}", code):
                raise ValueError(f"NAICS prefix {code!r} must be 2-6 digits")
            out.append(code)
        return out


class Size(_Strict):
    employees: Range | None = None
    facilities: Range | None = None
    revenue_usd_m: Range | None = None


class Geography(_Strict):
    countries: list[str] = Field(default_factory=lambda: ["US"])
    states: list[str] = Field(min_length=1)

    @field_validator("states")
    @classmethod
    def _states(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            code = normalize_state(str(s))
            if code not in _STATE_CODES:
                raise ValueError(f"unknown US state {s!r}")
            out.append(code)
        return sorted(set(out))


class Ownership(_Strict):
    prefer: list[str] = Field(default_factory=list)


class Exclusions(_Strict):
    keywords: list[str] = Field(default_factory=list)
    ownership: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class Thesis(_Strict):
    name: str
    sectors: Sectors
    geography: Geography
    size: Size = Field(default_factory=Size)
    ownership: Ownership = Field(default_factory=Ownership)
    exclusions: Exclusions = Field(default_factory=Exclusions)
    weights: dict[str, float] = Field(default_factory=dict)
    shortlist_threshold: float = 60

    def matches_naics(self, codes: list[str] | str | None) -> bool:
        if not codes:
            return False
        if isinstance(codes, str):
            codes = [c for c in codes.split(",") if c]
        return any(c.startswith(p) for c in codes for p in self.sectors.naics_prefixes)


class ThesisError(ValueError):
    pass


def load_thesis(path: Path) -> tuple[Thesis, str]:
    """Return (thesis, sha256 of the file). Errors name the field, never echo the file."""
    path = Path(path)
    if not path.exists():
        raise ThesisError(f"No thesis file at {path}")
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ThesisError(f"Thesis file is not valid YAML ({type(exc).__name__})") from exc
    try:
        thesis = Thesis.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ThesisError(f"Invalid thesis: {problems}") from exc
    return thesis, hashlib.sha256(raw).hexdigest()
