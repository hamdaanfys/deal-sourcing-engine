"""Load and validate the analyst's labels file (DESIGN.md §11.1-11.2).

Only the company name, website, state and decision columns are read; every other column
(notes, contact details, anything else) is ignored and never loaded.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dealsource.resolve.normalize import domain_key, name_key, normalize_state
from dealsource.sources.csv_source import is_contact_header, squash

PURSUE, PASS = "pursue", "pass"

DEFAULT_DECISION_VALUES = {
    "1": PURSUE,
    "yes": PURSUE,
    "y": PURSUE,
    "pursue": PURSUE,
    "target": PURSUE,
    "true": PURSUE,
    "0": PASS,
    "no": PASS,
    "n": PASS,
    "pass": PASS,
    "false": PASS,
}

LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": ("companyname", "company", "name", "businessname", "legalname", "accountname"),
    "website": ("website", "url", "domain", "web", "homepage", "site", "websiteurl"),
    "state": ("state", "st", "province", "hqstate"),
    "decision": ("decision", "label", "outcome", "verdict", "pursue", "target"),
}
REQUIRED = ("company_name", "decision")


class LabelsError(ValueError):
    pass


@dataclass(frozen=True)
class LabelRow:
    row_number: int  # 1-based data row (header excluded), for error messages
    company_name: str
    website: str | None
    state: str | None
    decision: str  # PURSUE or PASS
    key: str


def label_key(company_name: str, website: str | None, state: str | None) -> str:
    """Stable key independent of pipeline state: domain if there is one, else name + state."""
    domain = domain_key(website)
    if domain:
        return f"d:{domain}"
    return f"n:{name_key(company_name)}|{(normalize_state(state) or '').lower()}"


def _plan(headers: list[str], column_map: dict[str, str]) -> dict[str, str]:
    unknown = set(column_map) - set(LABEL_ALIASES)
    if unknown:
        raise LabelsError(
            f"Unknown field(s) in --map: {sorted(unknown)}; valid: {sorted(LABEL_ALIASES)}"
        )
    missing = [h for h in column_map.values() if h not in headers]
    if missing:
        raise LabelsError(f"--map refers to column(s) not in the labels file: {missing}")
    contact = [h for h in column_map.values() if is_contact_header(h)]
    if contact:
        raise LabelsError(f"Refusing to read column(s) that look like contact details: {contact}")
    mapping = dict(column_map)
    for fld, aliases in LABEL_ALIASES.items():
        if fld in mapping:
            continue
        for h in headers:
            if h not in mapping.values() and not is_contact_header(h) and squash(h) in aliases:
                mapping[fld] = h
                break
    absent = [f for f in REQUIRED if f not in mapping]
    if absent:
        raise LabelsError(f"Labels file needs column(s) for {absent}; pass --map field=Column")
    return mapping


def load_labels(
    path: Path,
    column_map: dict[str, str] | None = None,
    decision_values: dict[str, str] | None = None,
) -> list[LabelRow]:
    values = {k.lower(): v for k, v in (decision_values or DEFAULT_DECISION_VALUES).items()}
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        mapping = _plan(list(reader.fieldnames or []), column_map or {})
        rows = []
        bad_decisions: list[int] = []
        missing_names: list[int] = []
        for i, raw in enumerate(reader, start=1):
            if not any((v or "").strip() for v in raw.values()):
                continue  # blank line
            name = (raw.get(mapping["company_name"]) or "").strip()
            website = (
                (raw.get(mapping["website"]) or "").strip() or None
                if "website" in mapping
                else None
            )
            state = (
                (raw.get(mapping["state"]) or "").strip() or None if "state" in mapping else None
            )
            decision = values.get((raw.get(mapping["decision"]) or "").strip().lower())
            if not name:
                missing_names.append(i)
                continue
            if decision is None:
                bad_decisions.append(i)
                continue
            rows.append(
                LabelRow(i, name, website, state, decision, label_key(name, website, state))
            )
    # Errors cite row numbers only, never company names.
    problems = []
    if missing_names:
        problems.append(
            f"{len(missing_names)} row(s) without a company name (rows {_fmt(missing_names)})"
        )
    if bad_decisions:
        accepted = ", ".join(sorted(values))
        problems.append(
            f"{len(bad_decisions)} row(s) with an unrecognised decision (rows {_fmt(bad_decisions)}); "
            f"accepted values: {accepted}"
        )
    if problems:
        raise LabelsError("; ".join(problems))
    if not rows:
        raise LabelsError("The labels file has no rows")
    return rows


def _fmt(numbers: list[int], limit: int = 10) -> str:
    shown = ", ".join(str(n) for n in numbers[:limit])
    return shown + (f", … (+{len(numbers) - limit} more)" if len(numbers) > limit else "")


@dataclass(frozen=True)
class Groups:
    decisions: dict[str, str]  # label key -> decision, for consistent groups
    rows_per_key: dict[str, list[LabelRow]]
    conflicts: dict[str, list[LabelRow]]  # keys whose rows disagree


def group_labels(rows: list[LabelRow]) -> Groups:
    by_key: dict[str, list[LabelRow]] = defaultdict(list)
    for r in rows:
        by_key[r.key].append(r)
    decisions, conflicts = {}, {}
    for key, group in by_key.items():
        found = {r.decision for r in group}
        if len(found) == 1:
            decisions[key] = found.pop()
        else:
            conflicts[key] = group
    return Groups(decisions, dict(by_key), conflicts)


def write_conflicts(path: Path, conflicts: dict[str, list[LabelRow]]) -> None:
    """Company-level detail for the analyst's own review; lives under the private data dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label_key", "row_number", "company_name", "website", "state", "decision"])
        for key in sorted(conflicts):
            for r in conflicts[key]:
                w.writerow(
                    [key, r.row_number, r.company_name, r.website or "", r.state or "", r.decision]
                )
