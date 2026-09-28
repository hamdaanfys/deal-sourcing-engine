"""OpenStreetMap discovery: tag classification, parsing, resumable verified download. No network."""

import hashlib
import json

import httpx
import pytest
from conftest import EXAMPLE_THESIS, FIXTURES
from typer.testing import CliRunner

from dealsource import cli, db
from dealsource.sources.csv_source import store_records
from dealsource.sources.osm import (
    OsmDownloadError,
    OsmSource,
    classify,
    download_state,
    geofabrik_slug,
)

SAMPLE = FIXTURES / "osm" / "georgia-sample.osm"


def test_classify():
    assert classify({"craft": "brewery"}) == (True, "312120", "craft=brewery")
    assert classify({"industrial": "machine_shop"}) == (True, "332710", "industrial=machine_shop")
    assert classify({"man_made": "works", "product": "beer"}) == (True, "312120", "man_made=works")
    assert classify({"man_made": "works"}) == (True, None, "man_made=works")
    assert classify({"craft": "photographer"})[0] is False
    assert classify({"shop": "bakery"})[0] is False


def test_geofabrik_slug():
    assert geofabrik_slug("GA") == "georgia" and geofabrik_slug("NC") == "north-carolina"


def test_parse_keeps_named_non_chain_makers_with_websites():
    source = OsmSource(SAMPLE, state="GA")
    records = {r.name: r for r in source.iter_records()}
    assert set(records) == {
        "Ocmulgee Brewing Co",
        "Peach State Fabricators",
        "Dalton Carpet Mill",
        "Savannah Precision Machine",
    }
    brew = records["Ocmulgee Brewing Co"]
    assert (brew.website, brew.city, brew.state, brew.naics) == (
        "https://www.ocmulgeebrewing.test/",
        "Macon",
        "GA",
        "312120",
    )
    assert brew.source_record_id == "n101" and brew.extra["license"].startswith("ODbL")
    assert records["Peach State Fabricators"].website == "peachstatefab.test"  # contact:website
    assert records["Dalton Carpet Mill"].naics is None  # product=carpet has no mapping
    st = source.stats
    assert (st.with_website, st.kept, st.with_naics) == (7, 4, 3)
    assert (st.skipped_chains, st.skipped_not_maker, st.skipped_no_name) == (1, 1, 1)


def test_phone_and_email_tags_never_reach_the_database(conn):
    store_records(conn, OsmSource(SAMPLE, state="GA").iter_records())
    blob = " ".join(r[0] for r in conn.execute("SELECT payload_json FROM raw_records"))
    assert "555" not in blob and "owner@" not in blob


# --- download ---------------------------------------------------------------------------------

DATA = b"PBFDATA" * 1000
DATED = "https://download.geofabrik.de/north-america/us/georgia-260927.osm.pbf"


class Geofabrik:
    def __init__(self, data=DATA, md5=None, honour_range=True, fail_after=None):
        self.data, self.honour_range, self.fail_after = data, honour_range, fail_after
        self.md5 = md5 or hashlib.md5(data).hexdigest()
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        url = str(request.url)
        if request.method == "HEAD":
            return httpx.Response(302, headers={"location": DATED})
        if url.endswith(".md5"):
            return httpx.Response(200, text=f"{self.md5}  georgia-260927.osm.pbf\n")
        rng = request.headers.get("range")
        if rng and self.honour_range:
            start = int(rng.split("=")[1].rstrip("-"))
            body = self.data[start:]
            return httpx.Response(206, content=body, headers={"content-length": str(len(body))})
        body = self.data if self.fail_after is None else self.data[: self.fail_after]
        return httpx.Response(200, content=body, headers={"content-length": str(len(self.data))})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_full_download_is_verified_and_reused(tmp_path):
    server = Geofabrik()
    path, fresh = download_state(server.client(), "GA", tmp_path, user_agent="ua")
    assert fresh and path.name == "georgia-260927.osm.pbf" and path.read_bytes() == DATA
    assert not list(tmp_path.glob("*.part*"))
    again, fresh2 = download_state(server.client(), "GA", tmp_path, user_agent="ua")
    assert (again, fresh2) == (path, False)


def test_interrupted_download_resumes_with_a_range_request(tmp_path):
    (tmp_path / "georgia.osm.pbf.part").write_bytes(DATA[:3000])
    (tmp_path / "georgia.osm.pbf.part.url").write_text(DATED)
    server = Geofabrik()
    seen = []
    path, _ = download_state(server.client(), "GA", tmp_path, user_agent="ua", progress=seen.append)
    assert path.read_bytes() == DATA
    gets = [r for r in server.requests if r.method == "GET" and not str(r.url).endswith(".md5")]
    assert gets[0].headers["range"] == "bytes=3000-"
    assert not any(r.method == "HEAD" for r in server.requests)  # pinned to the dated URL
    assert seen[-1].resumed_from == 3000 and seen[-1].done == len(DATA)


def test_server_ignoring_range_restarts_cleanly(tmp_path):
    (tmp_path / "georgia.osm.pbf.part").write_bytes(b"GARBAGE")
    (tmp_path / "georgia.osm.pbf.part.url").write_text(DATED)
    path, _ = download_state(
        Geofabrik(honour_range=False).client(), "GA", tmp_path, user_agent="ua"
    )
    assert path.read_bytes() == DATA


def test_md5_mismatch_deletes_the_file(tmp_path):
    with pytest.raises(OsmDownloadError, match="MD5"):
        download_state(Geofabrik(md5="0" * 32).client(), "GA", tmp_path, user_agent="ua")
    assert list(tmp_path.iterdir()) == []


def test_network_failure_keeps_partial_file_for_resume(tmp_path):
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(302, headers={"location": DATED})
        raise httpx.ReadError("connection reset")

    with pytest.raises(OsmDownloadError, match="rerun to resume"):
        download_state(
            httpx.Client(transport=httpx.MockTransport(handler)), "GA", tmp_path, user_agent="ua"
        )
    assert (tmp_path / "georgia.osm.pbf.part.url").read_text() == DATED


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_discover_osm_uses_file_on_disk(settings, monkeypatch):
    settings.osm_dir.mkdir(parents=True)
    (settings.osm_dir / "georgia-260927.osm").write_bytes(SAMPLE.read_bytes())
    # pyosmium picks the format from the extension, so the XML fixture keeps its .osm name and the
    # download step is replaced by one that returns it.
    monkeypatch.setattr(
        cli, "download_state", lambda *a, **k: (settings.osm_dir / "georgia-260927.osm", False)
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["discover", "osm", "--thesis", str(EXAMPLE_THESIS), "--state", "GA"]
    )
    assert result.exit_code == 0, result.output
    assert "7 features with a website -> 4 makers (3 with a NAICS from tags)" in result.output
    assert "Ocmulgee" not in result.output and "ODbL" in result.output
    conn = db.connect(settings.db_path)
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM raw_records WHERE source_record_id = 'n101'"
        ).fetchone()[0]
    )
    assert payload["naics"] == "312120"
