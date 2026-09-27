"""Cached, validated structured extraction with per-call metrics.

Every call (including cache hits) writes an ``llm_calls`` row with tokens and latency. Only
responses that validate against the schema are cached. Invalid JSON gets one retry with a
"fix it" nudge; after that the failure is returned, not raised.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ValidationError

from dealsource.db import utcnow
from dealsource.enrich.prompts import FIX_PROMPT
from dealsource.llm.base import GenOptions, LLMBackend, LLMError, LLMTimeout, LLMUnavailable

# Outcome statuses
OK = "ok"
LLM_UNAVAILABLE = "llm_unavailable"
LLM_TIMEOUT = "llm_timeout"
LLM_INVALID_OUTPUT = "llm_invalid_output"
LLM_ERROR = "llm_error"


@dataclass
class ExtractOutcome:
    status: str
    data: dict | None
    cache_key: str
    cache_hit: bool
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    calls: int
    error: str | None = None


def cache_key(
    backend: LLMBackend, prompt_version: str, schema_hash: str, options: GenOptions, messages
) -> str:
    material = {
        "backend": backend.name,
        "model": backend.model,
        "prompt_version": prompt_version,
        "schema": schema_hash,
        "options": {k: v for k, v in asdict(options).items() if k != "timeout_seconds"},
        "messages": messages,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _error_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return f"{exc.error_count()} validation error(s), first at {loc or 'root'}: {first.get('type')}"
    return type(exc).__name__


class LLMRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        backend: LLMBackend,
        *,
        prompt_version: str,
        options: GenOptions | None = None,
        max_attempts: int = 2,
    ):
        self.conn = conn
        self.backend = backend
        self.prompt_version = prompt_version
        self.options = options or GenOptions()
        self.max_attempts = max_attempts

    def _record(
        self,
        company_id,
        stage,
        *,
        prompt_tokens,
        completion_tokens,
        latency_ms,
        cache_hit,
        ok,
        error=None,
    ):
        with self.conn:
            self.conn.execute(
                """INSERT INTO llm_calls (company_id, stage, backend, model, prompt_tokens, completion_tokens,
                     latency_ms, cache_hit, ok, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company_id,
                    stage,
                    self.backend.name,
                    self.backend.model,
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                    int(cache_hit),
                    int(ok),
                    error,
                    utcnow(),
                ),
            )

    def extract(
        self,
        *,
        company_id: int | None,
        stage: str,
        system: str,
        user: str,
        model_cls: type[BaseModel],
        schema_hash: str,
    ) -> ExtractOutcome:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = cache_key(self.backend, self.prompt_version, schema_hash, self.options, messages)

        row = self.conn.execute(
            "SELECT response_json, prompt_tokens, completion_tokens FROM llm_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is not None:
            try:
                data = model_cls.model_validate_json(row["response_json"]).model_dump()
            except ValidationError:
                data = None  # schema changed without a version bump: fall through and re-ask
            if data is not None:
                self._record(
                    company_id,
                    stage,
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    latency_ms=0.0,
                    cache_hit=True,
                    ok=True,
                )
                return ExtractOutcome(
                    OK,
                    data,
                    key,
                    True,
                    row["prompt_tokens"] or 0,
                    row["completion_tokens"] or 0,
                    0.0,
                    0,
                )

        schema = model_cls.model_json_schema()
        total_in = total_out = 0
        total_ms = 0.0
        calls = 0
        convo = list(messages)
        last_error = None
        for _ in range(self.max_attempts):
            calls += 1
            try:
                result = self.backend.chat(convo, schema, self.options)
            except LLMError as exc:
                status = (
                    LLM_UNAVAILABLE
                    if isinstance(exc, LLMUnavailable)
                    else LLM_TIMEOUT
                    if isinstance(exc, LLMTimeout)
                    else LLM_ERROR
                )
                self._record(
                    company_id,
                    stage,
                    prompt_tokens=None,
                    completion_tokens=None,
                    latency_ms=None,
                    cache_hit=False,
                    ok=False,
                    error=f"{status}: {exc}",
                )
                return ExtractOutcome(
                    status, None, key, False, total_in, total_out, total_ms, calls, str(exc)
                )
            total_in += result.prompt_tokens or 0
            total_out += result.completion_tokens or 0
            total_ms += result.latency_ms
            try:
                parsed = model_cls.model_validate_json(result.content)
            except (ValidationError, ValueError) as exc:
                last_error = _error_summary(exc)
                self._record(
                    company_id,
                    stage,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_ms=result.latency_ms,
                    cache_hit=False,
                    ok=False,
                    error=f"{LLM_INVALID_OUTPUT}: {last_error}",
                )
                convo = [
                    *convo,
                    {"role": "assistant", "content": result.content},
                    {"role": "user", "content": FIX_PROMPT.format(error=last_error)},
                ]
                continue
            self._record(
                company_id,
                stage,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                cache_hit=False,
                ok=True,
            )
            with self.conn:
                self.conn.execute(
                    """INSERT OR REPLACE INTO llm_cache (cache_key, backend, model, prompt_version, schema_hash,
                         response_json, prompt_tokens, completion_tokens, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        self.backend.name,
                        self.backend.model,
                        self.prompt_version,
                        schema_hash,
                        parsed.model_dump_json(),
                        total_in,
                        total_out,
                        utcnow(),
                    ),
                )
            return ExtractOutcome(
                OK, parsed.model_dump(), key, False, total_in, total_out, total_ms, calls
            )
        return ExtractOutcome(
            LLM_INVALID_OUTPUT, None, key, False, total_in, total_out, total_ms, calls, last_error
        )
