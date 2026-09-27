"""Source protocols and registries.

Company sources yield records about individual companies; market-data sources yield aggregate
statistics (e.g. Census CBP) and never create companies. Adding a source means one new module
that registers itself here; downstream stages don't change.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from dealsource.models import GeoSpec, MarketStat, RawCompanyRecord


class CompanySource(Protocol):
    name: str

    def iter_records(self) -> Iterator[RawCompanyRecord]: ...


class MarketDataSource(Protocol):
    name: str

    def fetch(self, naics: list[str], geos: list[GeoSpec], year: int) -> Iterator[MarketStat]: ...


COMPANY_SOURCES: dict[str, type] = {}
MARKET_SOURCES: dict[str, type] = {}


def register_company_source(cls: type) -> type:
    COMPANY_SOURCES[cls.name] = cls
    return cls


def register_market_source(cls: type) -> type:
    MARKET_SOURCES[cls.name] = cls
    return cls
