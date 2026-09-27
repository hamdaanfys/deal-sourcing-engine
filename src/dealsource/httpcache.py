"""HTTP GET through a SQLite-backed cache, so reruns never repeat a request.

Used by data sources (Census) now and by the website fetcher later. Secrets such as API keys
are sent with the request but never written into the cache key or the stored URL.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from dealsource.db import utcnow

SECRET_PARAMS = frozenset({"key", "api_key", "apikey", "token"})


@dataclass(frozen=True)
class CachedResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool

    def json(self):
        return json.loads(self.body)


def cache_url(url: str, params: dict[str, str] | None) -> str:
    """Canonical cache key: URL plus sorted params, with secret params removed."""
    public = sorted((k, v) for k, v in (params or {}).items() if k.lower() not in SECRET_PARAMS)
    return f"{url}?{urlencode(public)}" if public else url


def is_cacheable(status: int) -> bool:
    # 204 is how the Census API says "no rows"; 404 is a stable answer too. Redirects,
    # other client errors and server errors are not cached, so a fixed key or a retry works.
    return 200 <= status < 300 or status == 404


class CachedHttp:
    def __init__(self, conn: sqlite3.Connection, client: httpx.Client, user_agent: str):
        self.conn = conn
        self.client = client
        self.user_agent = user_agent

    def get(
        self, url: str, params: dict[str, str] | None = None, *, refresh: bool = False
    ) -> CachedResponse:
        key = cache_url(url, params)
        if not refresh:
            row = self.conn.execute(
                "SELECT status, headers_json, body FROM http_cache WHERE url = ?", (key,)
            ).fetchone()
            if row is not None:
                body = zlib.decompress(row["body"]) if row["body"] is not None else b""
                return CachedResponse(
                    key, row["status"], json.loads(row["headers_json"]), body, True
                )

        resp = self.client.get(
            url, params=params, headers={"User-Agent": self.user_agent}, follow_redirects=False
        )
        headers = {
            k.lower(): v
            for k, v in resp.headers.items()
            if k.lower() in {"content-type", "location", "etag", "last-modified"}
        }
        body = resp.content
        if is_cacheable(resp.status_code):
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
                        resp.status_code,
                        json.dumps(headers, sort_keys=True),
                        zlib.compress(body),
                        hashlib.sha256(body).hexdigest(),
                        utcnow(),
                    ),
                )
        return CachedResponse(key, resp.status_code, headers, body, False)
