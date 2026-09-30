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
from pydantic_core import PydanticCustomError

from dealsource.resolve.normalize import US_STATES, normalize_state

_STATE_CODES = set(US_STATES.values())
WEIGHT_KEYS = ("sector", "size", "geography", "ownership")
DEFAULT_WEIGHTS = {"sector": 0.40, "size": 0.20, "geography": 0.20, "ownership": 0.20}
OWNERSHIP_SIGNALS = ("founder_led", "family_owned", "pe_or_strategic_backed", "publicly_traded")


# Validation problems are reported as (field path, rule name). Rule names are fixed strings and
# messages never include values from the file, so a problem report can't leak thesis contents.


def _rule(name: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(name, message)


def _signals(v: list[str]) -> list[str]:
    if any(s not in OWNERSHIP_SIGNALS for s in v):
        raise _rule(
            "unknown_ownership_signal", f"valid ownership signals: {', '.join(OWNERSHIP_SIGNALS)}"
        )
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
                raise _rule("naics_prefix_format", "NAICS prefixes must be 2-6 digits")
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
                raise _rule("unknown_state", "states must be US state codes or names")
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
        if set(v) - set(WEIGHT_KEYS):
            raise _rule("unknown_weight", f"valid weights: {', '.join(WEIGHT_KEYS)}")
        if any(w < 0 for w in v.values()):
            raise _rule("negative_weight", "weights must not be negative")
        total = sum(v.values())
        if total <= 0:
            raise _rule("weights_sum_zero", "weights must add up to more than 0")
        return {k: v.get(k, 0.0) / total for k in WEIGHT_KEYS}

    def matches_naics(self, codes: list[str] | str | None) -> bool:
        if not codes:
            return False
        if isinstance(codes, str):
            codes = [c for c in codes.split(",") if c]
        return any(c.startswith(p) for c in codes for p in self.sectors.naics_prefixes)


_FIELD_NAMES = frozenset(
    name
    for model in (Thesis, Sectors, Size, Range, Geography, Ownership, Exclusions)
    for name in model.model_fields
)
_RULE_ALIASES = {"extra_forbidden": "unknown_field"}


def problems_from(exc: ValidationError) -> list[str]:
    """'field.path: rule_name' per problem. Path parts that aren't schema field names (dict keys
    from the file, unknown fields) show as <key>; list positions show as numbers."""
    out: list[str] = []
    for e in exc.errors():
        parts = [str(p) if isinstance(p, int) or p in _FIELD_NAMES else "<key>" for p in e["loc"]]
        line = f"{'.'.join(parts) or '(top level)'}: {_RULE_ALIASES.get(e['type'], e['type'])}"
        if line not in out:
            out.append(line)
    return out


class ThesisError(ValueError):
    """The message and ``problems`` name fields and rules only, never values from the file."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or [message]


def load_thesis(path: Path) -> tuple[Thesis, str]:
    """Return (thesis, sha256 of the file). Errors name the field and rule, never echo the file."""
    path = Path(path)
    if not path.exists():
        raise ThesisError(f"No thesis file at {path}", ["(file): not_found"])
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ThesisError(
            f"Thesis file is not valid YAML ({type(exc).__name__})", ["(file): invalid_yaml"]
        ) from exc
    try:
        thesis = Thesis.model_validate(data)
    except ValidationError as exc:
        problems = problems_from(exc)
        raise ThesisError(f"Invalid thesis: {'; '.join(problems)}", problems) from exc
    return thesis, hashlib.sha256(raw).hexdigest()
