"""Investment thesis YAML (DESIGN.md §9.1): validated with clear errors, hashed for traceability.

Discovery and the labeling export use the sector (NAICS) and geography parts; scoring uses all
of it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dealsource.resolve.normalize import US_STATES, normalize_state

_STATE_CODES = set(US_STATES.values())
WEIGHT_KEYS = ("sector", "size", "geography", "ownership")
DEFAULT_WEIGHTS = {"sector": 0.40, "size": 0.20, "geography": 0.20, "ownership": 0.20}
OWNERSHIP_SIGNALS = ("founder_led", "family_owned", "pe_or_strategic_backed", "publicly_traded")


def _signals(v: list[str]) -> list[str]:
    unknown = [s for s in v if s not in OWNERSHIP_SIGNALS]
    if unknown:
        raise ValueError(f"unknown ownership signal(s) {unknown}; valid: {list(OWNERSHIP_SIGNALS)}")
    return v


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

    _check_prefer = field_validator("prefer")(_signals)


class Exclusions(_Strict):
    keywords: list[str] = Field(default_factory=list)
    ownership: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    _check_ownership = field_validator("ownership")(_signals)


class Thesis(_Strict):
    name: str
    sectors: Sectors
    geography: Geography
    size: Size = Field(default_factory=Size)
    ownership: Ownership = Field(default_factory=Ownership)
    exclusions: Exclusions = Field(default_factory=Exclusions)
    weights: dict[str, float] = Field(default_factory=dict)
    shortlist_threshold: float = Field(60, ge=0, le=100)

    @field_validator("weights")
    @classmethod
    def _weights(cls, v: dict[str, float]) -> dict[str, float]:
        """Missing weights mean the defaults; given weights are normalized to sum to 1."""
        if not v:
            return dict(DEFAULT_WEIGHTS)
        unknown = sorted(set(v) - set(WEIGHT_KEYS))
        if unknown:
            raise ValueError(f"unknown weight(s) {unknown}; valid: {list(WEIGHT_KEYS)}")
        if any(w < 0 for w in v.values()):
            raise ValueError("weights must not be negative")
        total = sum(v.values())
        if total <= 0:
            raise ValueError("weights must add up to more than 0")
        return {k: v.get(k, 0.0) / total for k in WEIGHT_KEYS}

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
