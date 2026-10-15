"""Record/replay of LLM replies, keyed by a hash of the request (KTD5, KTD21).

The key is ``sha256`` of the canonical JSON of ``(messages, tools)``. Session ids (32 hex
characters inside graph names) are normalized first, so a transcript recorded in one session
replays in another one built from the same seed. Tool results reach the model already
stripped of timings and hashes (see ``loop.compact_result``), so equal worlds give equal keys.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ic.agent.llm import LLMClient, LLMReply

_SID = re.compile(r"\b[a-f0-9]{32}\b")
FORMAT = 1


class TranscriptMiss(KeyError):
    """Replay was asked for a request it has no recorded reply for."""


def _normalize(s: str) -> str:
    return _SID.sub("<sid>", s)


def transcript_key(messages: list[dict], tools: list[dict]) -> str:
    canon = json.dumps(
        {"messages": messages, "tools": tools},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(_normalize(canon).encode()).hexdigest()


def load_transcript(path: str | Path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return dict(data.get("replies", {}))


def _save(path: Path, replies: dict[str, dict], model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"format": FORMAT, "model": model, "replies": replies}
    path.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


class RecordingLLM:
    """Calls ``inner`` and records every reply under its request key (file kept current)."""

    def __init__(self, inner: LLMClient, path: str | Path):
        self.inner = inner
        self.model = inner.model
        self.path = Path(path)
        self.replies = load_transcript(self.path)

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        r = await self.inner.complete(messages, tools)
        self.replies[transcript_key(messages, tools)] = r.to_dict()
        _save(self.path, self.replies, self.model)
        return r


class ReplayLLM:
    """Serves recorded replies; a request never seen while recording raises TranscriptMiss."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"no transcript at {self.path}")
        doc = json.loads(self.path.read_text())
        self.model = doc.get("model") or "replay"
        self.replies: dict[str, dict] = dict(doc.get("replies", {}))
        self.hits = 0

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        key = transcript_key(messages, tools)
        try:
            d = self.replies[key]
        except KeyError:
            raise TranscriptMiss(f"no recorded reply for request {key[:12]}") from None
        self.hits += 1
        return LLMReply.from_dict(d)
