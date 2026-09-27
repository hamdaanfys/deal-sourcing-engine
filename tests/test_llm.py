"""Ollama backend (recorded replies via MockTransport) and the cached, validated runner."""

import json

import httpx
import pytest
from conftest import OLLAMA, FakeLLMBackend, extraction_json

from dealsource.enrich.schema import Extraction, schema_hash
from dealsource.llm import cache as c
from dealsource.llm.base import GenOptions, LLMResponseError, LLMTimeout, LLMUnavailable
from dealsource.llm.cache import LLMRunner
from dealsource.llm.ollama import OllamaBackend, RemoteHostRefused, is_loopback_host


def ollama_with(handler) -> OllamaBackend:
    return OllamaBackend(
        "http://127.0.0.1:11434",
        "qwen2.5:7b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# --- Ollama backend -----------------------------------------------------------------------


def test_chat_sends_schema_and_parses_tokens():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=(OLLAMA / "chat_ok.json").read_bytes())

    result = ollama_with(handler).chat(
        [{"role": "user", "content": "hi"}], Extraction.model_json_schema(), GenOptions()
    )
    assert seen["model"] == "qwen2.5:7b" and seen["stream"] is False
    assert seen["format"]["title"] == "Extraction"
    assert seen["options"] == {"temperature": 0.0, "seed": 0, "num_ctx": 8192}
    assert (result.prompt_tokens, result.completion_tokens) == (1234, 187)
    assert Extraction.model_validate_json(result.content).size_signals.employee_count == 85
    assert result.latency_ms >= 0


def test_check_reports_ollama_not_running():
    def refused(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMUnavailable, match="not reachable"):
        ollama_with(refused).check()


def test_check_reports_missing_model():
    backend = OllamaBackend(
        "http://localhost:11434",
        "llama3:70b",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=(OLLAMA / "tags.json").read_bytes())
            )
        ),
    )
    with pytest.raises(LLMUnavailable, match="ollama pull llama3:70b"):
        backend.check()
    ollama_with(lambda r: httpx.Response(200, content=(OLLAMA / "tags.json").read_bytes())).check()


@pytest.mark.parametrize(
    ("behaviour", "error"),
    [
        (httpx.ConnectError("refused"), LLMUnavailable),
        (httpx.ReadTimeout("slow"), LLMTimeout),
        (httpx.Response(404, json={"error": "model not found"}), LLMUnavailable),
        (httpx.Response(500, json={"error": "boom"}), LLMResponseError),
        (httpx.Response(200, content=b"not json"), LLMResponseError),
    ],
)
def test_chat_failures_map_to_llm_errors(behaviour, error):
    def handler(request):
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    with pytest.raises(error):
        ollama_with(handler).chat([{"role": "user", "content": "x"}], {}, GenOptions())


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:11434", True),
        ("http://[::1]:11434", True),
        ("http://127.0.0.2:11434", True),
        ("http://192.168.1.20:11434", False),
        ("https://ollama.example.com", False),
    ],
)
def test_locality_guard(host, local):
    assert is_loopback_host(host) is local
    if local:
        OllamaBackend(host)
    else:
        with pytest.raises(RemoteHostRefused):
            OllamaBackend(host)
        OllamaBackend(host, allow_remote=True)  # explicit opt-in only


# --- Runner: caching, validation, retries, metrics -----------------------------------------


def run(runner, user="Company: Acme\n\ntext"):
    return runner.extract(
        company_id=1,
        stage="enrich",
        system="sys",
        user=user,
        model_cls=Extraction,
        schema_hash=schema_hash(),
    )


def calls(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT cache_hit, ok, prompt_tokens, completion_tokens, latency_ms, error FROM llm_calls ORDER BY id"
        )
    ]


def test_valid_reply_is_cached_and_second_call_does_not_hit_backend(conn):
    backend = FakeLLMBackend([extraction_json()])
    runner = LLMRunner(conn, backend, prompt_version="v1")
    first = run(runner)
    assert first.status == c.OK and not first.cache_hit
    assert (first.prompt_tokens, first.completion_tokens, first.latency_ms) == (1000, 200, 1500.0)
    second = run(runner)
    assert second.status == c.OK and second.cache_hit and second.data == first.data
    assert len(backend.calls) == 1
    assert [(r["cache_hit"], r["ok"], r["prompt_tokens"]) for r in calls(conn)] == [
        (0, 1, 1000),
        (1, 1, 1000),
    ]


def test_bad_json_gets_one_retry_with_a_fix_nudge(conn):
    backend = FakeLLMBackend(['{"summary": "cut off', extraction_json()])
    outcome = run(LLMRunner(conn, backend, prompt_version="v1"))
    assert outcome.status == c.OK and outcome.calls == 2
    assert (outcome.prompt_tokens, outcome.completion_tokens) == (2000, 400)
    retry_messages = backend.calls[1]
    assert retry_messages[-2]["role"] == "assistant"
    assert "not valid JSON" in retry_messages[-1]["content"]
    assert [r["ok"] for r in calls(conn)] == [0, 1]
    assert calls(conn)[0]["error"].startswith("llm_invalid_output")


def test_schema_violations_count_as_invalid_and_are_never_cached(conn):
    bad = extraction_json(business_model="conglomerate")
    backend = FakeLLMBackend([bad, bad])
    outcome = run(LLMRunner(conn, backend, prompt_version="v1"))
    assert outcome.status == c.LLM_INVALID_OUTPUT and outcome.data is None
    assert "validation error" in outcome.error
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_unavailable_and_timeout_are_returned_not_raised(conn):
    backend = FakeLLMBackend([LLMUnavailable("down"), LLMTimeout("slow")])
    runner = LLMRunner(conn, backend, prompt_version="v1")
    assert run(runner).status == c.LLM_UNAVAILABLE
    assert run(runner).status == c.LLM_TIMEOUT
    assert [r["ok"] for r in calls(conn)] == [0, 0]


def test_prompt_version_or_model_change_invalidates_cache(conn):
    backend = FakeLLMBackend([extraction_json(), extraction_json(), extraction_json()])
    run(LLMRunner(conn, backend, prompt_version="v1"))
    run(LLMRunner(conn, backend, prompt_version="v2"))
    backend.model = "other-model"
    run(LLMRunner(conn, backend, prompt_version="v2"))
    assert len(backend.calls) == 3


def test_different_input_text_is_a_different_cache_entry(conn):
    backend = FakeLLMBackend([extraction_json(), extraction_json()])
    runner = LLMRunner(conn, backend, prompt_version="v1")
    run(runner, "Company: A\n\ntext one")
    run(runner, "Company: A\n\ntext two")
    assert len(backend.calls) == 2


def test_extraction_schema_has_no_person_or_revenue_fields():
    text = json.dumps(Extraction.model_json_schema()).lower()
    for forbidden in (
        "email",
        "phone",
        "revenue",
        "first_name",
        "last_name",
        "contact",
        "ceo_name",
    ):
        assert forbidden not in text
