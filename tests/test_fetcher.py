"""Polite fetching: robots.txt, rate limits, redirects, limits, caching. No network."""

import httpx
import pytest

from dealsource.enrich import fetcher as f
from dealsource.enrich.fetcher import PoliteFetcher, is_cacheable_page
from dealsource.httpcache import CachedHttp

HOST = "acme-precision.test"
BASE = f"https://{HOST}"
UA = "dealsource/0.1.0 (+https://example.org/contact)"


def make_fetcher(conn, server, clock, **kw):
    http = CachedHttp(conn, server.client(), UA, cacheable=is_cacheable_page)
    return PoliteFetcher(http, clock=clock, **kw)


def test_sends_identifying_user_agent(conn, site_server, clock):
    site_server.add_acme()
    assert make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST).status == f.OK
    assert {r.headers["user-agent"] for r in site_server.requests} == {UA}


def test_robots_disallow_blocks_without_requesting_the_page(conn, site_server, clock):
    site_server.add_acme()
    result = make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/private/pricing", HOST)
    assert result.status == f.BLOCKED_BY_ROBOTS
    assert site_server.paths_requested() == ["/robots.txt"]


def test_robots_rules_for_our_user_agent_apply(conn, site_server, clock):
    site_server.add_acme(robots="User-agent: dealsource\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert (
        make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST).status
        == f.BLOCKED_BY_ROBOTS
    )


@pytest.mark.parametrize(
    ("robots_status", "expected"), [(404, f.OK), (403, f.OK), (500, f.BLOCKED_BY_ROBOTS)]
)
def test_robots_status_codes_follow_rfc_9309(conn, site_server, clock, robots_status, expected):
    site_server.add_acme()
    site_server.add(HOST, "/robots.txt", status=robots_status)
    assert make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST).status == expected


def test_unreachable_robots_means_do_not_crawl(conn, site_server, clock):
    site_server.add_acme()
    site_server.add_error(HOST, "/robots.txt", httpx.ConnectError("refused"))
    assert (
        make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST).status
        == f.BLOCKED_BY_ROBOTS
    )


def test_requests_to_one_host_are_spaced_by_min_delay(conn, site_server, clock):
    site_server.add_acme()
    fetcher = make_fetcher(conn, site_server, clock, min_delay=2.0)
    fetcher.fetch_page(f"{BASE}/", HOST)
    fetcher.fetch_page(f"{BASE}/about-us", HOST)
    # robots.txt, then / 2 s later, then /about-us 2 s after that
    assert clock.sleeps == [2.0, 2.0]


def test_crawl_delay_is_honoured(conn, site_server, clock):
    site_server.add_acme(robots="User-agent: *\nCrawl-delay: 7\n")
    fetcher = make_fetcher(conn, site_server, clock, min_delay=2.0)
    fetcher.fetch_page(f"{BASE}/", HOST)
    fetcher.fetch_page(f"{BASE}/about-us", HOST)
    assert clock.sleeps == [7.0, 7.0]


def test_excessive_crawl_delay_skips_the_site(conn, site_server, clock):
    site_server.add_acme(robots="User-agent: *\nCrawl-delay: 3600\n")
    result = make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST)
    assert result.status == f.CRAWL_DELAY_TOO_LONG
    assert site_server.paths_requested() == ["/robots.txt"]


def test_429_is_retried_after_retry_after_and_not_cached(conn, site_server, clock):
    site_server.add_acme()
    calls = {"n": 0}
    original = site_server.handler

    def flaky(request):
        if request.url.path == "/about-us" and calls["n"] == 0:
            calls["n"] += 1
            site_server.requests.append(request)
            return httpx.Response(429, headers={"retry-after": "5"})
        return original(request)

    http = CachedHttp(
        conn, httpx.Client(transport=httpx.MockTransport(flaky)), UA, cacheable=is_cacheable_page
    )
    fetcher = PoliteFetcher(http, clock=clock, min_delay=2.0)
    result = fetcher.fetch_page(f"{BASE}/about-us", HOST)
    assert result.status == f.OK
    assert 5.0 in clock.sleeps
    assert site_server.paths_requested().count("/about-us") == 2


def test_persistent_503_gives_up_as_http_error(conn, site_server, clock):
    site_server.add_acme()
    site_server.add(HOST, "/", status=503)
    result = make_fetcher(conn, site_server, clock, max_retries=2).fetch_page(f"{BASE}/", HOST)
    assert result.status == f.HTTP_ERROR and result.http_status == 503
    assert site_server.paths_requested().count("/") == 3


def test_same_site_redirect_is_followed(conn, site_server, clock):
    site_server.add_acme(host=f"www.{HOST}")
    site_server.add(HOST, "/robots.txt", status=404)
    site_server.add(HOST, "/", status=301, headers={"location": f"https://www.{HOST}/"})
    result = make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST)
    assert result.status == f.OK
    assert result.final_url == f"https://www.{HOST}/"


def test_offsite_redirect_is_recorded_not_followed(conn, site_server, clock):
    site_server.add_acme()
    site_server.add(HOST, "/", status=301, headers={"location": "https://new-owner.test/acme"})
    result = make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/", HOST)
    assert result.status == f.OFFSITE_REDIRECT
    assert result.detail == "https://new-owner.test/acme"
    assert not any(r.url.host == "new-owner.test" for r in site_server.requests)


def test_timeouts_and_connection_errors_are_statuses_not_exceptions(conn, site_server, clock):
    site_server.add_acme()
    site_server.add_error(HOST, "/", httpx.ReadTimeout("slow"))
    site_server.add_error(HOST, "/about-us", httpx.ConnectError("refused"))
    fetcher = make_fetcher(conn, site_server, clock)
    assert fetcher.fetch_page(f"{BASE}/", HOST).status == f.TIMEOUT
    result = fetcher.fetch_page(f"{BASE}/about-us", HOST)
    assert result.status == f.CONNECTION_ERROR and result.detail == "ConnectError"


def test_non_html_and_oversized_pages_are_rejected(conn, site_server, clock):
    site_server.add_acme()
    site_server.add(
        HOST, "/products", body=b"%PDF-1.7", headers={"content-type": "application/pdf"}
    )
    site_server.add(HOST, "/about-us", body=b"<p>" + b"x" * 5000 + b"</p>")
    fetcher = make_fetcher(conn, site_server, clock, max_bytes=1000)
    assert fetcher.fetch_page(f"{BASE}/products", HOST).status == f.NOT_HTML
    assert fetcher.fetch_page(f"{BASE}/about-us", HOST).status == f.TOO_LARGE


def test_rerun_is_served_entirely_from_cache(conn, site_server, clock):
    site_server.add_acme()
    make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/about-us", HOST)
    n = len(site_server.requests)
    again = make_fetcher(conn, site_server, clock)
    result = again.fetch_page(f"{BASE}/about-us", HOST)
    assert result.status == f.OK and result.from_cache
    assert len(site_server.requests) == n and again.requests_made == 0


def test_stale_pages_are_refetched_when_asked(conn, site_server, clock):
    site_server.add_acme()
    make_fetcher(conn, site_server, clock).fetch_page(f"{BASE}/about-us", HOST)
    conn.execute("UPDATE http_cache SET fetched_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    fresh = make_fetcher(conn, site_server, clock, page_max_age_seconds=30 * 86400)
    assert fresh.fetch_page(f"{BASE}/about-us", HOST).from_cache is False
