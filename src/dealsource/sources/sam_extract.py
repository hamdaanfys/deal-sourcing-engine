"""SAM.gov public monthly entity extract (bulk file) as a company source.

One API request downloads the whole public monthly file (a ZIP of a pipe-delimited .dat), which
keeps us well inside the 10 requests/day allowed without a SAM.gov role. The file is kept under
the data dir and parsed locally, streaming, with the thesis NAICS/state filters applied.

Only these public columns are read (1-based, from "SAM Master Extract Mapping v6.0 Public File
V2 Layout"). Point-of-contact columns (47-112: names, titles, addresses) are never read.
"""

from __future__ import annotations

import io
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx

from dealsource.db import utcnow
from dealsource.models import RawCompanyRecord
from dealsource.sources.base import register_company_source

EXTRACTS_URL = "https://api.sam.gov/data-services/v1/extracts"
API_NAME = "sam.gov"
DEFAULT_DAILY_BUDGET = 8  # SAM.gov allows 10/day for non-federal users without a role

# 1-based column positions in the public V2 layout
COL = {
    "uei": 1,
    "extract_code": 6,  # A = active, E = expired
    "purpose": 7,
    "expiration_date": 9,
    "last_update_date": 10,
    "legal_name": 12,
    "dba_name": 13,
    "city": 18,
    "state": 19,
    "zip": 20,
    "country": 22,
    "dnb_open_data": 24,
    "entity_url": 27,
    "primary_naics": 33,
    "naics_string": 35,
    "exclusion_flag": 116,
    "no_public_display": 119,
}
N_COLUMNS = 142
END_OF_RECORD = "!end"
DNB_CUTOFF = "20220404"  # D&B terms cover records last updated before 4 April 2022


class SamError(RuntimeError):
    pass


def first_sunday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def latest_month(today: date) -> tuple[int, int]:
    """The newest monthly file that should exist: generated on the first Sunday of the month."""
    if today > first_sunday(today.year, today.month):
        return today.year, today.month
    prev = today.replace(day=1) - timedelta(days=1)
    return prev.year, prev.month


def parse_naics_string(value: str) -> list[tuple[str, str]]:
    """'333611Y~333612N' -> [('333611', 'Y'), ('333612', 'N')]  (flag: SBA small-business Y/N/E)."""
    out = []
    for part in (value or "").split("~"):
        m = re.match(r"(\d{6})(.?)", part.strip())
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def iter_rows(lines: Iterator[str]) -> Iterator[list[str]]:
    """Yield records as column lists; skips the BOF/EOF lines and joins records split by
    stray newlines (each record ends with '!end')."""
    buffer = ""
    for line in lines:
        line = line.rstrip("\r\n")
        if not buffer and (line.startswith("BOF ") or line.startswith("EOF ")):
            continue
        buffer = f"{buffer} {line}" if buffer else line
        if buffer.endswith(END_OF_RECORD):
            cols = buffer.split("|")
            buffer = ""
            if len(cols) >= COL["no_public_display"]:
                yield cols


@dataclass
class SamFilterStats:
    scanned: int = 0
    inactive: int = 0
    not_public: int = 0
    excluded_entities: int = 0
    wrong_country_or_state: int = 0
    wrong_naics: int = 0
    dnb_era: int = 0
    kept: int = 0
    with_website: int = 0
    per_state: dict[str, int] = field(default_factory=dict)


@register_company_source
class SamExtractSource:
    name = "sam"

    def __init__(self, zip_path: Path, *, naics_prefixes: list[str], states: list[str]):
        self.zip_path = Path(zip_path)
        self.naics_prefixes = tuple(naics_prefixes)
        self.states = set(states)
        self.stats = SamFilterStats()

    def _open_lines(self) -> Iterator[str]:
        try:
            zf = zipfile.ZipFile(self.zip_path)
        except zipfile.BadZipFile as exc:
            raise SamError(f"{self.zip_path.name} is not a valid ZIP file") from exc
        with zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".dat")]
            if not members:
                raise SamError(f"{self.zip_path.name} contains no .dat extract file")
            with zf.open(members[0]) as raw:
                yield from io.TextIOWrapper(raw, encoding="utf-8", errors="replace")

    def iter_records(self) -> Iterator[RawCompanyRecord]:
        st = self.stats
        for cols in iter_rows(self._open_lines()):
            st.scanned += 1

            def col(name: str, cols: list[str] = cols) -> str:
                i = COL[name] - 1
                return cols[i].strip() if i < len(cols) else ""

            if col("extract_code") != "A":
                st.inactive += 1
                continue
            if col("no_public_display").upper().startswith("NPDY"):
                st.not_public += 1  # the registrant did not authorize public display
                continue
            if col("exclusion_flag") == "D":
                st.excluded_entities += 1  # debarred/excluded from federal awards
                continue
            if col("country") != "USA" or col("state") not in self.states:
                st.wrong_country_or_state += 1
                continue
            naics = parse_naics_string(col("naics_string"))
            codes = [c for c, _ in naics]
            primary = col("primary_naics")
            if primary and primary not in codes:
                codes.insert(0, primary)
            if not any(c.startswith(self.naics_prefixes) for c in codes):
                st.wrong_naics += 1
                continue
            if col("dnb_open_data") == "Y" and col("last_update_date") < DNB_CUTOFF:
                st.dnb_era += 1  # D&B-sourced data under SAM.gov's D&B terms: not used
                continue

            st.kept += 1
            st.per_state[col("state")] = st.per_state.get(col("state"), 0) + 1
            website = col("entity_url") or None
            if website:
                st.with_website += 1
            small = dict(naics)
            yield RawCompanyRecord(
                source=self.name,
                source_record_id=col("uei"),
                name=col("legal_name"),
                website=website,
                city=col("city").title() or None,
                state=col("state"),
                country="US",
                naics=",".join(codes),
                extra={
                    "uei": col("uei"),
                    "dba_name": col("dba_name") or None,
                    "primary_naics": primary or None,
                    "sba_small_for_primary_naics": small.get(primary) or None,
                    "registration_expires": col("expiration_date") or None,
                    "purpose_of_registration": col("purpose") or None,
                },
            )


# --- Download ---------------------------------------------------------------------------


def _usage_today(conn: sqlite3.Connection, today: str) -> int:
    row = conn.execute(
        "SELECT requests FROM api_usage WHERE api = ? AND day = ?", (API_NAME, today)
    ).fetchone()
    return row[0] if row else 0


def _count_request(conn: sqlite3.Connection, today: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO api_usage (api, day, requests) VALUES (?, ?, 1) "
            "ON CONFLICT(api, day) DO UPDATE SET requests = requests + 1",
            (API_NAME, today),
        )


def download_extract(
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    api_key: str,
    dest_dir: Path,
    year: int,
    month: int,
    user_agent: str,
    today: date,
    daily_budget: int = DEFAULT_DAILY_BUDGET,
) -> tuple[Path, bool]:
    """Download the public monthly extract for (year, month) unless it is already on disk.

    Returns (path, downloaded_now). One request per file; refuses past the daily budget. The API
    key is sent as a query parameter (as SAM.gov requires) and never logged or stored.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{year:04d}{month:02d}"
    existing = sorted(dest_dir.glob(f"SAM_PUBLIC_UTF-8_MONTHLY_V2_{tag}*.ZIP"))
    if existing:
        return existing[-1], False
    day = today.isoformat()
    used = _usage_today(conn, day)
    if used >= daily_budget:
        raise SamError(
            f"SAM.gov request budget for today is used up ({used}/{daily_budget}); try again tomorrow."
        )
    params = {
        "api_key": api_key,
        "fileType": "ENTITY",
        "sensitivity": "PUBLIC",
        "frequency": "MONTHLY",
        "charset": "UTF8",
        "date": f"{month:02d}/{year:04d}",
    }
    _count_request(conn, day)
    part = dest_dir / f"download-{tag}.part"
    try:
        with client.stream(
            "GET",
            EXTRACTS_URL,
            params=params,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(60.0, read=600.0),
            follow_redirects=True,
        ) as resp:
            if resp.status_code in (401, 403):
                raise SamError(
                    f"SAM.gov rejected the API key (HTTP {resp.status_code}); check SAM_API_KEY"
                )
            if resp.status_code == 404:
                raise SamError(
                    f"No public monthly extract for {month:02d}/{year} yet; try --month for an earlier month"
                )
            if resp.status_code == 429:
                raise SamError("SAM.gov daily request limit reached (HTTP 429); try again tomorrow")
            if resp.status_code != 200:
                raise SamError(f"SAM.gov returned HTTP {resp.status_code}")
            name = (
                _filename(resp.headers.get("content-disposition"))
                or f"SAM_PUBLIC_UTF-8_MONTHLY_V2_{tag}01.ZIP"
            )
            with part.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise SamError(f"Download failed ({type(exc).__name__})") from exc
    except SamError:
        part.unlink(missing_ok=True)
        raise
    if not zipfile.is_zipfile(part):
        part.unlink(missing_ok=True)
        raise SamError("SAM.gov did not return a ZIP file")
    final = dest_dir / name
    part.rename(final)
    return final, True


def _filename(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    m = re.search(r'filename="?([A-Za-z0-9_.\-]+\.ZIP)"?', content_disposition, re.I)
    return m.group(1) if m else None


def record_download(conn: sqlite3.Connection, path: Path) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "at": utcnow()}
