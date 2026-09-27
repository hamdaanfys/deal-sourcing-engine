"""Ollama backend: structured output via /api/chat with a JSON schema in ``format``.

Refuses any host that is not this machine unless explicitly allowed, so company data never
leaves the machine by accident.
"""

from __future__ import annotations

import ipaddress
import time
from urllib.parse import urlsplit

import httpx

from dealsource.llm.base import GenOptions, LLMResponseError, LLMResult, LLMTimeout, LLMUnavailable


class RemoteHostRefused(ValueError):
    pass


def is_loopback_host(url: str) -> bool:
    """True for localhost / loopback IPs. Hostnames are not resolved (no DNS lookups)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaBackend:
    name = "ollama"

    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:7b",
        *,
        client: httpx.Client | None = None,
        allow_remote: bool = False,
    ):
        if not is_loopback_host(host) and not allow_remote:
            raise RemoteHostRefused(
                f"OLLAMA_HOST {host!r} is not this machine; inference must stay local. "
                "Set DEALSOURCE_ALLOW_REMOTE_LLM=1 only if you are sure."
            )
        self.host = host.rstrip("/")
        self.model = model
        self.client = client or httpx.Client()

    def check(self) -> None:
        try:
            resp = self.client.get(f"{self.host}/api/tags", timeout=5.0)
            resp.raise_for_status()
            names = {m.get("name") for m in resp.json().get("models", [])}
        except httpx.HTTPError as exc:
            raise LLMUnavailable(
                f"Ollama is not reachable at {self.host} ({type(exc).__name__}); is it running?"
            ) from exc
        except ValueError as exc:
            raise LLMUnavailable(f"Unexpected reply from Ollama at {self.host}") from exc
        if self.model not in names and f"{self.model}:latest" not in names:
            raise LLMUnavailable(
                f"Model {self.model!r} is not installed; run: ollama pull {self.model}"
            )

    def chat(self, messages: list[dict[str, str]], schema: dict, options: GenOptions) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": {
                "temperature": options.temperature,
                "seed": options.seed,
                "num_ctx": options.num_ctx,
            },
        }
        start = time.perf_counter()
        try:
            resp = self.client.post(
                f"{self.host}/api/chat", json=payload, timeout=options.timeout_seconds
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeout(
                f"Ollama did not answer within {options.timeout_seconds:.0f}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(
                f"Ollama is not reachable at {self.host} ({type(exc).__name__}); is it running?"
            ) from exc
        latency_ms = (time.perf_counter() - start) * 1000
        if resp.status_code == 404:
            raise LLMUnavailable(
                f"Model {self.model!r} is not installed; run: ollama pull {self.model}"
            )
        if resp.status_code != 200:
            raise LLMResponseError(f"Ollama returned HTTP {resp.status_code}")
        try:
            data = resp.json()
            content = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMResponseError("Ollama reply had no message content") from exc
        return LLMResult(
            content=content,
            model=data.get("model", self.model),
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            latency_ms=latency_ms,
        )
