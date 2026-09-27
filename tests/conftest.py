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


# --- Enrichment test doubles --------------------------------------------------------------

SITES = FIXTURES / "sites"
OLLAMA = FIXTURES / "ollama"


class FakeClock:
    """Monotonic time that only moves when something sleeps (or a test advances it)."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SiteServer:
    """Serves saved HTML fixtures and configurable responses by host + path, logging requests.

    A route value is (status, headers, body) or an exception instance to raise.
    """

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []

    def add(self, host: str, path: str, status=200, body: bytes | str = b"", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.routes[(host, path)] = (
            status,
            {"content-type": "text/html; charset=utf-8", **(headers or {})},
            body,
        )

    def add_error(self, host: str, path: str, exc: Exception):
        self.routes[(host, path)] = exc

    def add_acme(
        self,
        host: str = "acme-precision.test",
        robots: str = "User-agent: *\nDisallow: /private/\n",
    ):
        self.add(host, "/robots.txt", body=robots, headers={"content-type": "text/plain"})
        self.add(host, "/", body=(SITES / "acme" / "index.html").read_bytes())
        self.add(host, "/about-us", body=(SITES / "acme" / "about.html").read_bytes())
        self.add(host, "/products", body=(SITES / "acme" / "products.html").read_bytes())
        self.add(
            host,
            "/products/steam-boilers",
            body="<html><body><p>Industrial steam boilers.</p></body></html>",
        )
        self.add(
            host,
            "/capabilities",
            body="<html><body><p>5-axis milling and Swiss turning.</p></body></html>",
        )
        self.add(host, "/contact-us", body=(SITES / "acme" / "contact.html").read_bytes())
        self.add(host, "/our-team", body=(SITES / "acme" / "contact.html").read_bytes())

    def paths_requested(self, host: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if host is None or r.url.host == host]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.url.host, request.url.path))
        if route is None:
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")
        if isinstance(route, Exception):
            raise route
        status, headers, body = route
        return httpx.Response(status, headers=headers, content=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeLLMBackend:
    """Returns queued replies (strings, or exceptions to raise); records every request."""

    name = "fake"

    def __init__(
        self,
        replies=None,
        *,
        model: str = "fake-model",
        prompt_tokens=1000,
        completion_tokens=200,
        latency_ms=1500.0,
        available: bool = True,
    ):
        self.model = model
        self.replies = list(replies or [])
        self.default_reply = None
        self.calls: list[list[dict]] = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.latency_ms = latency_ms
        self.available = available

    def check(self) -> None:
        from dealsource.llm.base import LLMUnavailable

        if not self.available:
            raise LLMUnavailable("Ollama is not reachable (fake)")

    def chat(self, messages, schema, options):
        from dealsource.llm.base import LLMResult

        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else self.default_reply
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            raise AssertionError("FakeLLMBackend has no reply queued")
        return LLMResult(
            reply, self.model, self.prompt_tokens, self.completion_tokens, self.latency_ms
        )


def extraction_json(**overrides) -> str:
    data = {
        "summary": "Acme Precision machines tight-tolerance components for aerospace and medical customers.",
        "product_lines": ["CNC turned components", "5-axis milled housings"],
        "end_markets": ["aerospace", "medical devices"],
        "business_model": "manufacturer",
        "size_signals": {
            "employee_count": 85,
            "employee_count_quote": "Our team of 85 employees",
            "facility_count": 2,
            "facility_sqft_total": 60000,
            "founded_year": 1962,
        },
        "ownership": {
            "founder_led": "unknown",
            "family_owned": "yes",
            "generation": 3,
            "pe_or_strategic_backed": "unknown",
            "publicly_traded": "unknown",
        },
        "evidence": [
            {
                "claim": "family_owned",
                "page": "/about-us",
                "quote": "Founded in 1962 by John Smith, Acme Precision is a third-generation, family-owned company.",
            }
        ],
    }
    data.update(overrides)
    return json.dumps(data)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def site_server() -> SiteServer:
    return SiteServer()


@pytest.fixture
def fake_llm() -> FakeLLMBackend:
    return FakeLLMBackend()
