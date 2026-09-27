"""Shared fixtures. Every test runs in a temporary data dir with no .env and no network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dealsource import db
from dealsource.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never read the real .env or touch the real private/ folder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEALSOURCE_SKIP_DOTENV", "1")
    monkeypatch.setenv("DEALSOURCE_DATA_DIR", str(tmp_path / "data"))
    for var in ("CENSUS_API_KEY", "DEALSOURCE_SOURCE_PRIORITY", "DEALSOURCE_USER_AGENT_CONTACT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.from_env()


@pytest.fixture
def conn(settings):
    connection = db.connect(settings.db_path)
    yield connection
    connection.close()


@pytest.fixture
def split_ready(settings) -> Settings:
    """A synthetic labels split so pipeline commands are allowed to run."""
    settings.ensure_data_dir()
    settings.split_manifest_path.write_text(json.dumps({"version": 1, "assignments": {}}))
    return settings


class CBPServer:
    """Serves saved CBP fixture responses, keyed by (NAICS code, geography), and counts calls."""

    ROUTES = {
        ("332300", "state:13,37"): "2022_332300_state_13_37.json",
        ("332700", "us:1"): "2022_332700_us.json",
    }

    def __init__(self):
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        params = request.url.params
        if params.get("key") == "BAD":
            return httpx.Response(
                302, headers={"Location": "https://api.census.gov/data/missing_key.html"}
            )
        name = self.ROUTES.get((params.get("NAICS2017"), params.get("for")))
        if name is None:
            return httpx.Response(204)
        return httpx.Response(200, content=(FIXTURES / "cbp" / name).read_bytes())

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def cbp_server() -> CBPServer:
    return CBPServer()
