"""What every benchmark approach shares: the case handle, the prediction record, snapshot
readers and the single-question LLM helper.

Approaches read only the case's ``known`` as-of snapshot (what was known at alert time) and
never the answer key. Every approach gets the same candidate corpus: the changes and
expiries in the predicate's window ``[earliest onset - 120 min, alert time]`` (KTD15)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ic.agent.llm import LLMClient
from ic.graph.client import query
from ic.store import Store
from ic.tools.predicate import DEFAULT_WINDOW_MINUTES
from ic.world.embed import Embedder, get_embedder

ID_RE = re.compile(r"\b(chg-\d{5,}|exp-\d{4,})\b")


@dataclass(frozen=True)
class CaseView:
    """One benchmark case as an approach sees it: graph names and alert facts, no answer."""

    run: str
    case_id: str
    sid: str  # the case's session id (ledger scope for the graph agent)
    incident_id: str
    alert_time: int
    alert_service: str
    world: str
    snapshot_true: str
    snapshot_known: str


@dataclass
class Prediction:
    predicted: str | None
    llm_tokens: int = 0  # tokens actually sent to / returned by an LLM
    prompt_tokens_est: int = 0  # len(prompt)/4 of the prompt this approach builds
    tool_calls: int = 0
    verdict: str | None = None
    truncated: bool = False
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchContext:
    llm: LLMClient | None
    store: Store
    context_limit: int = 128_000
    top_k: int = 8
    embedder: Embedder | None = None  # default: get_embedder() (HashingEmbedder without a key)

    def get_embedder(self) -> Embedder:
        if self.embedder is None:
            self.embedder = get_embedder()
        return self.embedder


Approach = Callable[[CaseView, BenchContext], Awaitable[Prediction]]


def est_tokens(text: str) -> int:
    """Tokenizer-free estimate: one token per four characters."""
    return (len(text) + 3) // 4


def iso(t: int | None) -> str:
    return "" if t is None else datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_id(text: str | None, candidates: list[str]) -> str | None:
    """The first change/expiry id in ``text`` that is a candidate, else the first id seen."""
    found = ID_RE.findall(text or "")
    allowed = set(candidates)
    return next((i for i in found if i in allowed), found[0] if found else None)


async def ask_llm(llm: LLMClient, system: str, user: str) -> tuple[str | None, int]:
    """One completion without tools. Returns (content, prompt + completion tokens)."""
    reply = await llm.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], []
    )
    return reply.content, reply.usage.prompt_tokens + reply.usage.completion_tokens


# --- snapshot readers --------------------------------------------------------------------


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True, timeout_ms=30000)).result_set


async def read_incident(graph: str, incident_id: str) -> dict:
    rows = await _rows(
        graph,
        "MATCH (i:Incident {id: $iid}) OPTIONAL MATCH (m:SnapshotMeta) "
        "RETURN i.title, i.opened_at, i.alert_service_id, max(m.t)",
        {"iid": incident_id},
    )
    title, opened, svc, t = rows[0]
    return {"title": title or "", "opened_at": opened, "alert_service": svc, "t": t or opened}


async def read_symptoms(graph: str, incident_id: str) -> list[dict]:
    rows = await _rows(
        graph,
        "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom)-[:ON]->(s:Service) "
        "RETURN s.id, s.name, y.onset, y.signature ORDER BY y.onset, s.id",
        {"iid": incident_id},
    )
    return [{"service_id": a, "name": b, "onset": c, "signature": d} for a, b, c, d in rows]


async def read_window(
    graph: str, earliest: int, hi: int, minutes: int = DEFAULT_WINDOW_MINUTES
) -> list[dict]:
    """Changes (by valid_from) and expiries (by valid_to) in ``[earliest - minutes, hi]``."""
    p = {"lo": earliest - minutes * 60, "hi": hi}
    rows = await _rows(
        graph,
        "MATCH (c:Change)-[:CHANGES]->(s:Service) "
        "WHERE $lo <= c.valid_from AND c.valid_from <= $hi "
        "RETURN c.id AS id, 'Change' AS entity, c.kind AS kind, s.id AS service_id, "
        "s.name AS name, c.valid_from AS t, c.summary AS summary "
        "UNION ALL "
        "MATCH (e:Expiry)-[:EXPIRES_ON]->(s:Service) "
        "WHERE $lo <= e.valid_to AND e.valid_to <= $hi "
        "RETURN e.id AS id, 'Expiry' AS entity, e.kind AS kind, s.id AS service_id, "
        "s.name AS name, e.valid_to AS t, e.summary AS summary",
        p,
    )
    keys = ("id", "entity", "kind", "service_id", "name", "event_time", "summary")
    out = [dict(zip(keys, r, strict=True)) for r in rows]
    return sorted(out, key=lambda e: (e["event_time"], e["id"]))


async def read_declared(graph: str) -> list[tuple[str, str]]:
    """(caller, callee) DEPENDS_ON edges of the snapshot (already valid-time pruned): the
    declared service catalog. OBSERVED_CALL is deliberately not included."""
    rows = await _rows(
        graph, "MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) RETURN a.id, b.id ORDER BY a.id, b.id"
    )
    return [(a, b) for a, b in rows]


async def read_postmortems(graph: str) -> list[dict]:
    rows = await _rows(
        graph,
        "MATCH (p:Postmortem) RETURN p.id, p.title, p.symptoms_text, p.body ORDER BY p.id",
    )
    return [{"id": a, "title": b, "symptoms_text": c, "body": d} for a, b, c, d in rows]


@dataclass
class IncidentData:
    incident: dict
    symptoms: list[dict]
    candidates: list[dict]

    @property
    def earliest(self) -> int:
        return min(s["onset"] for s in self.symptoms)


async def read_incident_data(case: CaseView) -> IncidentData:
    g = case.snapshot_known
    inc = await read_incident(g, case.incident_id)
    syms = await read_symptoms(g, case.incident_id)
    if not syms:
        return IncidentData(inc, [], [])
    earliest = min(s["onset"] for s in syms)
    return IncidentData(inc, syms, await read_window(g, earliest, inc["t"]))
