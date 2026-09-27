"""LLM backend interface and error types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GenOptions:
    temperature: float = 0.0
    seed: int = 0
    num_ctx: int = 8192
    timeout_seconds: float = 180.0


@dataclass(frozen=True)
class LLMResult:
    content: str  # raw text of the reply (expected to be JSON)
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float


class LLMError(Exception):
    """Base class; the message never contains company text."""


class LLMUnavailable(LLMError):
    """The backend is not running, unreachable, or the model is not installed."""


class LLMTimeout(LLMError):
    pass


class LLMResponseError(LLMError):
    """The backend answered with an error or an unreadable response."""


class LLMBackend(Protocol):
    name: str
    model: str

    def check(self) -> None:
        """Raise LLMUnavailable if the backend cannot serve requests."""

    def chat(
        self, messages: list[dict[str, str]], schema: dict, options: GenOptions
    ) -> LLMResult: ...
