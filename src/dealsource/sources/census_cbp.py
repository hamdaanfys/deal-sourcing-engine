"""Census County Business Patterns: aggregate market sizing, report-only in v1.

CBP gives establishment, employment and payroll counts by NAICS code and geography. It is not
a list of companies, so it writes to ``market_stats`` and never to ``raw_records``, and it
does not affect scores.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import httpx

from dealsource.db import utcnow
from dealsource.httpcache import CachedHttp
from dealsource.models import GeoSpec, MarketStat
from dealsource.sources.base import register_market_source

BASE_URL = "https://api.census.gov/data/{year}/cbp"

# NAICS variable per CBP vintage (checked against the API's variables.json for 2017-2023).
NAICS_FIELD_BY_YEAR = {year: "NAICS2017" for year in range(2017, 2024)}

ALL_ESTABLISHMENTS = "001"  # EMPSZES / LFO code for "all"


class CensusError(RuntimeError):
    pass


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def build_params(naics_field: str, naics: str, geo: GeoSpec, api_key: str | None) -> dict[str, str]:
    params = {
        "get": f"NAME,{naics_field},{naics_field}_LABEL,EMPSZES,EMPSZES_LABEL,ESTAB,EMP,EMP_N,PAYANN",
        naics_field: naics,
    }
    if geo.level == "us":
        params["for"] = "us:1"
    elif geo.level == "state":
        params["for"] = "state:" + ",".join(geo.codes)
    else:
        params["for"] = "county:" + ",".join(geo.codes)
        params["in"] = f"state:{geo.within_state}"
    if api_key:
        params["key"] = api_key
    return params


def parse_rows(
    rows: list[list[str]], *, year: int, naics_field: str, geo_level: str
) -> list[MarketStat]:
    """Turn a CBP response (header row + data rows) into one MarketStat per NAICS x geography.

    Rows for individual employment-size classes are folded into ``size_classes``; the
    all-establishments row supplies the totals.
    """
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(header)}
    for required in (naics_field, "ESTAB", "EMP", "PAYANN"):
        if required not in idx:
            raise CensusError(f"CBP response is missing column {required!r}")

    def col(row: list[str], name: str) -> str | None:
        i = idx.get(name)
        return row[i] if i is not None else None

    def geo_code(row: list[str]) -> str:
        if geo_level == "us":
            return "1"
        if geo_level == "state":
            return col(row, "state") or ""
        return f"{col(row, 'state')}{col(row, 'county')}"

    stats: dict[tuple[str, str], MarketStat] = {}
    for row in data:
        lfo = col(row, "LFO")
        if lfo not in (None, ALL_ESTABLISHMENTS):
            continue
        key = (col(row, naics_field) or "", geo_code(row))
        stat = stats.setdefault(
            key,
            MarketStat(
                year=year,
                naics=key[0],
                naics_label=col(row, f"{naics_field}_LABEL"),
                geo_level=geo_level,
                geo_code=key[1],
                geo_name=col(row, "NAME"),
            ),
        )
        size_code = col(row, "EMPSZES") or ALL_ESTABLISHMENTS
        if size_code == ALL_ESTABLISHMENTS:
            stat.establishments = _to_int(col(row, "ESTAB"))
            stat.employees = _to_int(col(row, "EMP"))
            stat.employees_noise_flag = col(row, "EMP_N")
            stat.annual_payroll_usd_k = _to_int(col(row, "PAYANN"))
        else:
            stat.size_classes[size_code] = {
                "label": col(row, "EMPSZES_LABEL"),
                "establishments": _to_int(col(row, "ESTAB")),
            }
    return list(stats.values())


@register_market_source
class CensusCBPSource:
    name = "census_cbp"

    def __init__(self, http: CachedHttp, api_key: str | None = None, refresh: bool = False):
        self.http = http
        self.api_key = api_key
        self.refresh = refresh
        self.requests_made = 0
        self.cache_hits = 0

    def fetch(self, naics: list[str], geos: list[GeoSpec], year: int) -> Iterator[MarketStat]:
        naics_field = NAICS_FIELD_BY_YEAR.get(year)
        if naics_field is None:
            supported = f"{min(NAICS_FIELD_BY_YEAR)}-{max(NAICS_FIELD_BY_YEAR)}"
            raise CensusError(f"CBP year {year} is not supported (supported: {supported})")
        url = BASE_URL.format(year=year)
        for code in naics:
            for geo in geos:
                params = build_params(naics_field, code, geo, self.api_key)
                try:
                    resp = self.http.get(url, params, refresh=self.refresh)
                except httpx.HTTPError as exc:
                    raise CensusError(f"Census API request failed: {exc}") from exc
                if resp.from_cache:
                    self.cache_hits += 1
                else:
                    self.requests_made += 1
                if resp.status in (301, 302) and "missing_key" in resp.headers.get("location", ""):
                    raise CensusError("Census API requires a key: set CENSUS_API_KEY in .env")
                if resp.status == 204:  # no data for this NAICS x geography
                    continue
                if resp.status != 200:
                    raise CensusError(f"Census API returned HTTP {resp.status} for NAICS {code}")
                yield from parse_rows(
                    resp.json(), year=year, naics_field=naics_field, geo_level=geo.level
                )


def store_stats(conn: sqlite3.Connection, stats: list[MarketStat]) -> int:
    now = utcnow()
    with conn:
        for s in stats:
            conn.execute(
                """INSERT INTO market_stats (source, year, naics, naics_label, geo_level, geo_code, geo_name,
                     establishments, employees, employees_noise_flag, annual_payroll_usd_k,
                     size_classes_json, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, year, naics, geo_level, geo_code) DO UPDATE SET
                     naics_label=excluded.naics_label, geo_name=excluded.geo_name,
                     establishments=excluded.establishments, employees=excluded.employees,
                     employees_noise_flag=excluded.employees_noise_flag,
                     annual_payroll_usd_k=excluded.annual_payroll_usd_k,
                     size_classes_json=excluded.size_classes_json, fetched_at=excluded.fetched_at""",
                (
                    s.source,
                    s.year,
                    s.naics,
                    s.naics_label,
                    s.geo_level,
                    s.geo_code,
                    s.geo_name,
                    s.establishments,
                    s.employees,
                    s.employees_noise_flag,
                    s.annual_payroll_usd_k,
                    json.dumps(s.size_classes, sort_keys=True),
                    now,
                ),
            )
    return len(stats)
