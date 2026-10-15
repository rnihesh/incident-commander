"""Provider adapter over the OpenAI Chat Completions tool-calling shape (KTD5, R37).

OpenAI, Gemini, Anthropic (their OpenAI-compatible endpoints) and Ollama all speak the same
request shape, so one client class covers them; only ``base_url``, the API key, the model id
and a few per-provider request quirks differ. Requests use temperature 0 where the model
accepts it and ``parallel_tool_calls=False``: one tool call per turn keeps the transcript (and
the proof chain) linear and replayable.

``ScriptedLLM`` returns predefined replies in order, for tests that never touch a network.
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

# Default model ids per provider. Overridable with IC_LLM_MODEL; nothing else hardcodes one.
DEFAULT_MODELS = {
    "openai": "gpt-6-luna",
    "gemini": "gemini-2.5-flash",
    "ollama": "qwen3:8b",
    "anthropic": "claude-haiku-5-5",
}
BASE_URLS = {
    "openai": None,  # SDK default (https://api.openai.com/v1)
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "ollama": "http://localhost:11434/v1/",
    "anthropic": "https://api.anthropic.com/v1/",
}
# Environment variables consulted when IC_LLM_API_KEY is not set.
KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": None,
    "anthropic": "ANTHROPIC_API_KEY",
}
# OpenAI reasoning models refuse function tools on Chat Completions unless reasoning is off.
OPENAI_REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
# Local thinking models spend most of a step thinking (qwen3:8b: 11.7 s vs 0.7 s per tool call),
# which blows the per-call timeout; Ollama maps reasoning_effort "none" to think=false.
OLLAMA_THINKING_PREFIXES = ("qwen3", "deepseek-r1")


def request_options(provider: str, model: str) -> dict[str, Any]:
    """Sampling options a provider/model pair accepts (Anthropic rejects ``temperature``)."""
    if provider == "anthropic":
        return {}
    opts: dict[str, Any] = {"temperature": 0}
    if (provider == "openai" and model.startswith(OPENAI_REASONING_PREFIXES)) or (
        provider == "ollama" and model.startswith(OLLAMA_THINKING_PREFIXES)
    ):
        opts["reasoning_effort"] = "none"
    return opts


OLLAMA_PLACEHOLDER_KEY = "ollama"  # Ollama ignores the key but the SDK requires one


class LLMConfigError(ValueError):
    """The provider configuration cannot produce a client (unknown provider, no key)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str

    def arguments(self) -> dict:
        """Parsed arguments; raises ValueError on malformed JSON or a non-object."""
        args = json.loads(self.arguments_json or "{}")
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be a JSON object")
        return args


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class LLMReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> LLMReply:
        return cls(
            content=d.get("content"),
            tool_calls=[ToolCall(**tc) for tc in d.get("tool_calls") or []],
            usage=Usage(**(d.get("usage") or {})),
            model=d.get("model") or "",
        )

    def assistant_message(self) -> dict:
        """The Chat Completions assistant message that records this reply."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments_json},
                }
                for tc in self.tool_calls
            ]
        return msg


class LLMClient(Protocol):
    model: str

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    base_url: str | None
    api_key: str


class OpenAICompatLLM:
    """One client for every OpenAI-compatible provider."""

    def __init__(self, config: ProviderConfig, *, timeout_s: float = 30.0):
        self.config = config
        self.model = config.model
        # Retries are the loop's job (one retry per step, KTD5): the SDK must not add more.
        self.client = AsyncOpenAI(
            api_key=config.api_key, base_url=config.base_url, timeout=timeout_s, max_retries=0
        )

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            **request_options(self.config.provider, self.model),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False
        resp = await self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = [
            ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
            if getattr(tc, "function", None) is not None
        ]
        usage = Usage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )
        return LLMReply(msg.content, calls, usage, resp.model or self.model)


def provider_config(settings: Any) -> ProviderConfig:
    """Resolve provider, model, base_url and key from ``ic.config.Settings``-like values."""
    provider = (settings.llm_provider or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise LLMConfigError(f"unknown LLM provider {provider!r}; expected {sorted(BASE_URLS)}")
    key = settings.llm_api_key
    if not key and KEY_ENV[provider]:
        key = os.environ.get(KEY_ENV[provider])
    if not key:
        if provider != "ollama":
            raise LLMConfigError(
                f"no API key for {provider}: set IC_LLM_API_KEY or {KEY_ENV[provider]}"
            )
        key = OLLAMA_PLACEHOLDER_KEY
    return ProviderConfig(
        provider=provider,
        model=settings.llm_model or DEFAULT_MODELS[provider],
        base_url=settings.llm_base_url or BASE_URLS[provider],
        api_key=key,
    )


def make_llm(settings: Any = None) -> OpenAICompatLLM:
    """Client for the configured provider. No network call happens here."""
    if settings is None:
        from ic.config import get_settings

        settings = get_settings()
    return OpenAICompatLLM(provider_config(settings))


# --- scripted replies for tests ---------------------------------------------------------

ScriptStep = (
    LLMReply | BaseException | Callable[[list[dict], list[dict]], LLMReply | Awaitable[LLMReply]]
)


class ScriptExhausted(RuntimeError):
    """A ScriptedLLM was asked for more replies than it was given."""


_ids = itertools.count(1)


def call(name: str, **arguments: Any) -> ToolCall:
    """A tool call with a fresh deterministic id (for scripts)."""
    return ToolCall(f"call_{next(_ids):04d}", name, json.dumps(arguments, sort_keys=True))


def reply(*calls: ToolCall, content: str | None = None, tokens: tuple[int, int] = (0, 0)):
    return LLMReply(content, list(calls), Usage(*tokens), "scripted")


class ScriptedLLM:
    """Returns ``script`` entries in order. An entry may be an ``LLMReply``, an exception
    (raised), or ``fn(messages, tools)`` returning a reply (sync or async), which lets a
    script react to what the tools actually returned."""

    model = "scripted"

    def __init__(self, script: Sequence[ScriptStep]):
        self.script = list(script)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    @property
    def remaining(self) -> int:
        return len(self.script) - len(self.calls)

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        i = len(self.calls)
        self.calls.append((list(messages), list(tools)))
        if i >= len(self.script):
            raise ScriptExhausted(f"script has {len(self.script)} replies; asked for #{i + 1}")
        step = self.script[i]
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, LLMReply):
            return step
        out = step(messages, tools)
        if inspect.isawaitable(out):
            out = await out
        return out
