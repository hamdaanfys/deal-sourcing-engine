"""Polite website fetching (DESIGN.md §8.1).

- robots.txt is checked for our user agent before every request (cached for 24 h). Following
  RFC 9309: a 4xx robots.txt allows everything; a 5xx or unreachable one disallows everything.
- One request at a time per host, at least ``min_delay`` seconds apart, or the site's
  Crawl-delay if longer. Sites asking for more than MAX_CRAWL_DELAY are skipped.
- 429/503 are retried with backoff, honouring Retry-After (capped).
- Redirects are followed only within the company's own registrable domain.
- Only HTML is kept, bodies over ``max_bytes`` are rejected, and every answer is cached, so a
  rerun makes no requests at all.
"""

from __future__ import annotations

import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from dealsource.clock import Clock, SystemClock
from dealsource.httpcache import CachedHttp, CachedResponse
from dealsource.resolve.normalize import domain_key

ROBOTS_TOKEN = "dealsource"
ROBOTS_TTL_SECONDS = 24 * 3600
MAX_CRAWL_DELAY = 30.0
MAX_RETRY_AFTER = 60.0
MAX_REDIRECTS = 5

# Statuses for a single page fetch.
OK = "ok"
BLOCKED_BY_ROBOTS = "blocked_by_robots"
CRAWL_DELAY_TOO_LONG = "crawl_delay_too_long"
OFFSITE_REDIRECT = "offsite_redirect"
HTTP_ERROR = "http_error"
NOT_HTML = "not_html"
TOO_LARGE = "too_large"
TIMEOUT = "timeout"
CONNECTION_ERROR = "connection_error"


def is_cacheable_page(status: int) -> bool:
    """Website answers worth caching: content, redirects, and permanent 'not there' answers."""
    return 200 <= status < 400 or status in (401, 403, 404, 410)


@dataclass(frozen=True)
class PageResult:
    url: str  # the URL asked for
    final_url: str  # after same-site redirects
    status: str
    http_status: int | None = None
    html: str | None = None
    from_cache: bool = False
    detail: str | None = None


class PoliteFetcher:
    def __init__(
        self,
        http: CachedHttp,
        *,
        clock: Clock | None = None,
        min_delay: float = 2.0,
        max_retries: int = 2,
        max_bytes: int = 2_000_000,
        timeout: float = 15.0,
        page_max_age_seconds: float | None = None,
    ):
        self.http = http
        self.clock = clock or SystemClock()
        self.min_delay = min_delay
        self.max_retries = max_retries
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.page_max_age = page_max_age_seconds
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.requests_made = 0
        self.cache_hits = 0

    # -- rate limiting ------------------------------------------------------------------

    def _delay_for(self, host: str) -> float:
        rp = self._robots.get(host)
        crawl_delay = rp.crawl_delay(ROBOTS_TOKEN) if rp else None
        return max(self.min_delay, float(crawl_delay or 0))

    def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = last + self._delay_for(host) - self.clock.monotonic()
            if wait > 0:
                self.clock.sleep(wait)
        self._last_request[host] = self.clock.monotonic()

    def _request(self, url: str) -> CachedResponse:
        """One network request with politeness delay and 429/503 backoff."""
        host = urlsplit(url).netloc
        attempt = 0
        while True:
            self._wait_turn(host)
            self.requests_made += 1
            resp = self.http.fetch(url, timeout=self.timeout, max_bytes=self.max_bytes)
            if resp.status not in (429, 503) or attempt >= self.max_retries:
                return resp
            attempt += 1
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            backoff = retry_after if retry_after is not None else self.min_delay * 2**attempt
            self.clock.sleep(min(backoff, MAX_RETRY_AFTER))

    def _get(self, url: str, max_age: float | None) -> CachedResponse:
        hit = self.http.lookup(url, max_age_seconds=max_age)
        if hit is not None:
            self.cache_hits += 1
            return hit
        return self._request(url)

    # -- robots.txt -----------------------------------------------------------------------

    def robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urlsplit(url)
        host = parts.netloc
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{parts.scheme}://{host}/robots.txt"
        try:
            resp = self._get(robots_url, ROBOTS_TTL_SECONDS)
            hops = 0
            while (
                300 <= resp.status < 400 and resp.headers.get("location") and hops < MAX_REDIRECTS
            ):
                resp = self._get(urljoin(robots_url, resp.headers["location"]), ROBOTS_TTL_SECONDS)
                hops += 1
            if 200 <= resp.status < 300:
                rp.parse(resp.body.decode("utf-8", "replace").splitlines())
            elif 400 <= resp.status < 500:
                rp.allow_all = True
            else:
                rp.disallow_all = True
        except httpx.HTTPError:
            rp.disallow_all = True  # unreachable: assume we may not crawl
        self._robots[host] = rp
        return rp

    # -- pages ----------------------------------------------------------------------------

    def fetch_page(self, url: str, site_domain: str) -> PageResult:
        current = url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                rp = self.robots_for(current)
                if not rp.can_fetch(ROBOTS_TOKEN, current):
                    return PageResult(url, current, BLOCKED_BY_ROBOTS)
                if self._delay_for(urlsplit(current).netloc) > MAX_CRAWL_DELAY:
                    return PageResult(url, current, CRAWL_DELAY_TOO_LONG)
                resp = self._get(current, self.page_max_age)
                if 300 <= resp.status < 400 and resp.headers.get("location"):
                    target = urljoin(current, resp.headers["location"])
                    if domain_key(target) != site_domain:
                        return PageResult(
                            url, current, OFFSITE_REDIRECT, resp.status, detail=target
                        )
                    current = target
                    continue
                return self._page_result(url, current, resp)
            return PageResult(url, current, HTTP_ERROR, detail="too many redirects")
        except httpx.TimeoutException:
            return PageResult(url, current, TIMEOUT)
        except httpx.HTTPError as exc:
            return PageResult(url, current, CONNECTION_ERROR, detail=type(exc).__name__)

    @staticmethod
    def _page_result(url: str, final: str, resp: CachedResponse) -> PageResult:
        if resp.status != 200:
            return PageResult(url, final, HTTP_ERROR, resp.status, from_cache=resp.from_cache)
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype:
            return PageResult(url, final, NOT_HTML, resp.status, from_cache=resp.from_cache)
        if resp.truncated:
            return PageResult(url, final, TOO_LARGE, resp.status, from_cache=resp.from_cache)
        html = resp.body.decode(_charset(ctype), "replace")
        return PageResult(url, final, OK, resp.status, html, resp.from_cache)


def _charset(content_type: str) -> str:
    for part in content_type.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "charset" and value:
            return value.strip("\"'")
    return "utf-8"


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None
