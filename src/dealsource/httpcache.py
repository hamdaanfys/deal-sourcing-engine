"""HTTP GET through a SQLite-backed cache, so reruns never repeat a request.

Used by data sources (Census) and the website fetcher. ``lookup`` never touches the network,
which lets the fetcher apply robots.txt and rate limits only to real requests. Secrets such as
API keys are sent with the request but never written into the cache key or the stored URL.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from dealsource.db import utcnow

SECRET_PARAMS = frozenset({"key", "api_key", "apikey", "token"})
KEPT_HEADERS = frozenset(
    {"content-type", "location", "etag", "last-modified", "retry-after", "content-length"}
)
TRUNCATED_HEADER = "x-dealsource-truncated"


@dataclass(frozen=True)
class CachedResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool

    def json(self):
        return json.loads(self.body)

    @property
    def truncated(self) -> bool:
        return self.headers.get(TRUNCATED_HEADER) == "1"


def cache_url(url: str, params: dict[str, str] | None) -> str:
    """Canonical cache key: URL plus sorted params, with secret params removed."""
    public = sorted((k, v) for k, v in (params or {}).items() if k.lower() not in SECRET_PARAMS)
    return f"{url}?{urlencode(public)}" if public else url


def is_cacheable(status: int) -> bool:
    # 204 is how the Census API says "no rows"; 404 is a stable answer too. Redirects,
    # other client errors and server errors are not cached, so a fixed key or a retry works.
    return 200 <= status < 300 or status == 404


class CachedHttp:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: httpx.Client,
        user_agent: str,
        *,
        cacheable: Callable[[int], bool] = is_cacheable,
    ):
        self.conn = conn
        self.client = client
        self.user_agent = user_agent
        self.cacheable = cacheable

    def lookup(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        max_age_seconds: float | None = None,
    ) -> CachedResponse | None:
        key = cache_url(url, params)
        row = self.conn.execute(
            "SELECT status, headers_json, body, fetched_at FROM http_cache WHERE url = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if max_age_seconds is not None:
            age = (datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            if age > max_age_seconds:
                return None
        body = zlib.decompress(row["body"]) if row["body"] is not None else b""
        return CachedResponse(key, row["status"], json.loads(row["headers_json"]), body, True)

    def fetch(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> CachedResponse:
        """Make the request (no redirects followed) and cache the response if cacheable."""
        key = cache_url(url, params)
        kwargs = {
            "params": params,
            "headers": {"User-Agent": self.user_agent},
            "follow_redirects": False,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        with self.client.stream("GET", url, **kwargs) as resp:
            chunks, size, truncated = [], 0, False
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    truncated = True
                    break
            body = b"".join(chunks)
            if max_bytes is not None:
                body = body[:max_bytes]
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in KEPT_HEADERS}
            if truncated:
                headers[TRUNCATED_HEADER] = "1"
            status = resp.status_code
        if self.cacheable(status):
            with self.conn:
                self.conn.execute(
                    """INSERT INTO http_cache (url, final_url, status, headers_json, body, content_hash, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url, status=excluded.status,
                         headers_json=excluded.headers_json, body=excluded.body,
                         content_hash=excluded.content_hash, fetched_at=excluded.fetched_at""",
                    (
                        key,
                        key,
                        status,
                        json.dumps(headers, sort_keys=True),
                        zlib.compress(body),
                        hashlib.sha256(body).hexdigest(),
                        utcnow(),
                    ),
                )
        return CachedResponse(key, status, headers, body, False)

    @staticmethod
    def post_key(url: str, body: dict) -> str:
        payload = json.dumps(body, sort_keys=True)
        return f"{url}#post:{hashlib.sha256(payload.encode()).hexdigest()}"

    def post_json(self, url: str, body: dict, *, refresh: bool = False) -> CachedResponse:
        """POST a JSON body, cached by URL + a hash of the body (for read-only search APIs)."""
        payload = json.dumps(body, sort_keys=True)
        key = self.post_key(url, body)
        if not refresh:
            hit = self.lookup(key)
            if hit is not None:
                return hit
        resp = self.client.post(
            url,
            content=payload,
            headers={"User-Agent": self.user_agent, "Content-Type": "application/json"},
        )
        headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in KEPT_HEADERS}
        if self.cacheable(resp.status_code):
            with self.conn:
                self.conn.execute(
                    """INSERT INTO http_cache (url, final_url, status, headers_json, body, content_hash, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(url) DO UPDATE SET status=excluded.status, headers_json=excluded.headers_json,
                         body=excluded.body, content_hash=excluded.content_hash, fetched_at=excluded.fetched_at""",
                    (
                        key,
                        url,
                        resp.status_code,
                        json.dumps(headers, sort_keys=True),
                        zlib.compress(resp.content),
                        hashlib.sha256(resp.content).hexdigest(),
                        utcnow(),
                    ),
                )
        return CachedResponse(key, resp.status_code, headers, resp.content, False)

    def get(
        self, url: str, params: dict[str, str] | None = None, *, refresh: bool = False
    ) -> CachedResponse:
        if not refresh:
            hit = self.lookup(url, params)
            if hit is not None:
                return hit
        return self.fetch(url, params)
