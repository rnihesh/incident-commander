"""Provider adapter (R37), scripted replies, and transcript record/replay (KTD5, KTD21)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ic.agent.llm import (
    BASE_URLS,
    DEFAULT_MODELS,
    LLMConfigError,
    LLMReply,
    OpenAICompatLLM,
    ScriptedLLM,
    ScriptExhausted,
    ToolCall,
    Usage,
    call,
    make_llm,
    provider_config,
    reply,
)
from ic.agent.transcripts import RecordingLLM, ReplayLLM, TranscriptMiss, transcript_key


def settings(**kw):
    base = {"llm_provider": "openai", "llm_base_url": None, "llm_model": None, "llm_api_key": None}
    return SimpleNamespace(**(base | kw))


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize(
    ("provider", "host"),
    [
        ("openai", "api.openai.com"),
        ("gemini", "generativelanguage.googleapis.com"),
        ("ollama", "localhost:11434"),
        ("anthropic", "api.anthropic.com"),
    ],
)
def test_adapter_builds_clients_without_network(provider, host):
    llm = make_llm(settings(llm_provider=provider, llm_api_key="k-test"))
    assert isinstance(llm, OpenAICompatLLM)
    assert host in str(llm.client.base_url)
    assert llm.model == DEFAULT_MODELS[provider]
    assert llm.client.max_retries == 0  # retries belong to the loop


def test_gemini_and_ollama_use_openai_compatible_endpoints():
    assert BASE_URLS["gemini"].endswith("/v1beta/openai/")
    assert BASE_URLS["ollama"] == "http://localhost:11434/v1/"


def test_model_and_base_url_are_overridable():
    cfg = provider_config(
        settings(llm_provider="ollama", llm_model="llama3.2", llm_base_url="http://gpu:11434/v1/")
    )
    assert (cfg.model, cfg.base_url, cfg.api_key) == ("llama3.2", "http://gpu:11434/v1/", "ollama")


def test_keys_from_env_and_missing_key_errors(monkeypatch):
    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
        provider_config(settings())
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert provider_config(settings(llm_provider="gemini")).api_key == "g-key"
    with pytest.raises(LLMConfigError, match="unknown LLM provider"):
        provider_config(settings(llm_provider="nope", llm_api_key="x"))


async def test_request_shape_temperature_zero_and_no_parallel_calls():
    llm = make_llm(settings(llm_api_key="k"))
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        tc = SimpleNamespace(
            id="call_1", function=SimpleNamespace(name="list_symptoms", arguments='{"a":1}')
        )
        msg = SimpleNamespace(content="thinking", tool_calls=[tc])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
            model="gpt-x",
        )

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    tools = [{"type": "function", "function": {"name": "list_symptoms", "parameters": {}}}]
    r = await llm.complete([{"role": "user", "content": "hi"}], tools)
    assert seen["temperature"] == 0 and seen["parallel_tool_calls"] is False
    assert seen["tools"] == tools and seen["model"] == DEFAULT_MODELS["openai"]
    assert r.tool_calls == [ToolCall("call_1", "list_symptoms", '{"a":1}')]
    assert r.usage == Usage(11, 3) and r.model == "gpt-x" and r.content == "thinking"


def _capture(llm):
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        msg = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model=kwargs["model"],
        )

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return seen


def test_anthropic_defaults_to_haiku_and_reads_its_env_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    cfg = provider_config(settings(llm_provider="anthropic"))
    assert (cfg.model, cfg.api_key) == ("claude-haiku-5-5", "a-key")
    assert cfg.base_url == "https://api.anthropic.com/v1/"


async def test_anthropic_requests_omit_temperature():
    llm = make_llm(settings(llm_provider="anthropic", llm_api_key="k"))
    seen = _capture(llm)
    await llm.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert "temperature" not in seen and seen["parallel_tool_calls"] is False


async def test_openai_reasoning_models_disable_reasoning_for_tool_calls():
    llm = make_llm(settings(llm_api_key="k"))
    assert llm.model == "gpt-6-luna"
    seen = _capture(llm)
    await llm.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert seen["reasoning_effort"] == "none" and seen["temperature"] == 0
    other = make_llm(settings(llm_api_key="k", llm_model="gpt-4.1-mini"))
    seen = _capture(other)
    await other.complete([{"role": "user", "content": "hi"}], [])
    assert "reasoning_effort" not in seen


async def test_ollama_thinking_models_answer_without_thinking():
    qwen = make_llm(settings(llm_provider="ollama"))
    seen = _capture(qwen)
    await qwen.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert seen["reasoning_effort"] == "none" and seen["temperature"] == 0
    plain = make_llm(settings(llm_provider="ollama", llm_model="mistral"))
    seen = _capture(plain)
    await plain.complete([{"role": "user", "content": "hi"}], [])
    assert "reasoning_effort" not in seen


async def test_scripted_llm_replies_in_order_and_raises():
    async def dynamic(messages, tools):
        return reply(content=f"saw {len(messages)} messages")

    s = ScriptedLLM(
        [reply(call("list_symptoms", incident_id="inc-1")), RuntimeError("boom"), dynamic]
    )
    r1 = await s.complete([], [])
    assert r1.tool_calls[0].name == "list_symptoms"
    assert r1.tool_calls[0].arguments() == {"incident_id": "inc-1"}
    with pytest.raises(RuntimeError, match="boom"):
        await s.complete([], [])
    assert (await s.complete([{"role": "user"}], [])).content == "saw 1 messages"
    with pytest.raises(ScriptExhausted):
        await s.complete([], [])
    assert len(s.calls) == 4


def test_assistant_message_shape():
    r = LLMReply("x", [ToolCall("c1", "t", "{}")], Usage(1, 2), "m")
    msg = r.assistant_message()
    assert msg == {
        "role": "assistant",
        "content": "x",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        ],
    }
    assert LLMReply.from_dict(r.to_dict()) == r


async def test_transcript_record_then_replay_roundtrip(tmp_path):
    path = tmp_path / "t.json"
    script = ScriptedLLM(
        [reply(call("list_symptoms", incident_id="inc-1"), tokens=(5, 2)), reply(content="done")]
    )
    rec = RecordingLLM(script, path)
    m1 = [{"role": "user", "content": "graph s:" + "a" * 32 + ":world"}]
    m2 = [*m1, {"role": "assistant", "content": None}]
    r1 = await rec.complete(m1, [])
    r2 = await rec.complete(m2, [])
    doc = json.loads(path.read_text())
    assert len(doc["replies"]) == 2

    rep = ReplayLLM(path)
    # a different session id inside graph names maps to the same key
    other = [{"role": "user", "content": "graph s:" + "b" * 32 + ":world"}]
    assert await rep.complete(other, []) == r1
    assert await rep.complete(m2, []) == r2
    assert rep.hits == 2
    with pytest.raises(TranscriptMiss):
        await rep.complete([{"role": "user", "content": "never recorded"}], [])


def test_transcript_key_is_canonical():
    a = [{"role": "user", "content": "x", "name": "n"}]
    b = [{"name": "n", "content": "x", "role": "user"}]
    assert transcript_key(a, []) == transcript_key(b, [])
    assert transcript_key(a, []) != transcript_key(a, [{"type": "function"}])


def test_replay_requires_a_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplayLLM(tmp_path / "missing.json")
