"""USAspending.gov federal contract recipients as a company source (no API key).

Uses /api/v2/search/spending_by_category/recipient/: one row per recipient (with UEI and total
obligated amount) for contracts whose NAICS matches the thesis, for recipients located in each
thesis state, over the last N fiscal years. Responses are cached; requests are spaced.
Recipients have no website here; they join SAM.gov records on UEI during resolution.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

import httpx

from dealsource.clock import Clock, SystemClock
from dealsource.httpcache import CachedHttp
from dealsource.models import RawCompanyRecord
from dealsource.sources.base import register_company_source

URL = "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/"
CONTRACT_AWARD_TYPES = [
    "A",
    "B",
    "C",
    "D",
]  # definitive contracts, purchase and delivery orders, BPA calls
PAGE_SIZE = 100
MAX_PAGES_PER_STATE = 50
MIN_DELAY_SECONDS = 1.0


class UsaSpendingError(RuntimeError):
    pass


def fiscal_year_window(today: date, years: int) -> tuple[str, str]:
    """The last `years` federal fiscal years including the current one (FY starts 1 October)."""
    current_fy = today.year + 1 if today.month >= 10 else today.year
    start = date(current_fy - years, 10, 1)
    return start.isoformat(), today.isoformat()


@dataclass
class UsaStats:
    requests: int = 0
    cache_hits: int = 0
    recipients: int = 0
    skipped_no_uei: int = 0
    per_state: dict[str, int] = field(default_factory=dict)
    truncated_states: list[str] = field(default_factory=list)


@register_company_source
class UsaSpendingSource:
    name = "usaspending"

    def __init__(
        self,
        http: CachedHttp,
        *,
        naics_prefixes: list[str],
        states: list[str],
        today: date,
        fiscal_years: int = 5,
        clock: Clock | None = None,
    ):
        self.http = http
        self.naics = list(naics_prefixes)
        self.states = list(states)
        self.start, self.end = fiscal_year_window(today, fiscal_years)
        self.clock = clock or SystemClock()
        self.stats = UsaStats()
        self._last_request: float | None = None

    def _post(self, body: dict) -> dict:
        cached = self.http.lookup(self.http.post_key(URL, body)) is not None
        if not cached and self._last_request is not None:
            wait = self._last_request + MIN_DELAY_SECONDS - self.clock.monotonic()
            if wait > 0:
                self.clock.sleep(wait)
        try:
            resp = self.http.post_json(URL, body)
        except httpx.HTTPError as exc:
            raise UsaSpendingError(f"USAspending request failed ({type(exc).__name__})") from exc
        if resp.from_cache:
            self.stats.cache_hits += 1
        else:
            self.stats.requests += 1
            self._last_request = self.clock.monotonic()
        if resp.status != 200:
            raise UsaSpendingError(f"USAspending returned HTTP {resp.status}")
        return resp.json()

    def iter_records(self) -> Iterator[RawCompanyRecord]:
        seen: set[str] = set()
        for state in self.states:
            for page in range(1, MAX_PAGES_PER_STATE + 1):
                body = {
                    "filters": {
                        "award_type_codes": CONTRACT_AWARD_TYPES,
                        "naics_codes": {"require": self.naics},
                        "recipient_locations": [{"country": "USA", "state": state}],
                        "time_period": [{"start_date": self.start, "end_date": self.end}],
                    },
                    "category": "recipient",
                    "limit": PAGE_SIZE,
                    "page": page,
                }
                data = self._post(body)
                for row in data.get("results", []):
                    uei = (row.get("uei") or "").strip()
                    name = (row.get("name") or "").strip()
                    if not uei or not name:
                        self.stats.skipped_no_uei += 1
                        continue
                    if uei in seen:
                        continue
                    seen.add(uei)
                    self.stats.recipients += 1
                    self.stats.per_state[state] = self.stats.per_state.get(state, 0) + 1
                    yield RawCompanyRecord(
                        source=self.name,
                        source_record_id=uei,
                        name=name,
                        state=state,
                        country="US",
                        extra={
                            "uei": uei,
                            "federal_contract_obligations_usd": row.get("amount"),
                            "window": f"{self.start}..{self.end}",
                        },
                    )
                if not data.get("page_metadata", {}).get("hasNext"):
                    break
            else:
                self.stats.truncated_states.append(state)
