"""USAspending.gov federal contract recipients as a company source (no API key).

Uses /api/v2/search/spending_by_category/recipient/: one row per recipient (with UEI and total
obligated amount), queried once per thesis state x NAICS prefix over the last N fiscal years.
Each recipient's `naics` is the list of prefixes it won contracts under (award-based, not the
company's registered NAICS). Responses are cached; requests are spaced.
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
NAICS_REFERENCE_URL = "https://api.usaspending.gov/api/v2/references/naics/{code}/"
SUPPORTED_NAICS_LENGTHS = (2, 4, 6)  # the API rejects 3- and 5-digit codes
CONTRACT_AWARD_TYPES = [
    "A",
    "B",
    "C",
    "D",
]  # definitive contracts, purchase and delivery orders, BPA calls
PAGE_SIZE = 100
MAX_PAGES_PER_QUERY = 50
MIN_DELAY_SECONDS = 1.0
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 10.0


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
    unknown_prefixes: list[str] = field(default_factory=list)
    retries: int = 0


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
        self._seen: set[str] = set()
        self._expanded: dict[str, list[str]] = {}

    def _post(self, body: dict) -> dict:
        """POST with caching, spacing, and retries: timeouts, connection errors and 5xx are
        retried up to MAX_ATTEMPTS times with growing waits; a rerun resumes from the cache."""
        key = self.http.post_key(URL, body)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.http.lookup(key) is None:
                self._wait_turn()
            try:
                resp = self.http.post_json(URL, body)
            except httpx.HTTPError as exc:
                self._last_request = self.clock.monotonic()
                if attempt == MAX_ATTEMPTS:
                    raise UsaSpendingError(
                        f"USAspending request failed after {MAX_ATTEMPTS} attempts ({type(exc).__name__}); "
                        "rerun to resume from the cache"
                    ) from exc
                self.stats.retries += 1
                self.clock.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            if resp.from_cache:
                self.stats.cache_hits += 1
            else:
                self.stats.requests += 1
                self._last_request = self.clock.monotonic()
            if resp.status >= 500 and attempt < MAX_ATTEMPTS:
                self.stats.retries += 1
                self.clock.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            if resp.status != 200:
                raise UsaSpendingError(f"USAspending returned HTTP {resp.status}")
            return resp.json()
        raise UsaSpendingError("USAspending request failed")  # pragma: no cover

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + MIN_DELAY_SECONDS - self.clock.monotonic()
            if wait > 0:
                self.clock.sleep(wait)

    def expand_prefix(self, prefix: str) -> list[str]:
        """Codes the API accepts for a thesis prefix: itself if 2/4/6 digits, otherwise its exact
        children from USAspending's NAICS reference (e.g. 33992 -> 339920, ...)."""
        if len(prefix) in SUPPORTED_NAICS_LENGTHS:
            return [prefix]
        if prefix in self._expanded:
            return self._expanded[prefix]
        parent = prefix[:-1]
        url = NAICS_REFERENCE_URL.format(code=parent)
        if self.http.lookup(url) is None:
            self._wait_turn()
        try:
            resp = self.http.get(url)
        except httpx.HTTPError as exc:
            raise UsaSpendingError(
                f"USAspending NAICS lookup failed ({type(exc).__name__})"
            ) from exc
        if resp.from_cache:
            self.stats.cache_hits += 1
        else:
            self.stats.requests += 1
            self._last_request = self.clock.monotonic()
        if resp.status != 200:
            raise UsaSpendingError(f"USAspending NAICS lookup returned HTTP {resp.status}")
        children = [
            c["naics"]
            for r in resp.json().get("results", [])
            for c in r.get("children") or []
            if str(c.get("naics", "")).startswith(prefix)
        ]
        self._expanded[prefix] = sorted(set(children))
        if not children:
            self.stats.unknown_prefixes.append(prefix)
        return self._expanded[prefix]

    def _recipients(self, state: str, prefix: str) -> Iterator[dict]:
        """All recipient rows for contracts under one NAICS prefix in one state."""
        codes = self.expand_prefix(prefix)
        if not codes:
            return
        for page in range(1, MAX_PAGES_PER_QUERY + 1):
            body = {
                "filters": {
                    "award_type_codes": CONTRACT_AWARD_TYPES,
                    "naics_codes": {"require": codes},
                    "recipient_locations": [{"country": "USA", "state": state}],
                    "time_period": [{"start_date": self.start, "end_date": self.end}],
                },
                "category": "recipient",
                "limit": PAGE_SIZE,
                "page": page,
            }
            data = self._post(body)
            yield from data.get("results", [])
            if not data.get("page_metadata", {}).get("hasNext"):
                return
        self.stats.truncated_states.append(f"{state}/{prefix}")

    def iter_records(self) -> Iterator[RawCompanyRecord]:
        """One query per state x NAICS prefix, so each recipient is tagged with the prefixes it
        won contracts under ("award-based NAICS"; USAspending has no company NAICS)."""
        for state in self.states:
            found: dict[str, dict] = {}
            for prefix in self.naics:
                for row in self._recipients(state, prefix):
                    uei = (row.get("uei") or "").strip()
                    name = (row.get("name") or "").strip()
                    if not uei or not name:
                        self.stats.skipped_no_uei += 1
                        continue
                    entry = found.setdefault(uei, {"name": name, "amount": 0.0, "prefixes": []})
                    entry["amount"] += float(row.get("amount") or 0)
                    if prefix not in entry["prefixes"]:
                        entry["prefixes"].append(prefix)
            for uei, entry in found.items():
                if uei in self._seen:
                    continue  # already yielded for an earlier state
                self._seen.add(uei)
                self.stats.recipients += 1
                self.stats.per_state[state] = self.stats.per_state.get(state, 0) + 1
                yield RawCompanyRecord(
                    source=self.name,
                    source_record_id=uei,
                    name=entry["name"],
                    state=state,
                    country="US",
                    naics=",".join(entry["prefixes"]),
                    extra={
                        "uei": uei,
                        "naics_source": "usaspending_award_prefix",
                        "federal_contract_obligations_usd": round(entry["amount"], 2),
                        "window": f"{self.start}..{self.end}",
                    },
                )
