"""OpenStreetMap businesses with a website, from Geofabrik state extracts (DESIGN.md §6.5).

The public Overpass server asks commercial users to self-host, so we download each state's
.osm.pbf from Geofabrik (resumable, MD5-verified, kept under the data dir) and read it locally.
Only named features with a website tag that look like makers are kept: man_made=works,
manufacturing-type industrial=*, and maker crafts (brewery, winery, metal fabrication, ...).
Chains (brand=*) are skipped. OSM tags give a coarse NAICS where the mapping is clear.
Only name, website, city and the classifying tags are read; phone/email tags never are.

OSM data is © OpenStreetMap contributors, available under the ODbL.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import osmium

from dealsource.models import RawCompanyRecord
from dealsource.resolve.normalize import US_STATES
from dealsource.sources.base import register_company_source

GEOFABRIK_US = "https://download.geofabrik.de/north-america/us"
CHUNK = 1 << 20

# Crafts that make things, with the NAICS they map to (None = maker, industry unclear).
CRAFT_NAICS: dict[str, str | None] = {
    "brewery": "312120",
    "winery": "312130",
    "distillery": "312140",
    "cider": "312130",
    "metal_construction": "332312",
    "welder": "332312",
    "blacksmith": "332111",
    "window_construction": "332321",
    "cabinet_maker": "337110",
    "joiner": "321911",
    "furniture": "337122",
    "upholsterer": "337121",
    "sawmill": "321113",
    "boatbuilder": "336612",
    "confectionery": "311340",
    "bakery": "311812",
    "signmaker": "339950",
    "printer": "323111",
    "electronics": "334",
    "toolmaker": "333514",
    "agricultural_engines": "333111",
    "steel": "3311",
}
INDUSTRIAL_NAICS: dict[str, str | None] = {
    "brewery": "312120",
    "distillery": "312140",
    "winery": "312130",
    "bakery": "311812",
    "food_industry": "311",
    "food": "311",
    "machine_shop": "332710",
    "metal_processing": "332",
    "metal_working": "332",
    "metal": "332",
    "steel": "3311",
    "furniture": "337",
    "plastic": "3261",
    "plastics": "3261",
    "chemical": "325",
    "paper": "322",
    "sawmill": "321113",
    "concrete_plant": "327320",
    "printing": "323111",
    "textile": "313",
    "aerospace": "336413",
    "electronics": "334",
    "automotive": "3363",
    "factory": None,
    "manufacturing": None,
    "yes": None,
}
PRODUCT_NAICS: dict[str, str] = {
    "beer": "312120",
    "wine": "312130",
    "spirits": "312140",
    "bread": "311812",
    "furniture": "337",
    "concrete": "327320",
    "steel": "3311",
    "plastic": "3261",
    "paper": "322",
    "chemicals": "325",
    "food": "311",
    "machinery": "333",
}
READ_KEYS = (
    "name",
    "website",
    "contact:website",
    "addr:city",
    "brand",
    "man_made",
    "industrial",
    "craft",
    "product",
)


def geofabrik_slug(state_code: str) -> str:
    names = {code: name for name, code in US_STATES.items()}
    return names[state_code].replace(" ", "-")


def classify(tags: dict[str, str]) -> tuple[bool, str | None, str]:
    """(is a maker, NAICS or None, which tag decided)."""
    craft = tags.get("craft")
    if craft in CRAFT_NAICS:
        return True, CRAFT_NAICS[craft], f"craft={craft}"
    industrial = tags.get("industrial")
    if industrial in INDUSTRIAL_NAICS:
        naics = INDUSTRIAL_NAICS[industrial] or PRODUCT_NAICS.get(tags.get("product", ""))
        return True, naics, f"industrial={industrial}"
    if tags.get("man_made") == "works":
        return True, PRODUCT_NAICS.get(tags.get("product", "")), "man_made=works"
    return False, None, ""


@dataclass
class OsmStats:
    with_website: int = 0
    kept: int = 0
    with_naics: int = 0
    skipped_chains: int = 0
    skipped_not_maker: int = 0
    skipped_no_name: int = 0


@register_company_source
class OsmSource:
    name = "osm"

    def __init__(self, path: Path, *, state: str):
        self.path = Path(path)
        self.state = state
        self.stats = OsmStats()

    def iter_records(self) -> Iterator[RawCompanyRecord]:
        st = self.stats
        processor = osmium.FileProcessor(
            str(self.path), osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION
        ).with_filter(osmium.filter.KeyFilter("website", "contact:website"))
        for obj in processor:
            tags = {k: obj.tags.get(k) for k in READ_KEYS if k in obj.tags}
            st.with_website += 1
            name = (tags.get("name") or "").strip()
            if not name:
                st.skipped_no_name += 1
                continue
            if tags.get("brand"):
                st.skipped_chains += 1
                continue
            maker, naics, decided_by = classify(tags)
            if not maker:
                st.skipped_not_maker += 1
                continue
            st.kept += 1
            if naics:
                st.with_naics += 1
            yield RawCompanyRecord(
                source=self.name,
                source_record_id=f"{obj.type_str()}{obj.id}",
                name=name,
                website=tags.get("website") or tags.get("contact:website"),
                city=tags.get("addr:city"),
                state=self.state,
                country="US",
                naics=naics,
                extra={
                    "osm": f"{obj.type_str()}{obj.id}",
                    "osm_tag": decided_by,
                    "naics_source": "osm_tag" if naics else None,
                    "license": "ODbL; © OpenStreetMap contributors",
                },
            )


# --- Download ------------------------------------------------------------------------------


class OsmDownloadError(RuntimeError):
    pass


@dataclass
class DownloadProgress:
    state: str
    done: int
    total: int | None
    resumed_from: int = 0
    notes: list[str] = field(default_factory=list)


def download_state(
    client: httpx.Client,
    state: str,
    dest_dir: Path,
    *,
    user_agent: str,
    refresh: bool = False,
    progress: Callable[[DownloadProgress], None] | None = None,
) -> tuple[Path, bool]:
    """Download (or resume) the Geofabrik extract for a state. Returns (path, downloaded_now).

    The '-latest' URL redirects to a dated file; the partial download is pinned to that dated
    URL so a resume never mixes two versions. The finished file is checked against Geofabrik's
    published .md5.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    slug = geofabrik_slug(state)
    finished = sorted(dest_dir.glob(f"{slug}-*.osm.pbf"))
    if finished and not refresh:
        return finished[-1], False
    headers = {"User-Agent": user_agent}
    part = dest_dir / f"{slug}.osm.pbf.part"
    pin = dest_dir / f"{slug}.osm.pbf.part.url"

    if part.exists() and pin.exists():
        url = pin.read_text().strip()
    else:
        part.unlink(missing_ok=True)
        resp = client.head(
            f"{GEOFABRIK_US}/{slug}-latest.osm.pbf", headers=headers, follow_redirects=False
        )
        location = resp.headers.get("location")
        url = (
            str(httpx.URL(f"{GEOFABRIK_US}/").join(location))
            if location
            else f"{GEOFABRIK_US}/{slug}-latest.osm.pbf"
        )
        pin.write_text(url)
    name = url.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[a-z\-]+-(\d{6}|latest)\.osm\.pbf", name):
        raise OsmDownloadError(f"Unexpected Geofabrik file name {name!r}")

    start = part.stat().st_size if part.exists() else 0
    req_headers = dict(headers)
    if start:
        req_headers["Range"] = f"bytes={start}-"
    try:
        with client.stream(
            "GET",
            url,
            headers=req_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=300.0),
        ) as resp:
            if resp.status_code == 200 and start:
                start = 0  # server ignored the range: start over
            elif resp.status_code not in (200, 206):
                raise OsmDownloadError(f"Geofabrik returned HTTP {resp.status_code} for {name}")
            length = resp.headers.get("content-length")
            total = start + int(length) if length else None
            info = DownloadProgress(state, start, total, resumed_from=start)
            with part.open("ab" if start else "wb") as out:
                last_report = start
                for chunk in resp.iter_bytes(CHUNK):
                    out.write(chunk)
                    info.done += len(chunk)
                    if progress and info.done - last_report >= 25 * CHUNK:
                        progress(info)
                        last_report = info.done
            if progress:
                progress(info)
    except httpx.HTTPError as exc:
        raise OsmDownloadError(
            f"Download of {name} interrupted ({type(exc).__name__}); rerun to resume from "
            f"{part.stat().st_size if part.exists() else 0:,} bytes"
        ) from exc

    md5_resp = client.get(f"{url}.md5", headers=headers, follow_redirects=True)
    if md5_resp.status_code == 200:
        expected = md5_resp.text.split()[0].strip().lower()
        digest = hashlib.md5(usedforsecurity=False)
        with part.open("rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            pin.unlink(missing_ok=True)
            raise OsmDownloadError(
                f"{name} failed its MD5 check and was deleted; rerun to download again"
            )
    final_name = name if not name.endswith("-latest.osm.pbf") else f"{slug}-latest.osm.pbf"
    final = dest_dir / final_name
    for old in finished:
        if old != final:
            old.unlink(missing_ok=True)
    part.rename(final)
    pin.unlink(missing_ok=True)
    return final, True
