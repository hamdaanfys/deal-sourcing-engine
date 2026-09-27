"""End-to-end enrichment with saved HTML, a fake site server and a fake LLM. No network."""

import json

import httpx
import pytest
from conftest import FakeLLMBackend, extraction_json
from typer.testing import CliRunner

from dealsource import cli
from dealsource.enrich import pipeline
from dealsource.enrich.fetcher import PoliteFetcher, is_cacheable_page
from dealsource.enrich.pipeline import EnrichConfig, enrich
from dealsource.enrich.prompts import PROMPT_VERSION
from dealsource.httpcache import CachedHttp
from dealsource.llm.base import LLMTimeout, LLMUnavailable
from dealsource.llm.cache import LLMRunner
from dealsource.privacy import contains_contact_info

HOST = "acme-precision.test"


def add_company(conn, name, domain=None, city="Macon", state="GA"):
    cur = conn.execute(
        "INSERT INTO companies (canonical_name, domain, city, state, created_at, updated_at) VALUES (?, ?, ?, ?, 'x', 'x')",
        (name, domain, city, state),
    )
    conn.commit()
    return cur.lastrowid


def run_enrich(conn, server, llm, clock, **kw):
    http = CachedHttp(
        conn, server.client(), "dealsource/test (+https://example.org)", cacheable=is_cacheable_page
    )
    fetcher = PoliteFetcher(http, clock=clock, min_delay=2.0)
    runner = LLMRunner(conn, llm, prompt_version=PROMPT_VERSION)
    return enrich(
        conn, fetcher=fetcher, runner=runner, clock=clock, config=EnrichConfig(max_pages=5), **kw
    ), fetcher


def row(conn, company_id):
    return conn.execute("SELECT * FROM enrichments WHERE company_id = ?", (company_id,)).fetchone()


def test_happy_path_extracts_masks_and_records_metrics(conn, site_server, clock):
    site_server.add_acme()
    cid = add_company(conn, "Acme Precision", HOST)
    llm = FakeLLMBackend([extraction_json()])
    stats, _ = run_enrich(conn, site_server, llm, clock)

    assert stats["statuses"] == {"ok": 1}
    r = row(conn, cid)
    assert r["status"] == "ok"
    data = json.loads(r["extraction_json"])
    assert data["size_signals"]["employee_count"] == 85
    assert data["ownership"]["family_owned"] == "yes"
    assert "John Smith" not in r["extraction_json"] and "[PERSON]" in data["evidence"][0]["quote"]
    assert json.loads(r["pages_used_json"]) == [
        "/",
        "/about-us",
        "/products",
        "/products/steam-boilers",
        "/capabilities",
    ]
    assert (r["prompt_tokens"], r["completion_tokens"], r["llm_ms"]) == (1000, 200, 1500.0)
    assert r["total_ms"] >= r["fetch_ms"] > 0  # fake clock advanced by the politeness delays
    assert r["mask_version"] == 1

    # What the LLM saw: business pages only, no contact details.
    sent = llm.calls[0][1]["content"]
    assert "### /about-us" in sent and "5-axis milled housings" in sent
    assert not contains_contact_info(sent)
    assert "Jane Doe" not in sent
    requested = site_server.paths_requested()
    assert (
        "/contact-us" not in requested
        and "/our-team" not in requested
        and "/careers" not in requested
    )


def test_rerun_uses_caches_only(conn, site_server, clock):
    site_server.add_acme()
    cid = add_company(conn, "Acme Precision", HOST)
    llm = FakeLLMBackend([extraction_json()])
    run_enrich(conn, site_server, llm, clock)
    first = dict(row(conn, cid))
    n_requests = len(site_server.requests)

    stats, fetcher = run_enrich(conn, site_server, llm, clock)
    assert len(site_server.requests) == n_requests and fetcher.requests_made == 0
    assert len(llm.calls) == 1
    again = dict(row(conn, cid))
    assert again["extraction_json"] == first["extraction_json"] and again["llm_ms"] == 0.0
    hits = conn.execute("SELECT cache_hit FROM llm_calls ORDER BY id").fetchall()
    assert [h[0] for h in hits] == [0, 1]


def test_company_without_website(conn, site_server, clock):
    cid = add_company(conn, "Oakmont Valve Service")
    stats, _ = run_enrich(conn, site_server, FakeLLMBackend(), clock)
    assert row(conn, cid)["status"] == "no_website" and stats["statuses"] == {"no_website": 1}
    assert site_server.requests == []


def test_blocked_site_skips_llm(conn, site_server, clock):
    site_server.add_acme(robots="User-agent: *\nDisallow: /\n")
    cid = add_company(conn, "Acme Precision", HOST)
    llm = FakeLLMBackend()
    run_enrich(conn, site_server, llm, clock)
    assert row(conn, cid)["status"] == "blocked_by_robots"
    assert llm.calls == []


def test_ollama_not_running_still_fetches_and_later_run_completes(conn, site_server, clock):
    site_server.add_acme()
    cid = add_company(conn, "Acme Precision", HOST)
    down = FakeLLMBackend(available=False)
    stats, _ = run_enrich(conn, site_server, down, clock)
    assert stats["statuses"] == {"llm_unavailable": 1}
    assert "not reachable" in stats["llm_problem"]
    assert row(conn, cid)["text_sha256"] is not None and down.calls == []

    n_requests = len(site_server.requests)
    up = FakeLLMBackend([extraction_json()])
    stats, _ = run_enrich(conn, site_server, up, clock)
    assert stats["statuses"] == {"ok": 1}
    assert len(site_server.requests) == n_requests  # pages came from the cache


def test_ollama_dying_mid_run_skips_remaining_llm_calls(conn, site_server, clock):
    site_server.add_acme()
    site_server.add_acme(host="bluegrass-tooling.test")
    a = add_company(conn, "Acme Precision", HOST)
    b = add_company(conn, "Bluegrass Tooling", "bluegrass-tooling.test")
    llm = FakeLLMBackend([LLMUnavailable("connection refused")])
    stats, _ = run_enrich(conn, site_server, llm, clock)
    assert (
        row(conn, a)["status"] == "llm_unavailable" and row(conn, b)["status"] == "llm_unavailable"
    )
    assert len(llm.calls) == 1


def test_failures_do_not_stop_the_run(conn, site_server, clock, monkeypatch):
    site_server.add_acme()
    site_server.add_acme(host="slow-site.test")
    site_server.add_error("slow-site.test", "/", httpx.ReadTimeout("slow"))
    site_server.add_error("www.slow-site.test", "/", httpx.ReadTimeout("slow"))
    site_server.add_error("slow-site.test", "/robots.txt", httpx.ReadTimeout("slow"))
    site_server.add_acme(host="weird.test")
    site_server.add_acme(host="garbage.test")
    ok = add_company(conn, "Acme Precision", HOST)
    slow = add_company(conn, "Slow Site Co", "slow-site.test")
    boom = add_company(conn, "Weird Co", "weird.test")
    garbage = add_company(conn, "Garbage Co", "garbage.test")

    real_page_text = pipeline.page_text

    def exploding_page_text(url, html):
        if "weird.test" in url:
            raise RuntimeError("unexpected parser failure")
        return real_page_text(url, html)

    monkeypatch.setattr(pipeline, "page_text", exploding_page_text)
    llm = FakeLLMBackend([extraction_json(), "not json", "still not json", LLMTimeout("slow")])
    stats, _ = run_enrich(conn, site_server, llm, clock)
    assert row(conn, ok)["status"] == "ok"
    assert (
        row(conn, slow)["status"] == "blocked_by_robots"
    )  # unreachable robots.txt means do not crawl
    assert row(conn, boom)["status"] == "error" and row(conn, boom)["detail"] == "RuntimeError"
    assert row(conn, garbage)["status"] == "llm_invalid_output"
    assert stats["companies"] == 4


def test_llm_timeout_is_recorded(conn, site_server, clock):
    site_server.add_acme()
    cid = add_company(conn, "Acme Precision", HOST)
    run_enrich(
        conn, site_server, FakeLLMBackend([LLMTimeout("Ollama did not answer within 180s")]), clock
    )
    r = row(conn, cid)
    assert r["status"] == "llm_timeout" and "180s" in r["detail"]


def test_limit_and_company_ids(conn, site_server, clock):
    site_server.add_acme()
    a = add_company(conn, "Acme Precision", HOST)
    add_company(conn, "No Site Co")
    stats, _ = run_enrich(
        conn, site_server, FakeLLMBackend([extraction_json()]), clock, company_ids=[a]
    )
    assert stats["companies"] == 1
    stats, _ = run_enrich(conn, site_server, FakeLLMBackend(), clock, limit=1)
    assert stats["companies"] == 1


# --- CLI -------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(split_ready, site_server, clock, monkeypatch):
    monkeypatch.setenv("DEALSOURCE_USER_AGENT_CONTACT", "https://example.org/contact")
    monkeypatch.setattr(cli, "make_http_client", site_server.client)
    monkeypatch.setattr(cli, "make_clock", lambda: clock)
    site_server.add_acme()
    return split_ready


def test_cli_enrich_prints_aggregates_only(cli_env, conn, monkeypatch, site_server):
    add_company(conn, "Acme Precision", HOST)
    add_company(conn, "Oakmont Valve Service")
    monkeypatch.setattr(cli, "make_llm_backend", lambda s: FakeLLMBackend([extraction_json()]))
    result = runner.invoke(cli.app, ["enrich"])
    assert result.exit_code == 0, result.output
    assert "Enriched 2 companies: no_website 1, ok 1" in result.output
    assert "LLM tokens: 1,000 in, 200 out" in result.output
    assert "Time per company" in result.output
    for leak in ("Acme", "Oakmont", HOST):
        assert leak not in result.output
    ua = {r.headers["user-agent"] for r in site_server.requests}
    assert ua == {"dealsource/0.1.0 (+https://example.org/contact)"}

    stats = runner.invoke(cli.app, ["stats"])
    assert "enrich ok" in stats.output and "tokens per company" in stats.output


def test_cli_enrich_requires_user_agent_contact(split_ready, monkeypatch):
    monkeypatch.delenv("DEALSOURCE_USER_AGENT_CONTACT", raising=False)
    result = runner.invoke(cli.app, ["enrich"])
    assert result.exit_code == 1 and "DEALSOURCE_USER_AGENT_CONTACT" in result.output


def test_cli_enrich_refuses_remote_ollama(cli_env, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box.example.com:11434")
    result = runner.invoke(cli.app, ["enrich"])
    assert result.exit_code == 1 and "inference must stay local" in result.output


def test_cli_enrich_with_ollama_down_does_not_crash(cli_env, conn, monkeypatch):
    add_company(conn, "Acme Precision", HOST)
    monkeypatch.setattr(cli, "make_llm_backend", lambda s: FakeLLMBackend(available=False))
    result = runner.invoke(cli.app, ["enrich"])
    assert result.exit_code == 0, result.output
    assert "Warning: LLM unavailable" in result.output
    assert "llm_unavailable 1" in result.output


def test_cli_enrich_is_gated_on_labels_split(settings, monkeypatch):
    monkeypatch.setenv("DEALSOURCE_USER_AGENT_CONTACT", "https://example.org/contact")
    assert runner.invoke(cli.app, ["enrich"]).exit_code == 2


def test_hallucinated_numbers_are_dropped_before_storage(conn, site_server, clock):
    site_server.add_acme()
    cid = add_company(conn, "Acme Precision", HOST)
    invented = extraction_json(
        size_signals={
            "employee_count": 1000,
            "employee_count_quote": "tight-tolerance components",
            "facility_count": 2,
            "facility_sqft_total": 60000,
            "founded_year": 1962,
        },
        evidence=[{"claim": "publicly_traded", "page": "/", "quote": "Listed on NASDAQ as ACME"}],
    )
    run_enrich(conn, site_server, FakeLLMBackend([invented]), clock)
    r = row(conn, cid)
    data = json.loads(r["extraction_json"])
    assert data["size_signals"]["employee_count"] is None
    assert (
        data["size_signals"]["facility_sqft_total"] == 60000
    )  # "60,000 square feet" is on /about-us
    assert data["evidence"] == []
    assert (
        r["status"] == "ok"
        and r["detail"] == "unsupported by page text: evidence:1, employee_count"
    )
