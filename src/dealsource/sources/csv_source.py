"""CSV company import.

Maps columns to RawCompanyRecord fields (auto-detected or via ``--map``), and drops any column
that looks like personal contact information before anything is stored. Contact details that
slip into free-text fields are scrubbed out.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from dealsource.db import utcnow
from dealsource.models import RawCompanyRecord
from dealsource.privacy import EMAIL_RE, LINKEDIN_PERSON_RE, PHONE_RE, scrub_contact_info
from dealsource.sources.base import register_company_source

# Recognised header spellings per field (compared after lowercasing and removing non-letters).
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "company",
        "companyname",
        "businessname",
        "legalname",
        "accountname",
        "organization",
        "organisation",
    ),
    "website": (
        "website",
        "url",
        "domain",
        "web",
        "homepage",
        "site",
        "websiteurl",
        "companywebsite",
    ),
    "city": ("city", "town", "hqcity"),
    "state": ("state", "st", "province", "stateprovince", "hqstate"),
    "country": ("country", "hqcountry"),
    "naics": ("naics", "naicscode", "primarynaics"),
    "employees": ("employees", "employeecount", "headcount", "numemployees", "fte", "staffcount"),
    "revenue_usd_m": (
        "revenue",
        "annualrevenue",
        "sales",
        "revenueusd",
        "revenuem",
        "revenueusdm",
        "revenuemm",
    ),
    "description": ("description", "desc", "about", "businessdescription"),
    "id": ("id", "recordid", "companyid", "sourceid"),
}

# Header words / word pairs that mark a column as personal contact information.
CONTACT_WORDS = frozenset(
    {
        "email",
        "emails",
        "phone",
        "phones",
        "telephone",
        "tel",
        "mobile",
        "cell",
        "fax",
        "contact",
        "contacts",
        "linkedin",
        "person",
        "ceo",
        "president",
        "street",
        "address",
        "zip",
        "zipcode",
        "postal",
    }
)
CONTACT_PAIRS = frozenset(
    {
        ("e", "mail"),
        ("first", "name"),
        ("last", "name"),
        ("full", "name"),
        ("owner", "name"),
        ("linked", "in"),
    }
)


REVENUE_UNITS = {"usd": 1e-6, "usd_k": 1e-3, "usd_m": 1.0}

# A column is contact data if at least half of its non-empty values are *entirely* an email,
# phone number or personal LinkedIn URL, whatever its header says. Contact details that only
# appear inside free text (e.g. a notes column) are scrubbed instead of dropping the column.
CONTACT_VALUE_SHARE = 0.5


def squash(header: str) -> str:
    return re.sub(r"[^a-z]", "", header.lower())


def header_words(header: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", header)
    return re.findall(r"[a-z]+", spaced.lower())


def is_contact_header(header: str) -> bool:
    words = header_words(header)
    if any(w in CONTACT_WORDS or w.startswith("email") for w in words):
        return True
    return any(pair in CONTACT_PAIRS for pair in zip(words, words[1:], strict=False))


def looks_like_contact_value(value: str) -> bool:
    v = value.strip()
    return bool(EMAIL_RE.fullmatch(v) or PHONE_RE.fullmatch(v) or LINKEDIN_PERSON_RE.search(v))


def parse_int(value: str | None) -> int | None:
    """Parse '1,200', '~50', '50-100' (midpoint) or '200+'; None if unparseable."""
    if not value:
        return None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", value.replace(",", ""))]
    if not nums:
        return None
    if len(nums) >= 2 and re.search(r"\d\s*(-|–|to)\s*\d", value):
        return round((nums[0] + nums[1]) / 2)
    return round(nums[0])


def parse_float(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", "").replace("$", ""))
    return float(m.group()) if m else None


@dataclass
class ColumnPlan:
    mapping: dict[str, str]  # field -> header
    dropped: list[str]  # headers dropped as contact-like
    extra: list[str] = field(default_factory=list)  # headers kept in ``extra``


class ContactColumnError(ValueError):
    pass


def plan_columns(
    headers: list[str], rows: list[dict[str, str]], overrides: dict[str, str]
) -> ColumnPlan:
    unknown = set(overrides) - set(HEADER_ALIASES)
    if unknown:
        raise ValueError(
            f"Unknown field(s) in --map: {sorted(unknown)}; valid: {sorted(HEADER_ALIASES)}"
        )
    missing = [h for h in overrides.values() if h not in headers]
    if missing:
        raise ValueError(f"--map refers to column(s) not in the file: {missing}")

    dropped = []
    for h in headers:
        values = [r.get(h) or "" for r in rows]
        non_empty = [v for v in values if v.strip()]
        by_value = (
            non_empty
            and sum(looks_like_contact_value(v) for v in non_empty) / len(non_empty)
            >= CONTACT_VALUE_SHARE
        )
        if is_contact_header(h) or by_value:
            dropped.append(h)

    mapped_contact = [h for h in overrides.values() if h in dropped]
    if mapped_contact:
        raise ContactColumnError(
            f"Refusing to import column(s) that look like contact details: {mapped_contact}"
        )

    mapping = dict(overrides)
    for fld, aliases in HEADER_ALIASES.items():
        if fld in mapping:
            continue
        for h in headers:
            if h not in dropped and h not in mapping.values() and squash(h) in aliases:
                mapping[fld] = h
                break
    if "name" not in mapping:
        raise ValueError("Could not find a company-name column; pass --map name=<column>")
    used = set(mapping.values())
    extra = [h for h in headers if h not in used and h not in dropped]
    return ColumnPlan(mapping=mapping, dropped=dropped, extra=extra)


@register_company_source
class CSVSource:
    name = "csv"

    def __init__(
        self,
        path: Path,
        *,
        source_name: str = "csv",
        column_map: dict[str, str] | None = None,
        revenue_unit: str = "usd_m",
    ):
        if revenue_unit not in REVENUE_UNITS:
            raise ValueError(f"revenue unit must be one of {sorted(REVENUE_UNITS)}")
        self.path = Path(path)
        self.source_name = source_name
        self.column_map = column_map or {}
        self.revenue_factor = REVENUE_UNITS[revenue_unit]
        self.plan: ColumnPlan | None = None
        self.skipped_rows = 0

    def _read(self) -> tuple[list[str], list[dict[str, str]]]:
        with self.path.open(newline="", encoding="utf-8-sig") as f:
            sample = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            rows = list(reader)
            return list(reader.fieldnames or []), rows

    def iter_records(self) -> Iterator[RawCompanyRecord]:
        headers, rows = self._read()
        self.plan = plan_columns(headers, rows, self.column_map)
        m = self.plan.mapping

        def get(fld: str) -> str | None:
            h = m.get(fld)
            v = (row.get(h) or "").strip() if h else ""
            return v or None

        for row in rows:
            name = get("name")
            if not name:
                self.skipped_rows += 1
                continue
            extra = {
                h: scrub_contact_info(v.strip())
                for h in self.plan.extra
                if (v := row.get(h)) and v.strip()
            }
            revenue = parse_float(get("revenue_usd_m"))
            description = get("description")
            record_id = get("id") or row_hash(
                row, [h for h in headers if h not in self.plan.dropped]
            )
            yield RawCompanyRecord(
                source=self.source_name,
                source_record_id=record_id,
                name=name,
                website=get("website"),
                city=get("city"),
                state=get("state"),
                country=get("country"),
                naics=get("naics"),
                employees=parse_int(get("employees")),
                revenue_usd_m=None if revenue is None else round(revenue * self.revenue_factor, 4),
                description=scrub_contact_info(description) if description else None,
                extra=extra,
            )


def row_hash(row: dict[str, str], headers: list[str]) -> str:
    canonical = json.dumps([(h, (row.get(h) or "").strip().lower()) for h in sorted(headers)])
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def store_records(conn: sqlite3.Connection, records: Iterator[RawCompanyRecord]) -> dict[str, int]:
    """Upsert raw records. Returns counts of inserted / updated / unchanged rows."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    now = utcnow()
    with conn:
        for rec in records:
            payload = rec.model_dump_json(exclude={"source", "source_record_id"})
            digest = hashlib.sha256(payload.encode()).hexdigest()
            existing = conn.execute(
                "SELECT payload_hash FROM raw_records WHERE source = ? AND source_record_id = ?",
                (rec.source, rec.source_record_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO raw_records (source, source_record_id, payload_json, payload_hash, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rec.source, rec.source_record_id, payload, digest, now),
                )
                counts["inserted"] += 1
            elif existing["payload_hash"] != digest:
                conn.execute(
                    "UPDATE raw_records SET payload_json = ?, payload_hash = ?, ingested_at = ? "
                    "WHERE source = ? AND source_record_id = ?",
                    (payload, digest, now, rec.source, rec.source_record_id),
                )
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
    return counts
