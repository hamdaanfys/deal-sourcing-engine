"""Pydantic models shared across stages."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RawCompanyRecord(BaseModel):
    """One company as a source describes it, before entity resolution.

    Deliberately has no fields for people or contact details.
    """

    source: str
    source_record_id: str
    name: str
    website: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    naics: str | None = None
    employees: int | None = None
    revenue_usd_m: float | None = None
    description: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class GeoSpec(BaseModel):
    """A CBP geography: ``us``, ``state:13,37``, ``state:*`` or ``county:*/state:13``."""

    level: str  # "us" | "state" | "county"
    codes: tuple[str, ...] = ("*",)
    within_state: str | None = None  # counties only

    @classmethod
    def parse(cls, spec: str) -> GeoSpec:
        spec = spec.strip()
        if spec == "us":
            return cls(level="us", codes=("1",))
        head, _, parent = spec.partition("/")
        level, sep, codes = head.partition(":")
        if not sep or level not in {"state", "county"}:
            raise ValueError(
                f"Unrecognised geography {spec!r}; use us, state:13,37 or county:*/state:13"
            )
        code_list = tuple(c.strip() for c in codes.split(",") if c.strip())
        within = None
        if level == "county":
            p_level, _, p_code = parent.partition(":")
            if p_level != "state" or not p_code:
                raise ValueError(
                    f"County geography needs a parent state, e.g. county:*/state:13 (got {spec!r})"
                )
            within = p_code
        elif parent:
            raise ValueError(f"Only county geographies take a parent (got {spec!r})")
        return cls(level=level, codes=code_list or ("*",), within_state=within)


class MarketStat(BaseModel):
    """Aggregate CBP figures for one NAICS code in one geography and year."""

    source: str = "census_cbp"
    year: int
    naics: str
    naics_label: str | None = None
    geo_level: str
    geo_code: str
    geo_name: str | None = None
    establishments: int | None = None
    employees: int | None = None
    employees_noise_flag: str | None = None
    annual_payroll_usd_k: int | None = None
    # EMPSZES code -> {"label": ..., "establishments": ...}
    size_classes: dict[str, dict[str, Any]] = Field(default_factory=dict)
