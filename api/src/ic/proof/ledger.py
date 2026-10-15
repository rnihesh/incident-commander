"""Proof ledger (KTD4, R12, R13): pre-registered, hash-chained proofs in ``s:{sid}:ledger``.

A hypothesis is one of a fixed set of kinds, each bound to an existing registry tool (its
falsifier), the arguments it is called with, and a pure-Python ``holds`` test over a small
``conclusion`` dict. ``test_hypothesis`` (one agent turn) does, strictly in this order:

  1. write ``(:Hypothesis {state: 'REGISTERED', falsifier_tool, falsifier_args, expected})``
     and wait for the write to be confirmed (R12: the falsifier is on record first);
  2. run the falsifier through the registry on the run's snapshot;
  3. write ``(:Proof)`` linked ``TESTS`` the hypothesis and ``PREV`` the previous proof, and
     set the hypothesis to HOLDS or REFUTED.

Chain link (R13)::

    hash = sha256(prev_hash | result_hash | conclusion_hash | evidence_hash
                  | hypothesis_hash | kind | entity_id | seq)
    evidence_hash   = sha256(ordered JSON {cypher, params, nodes_touched, graph})
    hypothesis_hash = content_hash({claim, expected, falsifier_tool, falsifier_args,
                                    kind, entity_id})
    genesis prev_hash = sha256("genesis:" + sid + ":" + iid + ":" + t + ":" + world)

Everything Verify replays or displays is therefore bound to the head hash, and the head
hash plus ``t``/``world`` are anchored in the API-owned SQLite store (KTD4): the ledger
graph is editable by anyone with Browser access, the store is not.

Ledger graph::

    (:LedgerIncident {id, sid, t, world, opened_at})
    (:Hypothesis {id, incident_id, seq, kind, entity_id, claim, falsifier_tool,
                  falsifier_args, expected, state, registered_at})-[:FOR]->(:LedgerIncident)
    (:Proof {id, incident_id, seq, result_hash, conclusion, conclusion_hash, evidence_hash,
             hypothesis_hash, prev_hash, hash, holds, cypher, params, nodes_touched, graph,
             executed_at})-[:TESTS]->(:Hypothesis)
    (:Proof)-[:PREV]->(:Proof)

JSON-valued properties (args, expected, conclusion, cypher, params, nodes_touched) are stored
as canonical JSON strings. Timestamps are epoch seconds (float) and never hashed.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from pydantic import Field

from ic.graph import naming
from ic.graph.client import query
from ic.locks import KeyedLocks
from ic.proof.canonical import canonical_json, content_hash, sha256_hex
from ic.store import Store
from ic.tools import registry
from ic.tools.registry import ScopeError, ToolContext, ToolInput, register
from ic.tools.result import ToolError, ToolResult

REGISTERED, HOLDS, REFUTED, ABANDONED = "REGISTERED", "HOLDS", "REFUTED", "ABANDONED"

KindName = Literal[
    "suspect_qualifies", "suspect_necessary", "no_other_qualifying", "entity_refuted"
]
KIND_NAMES: tuple[str, ...] = get_args(KindName)


# --- hypothesis kinds ------------------------------------------------------------------


def _verdict_conclusion(result: dict) -> dict:
    return {"ok": bool(result["qualifies"]), "clause": result["verdict"]["clause"]}


def _necessity_conclusion(result: dict) -> dict:
    return {
        "necessary": bool(result["necessary"]),
        "rivals": sorted(r["id"] for r in result["rivals"]),
    }


def _absence_conclusion(result: dict) -> dict:
    return {"qualifying": sorted(result["qualifying"])}


@dataclass(frozen=True)
class HypothesisKind:
    """A falsifiable claim shape: which tool tests it and what result makes it hold."""

    name: str
    falsifier_tool: str
    entity_arg: str  # the tool argument the entity id goes into
    expected: dict  # what the conclusion must show for the hypothesis to hold
    conclusion: Callable[[dict], dict]
    holds_if: Callable[[dict], bool]  # over the conclusion
    describe: str

    def args(self, incident_id: str, entity_id: str) -> dict:
        return {"incident_id": incident_id, self.entity_arg: entity_id}

    def holds(self, result: dict) -> bool:
        return self.holds_if(self.conclusion(result))


KINDS: dict[str, HypothesisKind] = {
    k.name: k
    for k in (
        HypothesisKind(
            "suspect_qualifies",
            "exonerate",
            "change_id",
            {"ok": True},
            _verdict_conclusion,
            lambda c: c["ok"] is True,
            "the entity passes every KTD23 clause",
        ),
        HypothesisKind(
            "suspect_necessary",
            "necessity",
            "suspect_id",
            {"necessary": True, "rivals": []},
            _necessity_conclusion,
            lambda c: c["necessary"] is True,
            "with the entity removed, no rival qualifies",
        ),
        HypothesisKind(
            "no_other_qualifying",
            "absence_certificate",
            "suspect_id",
            {"qualifying": []},
            _absence_conclusion,
            lambda c: c["qualifying"] == [],
            "no other change or expiry upstream of the symptoms qualifies",
        ),
        HypothesisKind(
            "entity_refuted",
            "exonerate",
            "change_id",
            {"ok": False},
            _verdict_conclusion,
            lambda c: c["ok"] is False,
            "the entity fails at least one KTD23 clause (the first is recorded)",
        ),
    )
}
assert set(KINDS) == set(KIND_NAMES)


# --- records ---------------------------------------------------------------------------


@dataclass
class HypothesisRecord:
    id: str
    incident_id: str
    seq: int
    kind: str
    entity_id: str
    claim: str
    falsifier_tool: str
    falsifier_args: dict
    expected: dict
    state: str
    registered_at: float


@dataclass
class ProofRecord:
    id: str
    sid: str
    incident_id: str
    t: int  # the ledger incident's alert time and world, as stored (genesis inputs)
    world: str
    seq: int
    kind: str
    entity_id: str
    claim: str
    state: str  # HOLDS | REFUTED (the hypothesis' resolved state)
    holds: bool
    falsifier_tool: str
    falsifier_args: dict
    expected: dict
    result_hash: str
    conclusion: dict
    conclusion_hash: str
    evidence_hash: str
    hypothesis_hash: str
    prev_hash: str
    hash: str
    cypher: list[str]
    params: list[dict]
    nodes_touched: list[str]
    graph: str
    registered_at: float
    executed_at: float
    hypothesis_id: str
    prev_id: str | None = None
    result: Any = field(default=None, repr=False)  # the falsifier's result, when just run

    def as_dict(self, *, with_result: bool = False) -> dict:
        d = dataclasses.asdict(self)
        if not with_result:
            d.pop("result")
        return d


class LedgerCorrupt(ToolError):
    """A ledger node no longer parses (an edited JSON property, a missing field)."""


def genesis_hash(sid: str, iid: str, t: int, world: str) -> str:
    """The chain's root: binds the session, incident, alert time and world graph, so an
    edit to any of them on the LedgerIncident node changes every hash after it."""
    return sha256_hex(f"genesis:{sid}:{iid}:{int(t)}:{world}")


def _ordered_dump(obj: Any) -> str:
    """JSON that keeps list order (cypher/params are positional)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def evidence_hash(
    cypher: list[str], params: list[dict], nodes_touched: list[str], graph: str
) -> str:
    """The displayed, re-runnable evidence. Order-preserving: ``cypher[i]`` pairs with
    ``params[i]``, so reordering statements is an edit too."""
    return sha256_hex(
        _ordered_dump(
            {"cypher": cypher, "params": params, "nodes_touched": nodes_touched, "graph": graph}
        )
    )


def hypothesis_hash(
    claim: str,
    expected: dict,
    falsifier_tool: str,
    falsifier_args: dict,
    kind: str,
    entity_id: str,
) -> str:
    return content_hash(
        {
            "claim": claim,
            "expected": expected,
            "falsifier_tool": falsifier_tool,
            "falsifier_args": falsifier_args,
            "kind": kind,
            "entity_id": entity_id,
        }
    )


def _evidence_of(p: ProofRecord) -> str:
    return evidence_hash(p.cypher, p.params, p.nodes_touched, p.graph)


def _hypothesis_of(p: ProofRecord) -> str:
    return hypothesis_hash(
        p.claim, p.expected, p.falsifier_tool, p.falsifier_args, p.kind, p.entity_id
    )


def link_hash(
    prev_hash: str,
    result_hash: str,
    conclusion_hash: str,
    evidence: str,
    hypothesis: str,
    kind: str,
    entity_id: str,
    seq: int,
) -> str:
    return sha256_hex(
        "|".join(
            [
                prev_hash,
                result_hash,
                conclusion_hash,
                evidence,
                hypothesis,
                kind,
                entity_id,
                str(seq),
            ]
        )
    )


def _genesis_of(chain: list[ProofRecord], genesis: str | None) -> str | None:
    if genesis is not None or not chain:
        return genesis
    p = chain[0]
    return genesis_hash(p.sid, p.incident_id, p.t, p.world)


def verify_chain_integrity(chain: list[ProofRecord], genesis: str | None = None) -> int | None:
    """Recompute every link; the seq of the first broken proof, or None if intact.

    ``genesis`` is the trusted chain root (Verify derives it from the anchored t/world);
    by default it is rebuilt from the ledger incident's stored values. Besides the links,
    each proof's stored evidence/hypothesis hashes must match its stored content, its
    ``holds`` must follow from its conclusion, and its hypothesis state must be the one
    that ``holds`` implies (a proven hypothesis cannot be relabelled ABANDONED)."""
    prev_hash = _genesis_of(chain, genesis)
    prev_id: str | None = None
    for p in chain:
        kind = KINDS.get(p.kind)
        ok = (
            kind is not None
            and p.prev_hash == prev_hash
            and p.prev_id == prev_id
            and p.conclusion_hash == content_hash(p.conclusion)
            and p.evidence_hash == _evidence_of(p)
            and p.hypothesis_hash == _hypothesis_of(p)
            and p.holds == kind.holds_if(p.conclusion)
            and p.state == (HOLDS if p.holds else REFUTED)
            and p.hash
            == link_hash(
                p.prev_hash,
                p.result_hash,
                p.conclusion_hash,
                p.evidence_hash,
                p.hypothesis_hash,
                p.kind,
                p.entity_id,
                p.seq,
            )
        )
        if not ok:
            return p.seq
        prev_hash, prev_id = p.hash, p.id
    return None


def recompute_head(chain: list[ProofRecord], genesis: str | None = None) -> str | None:
    """Head hash rebuilt from genesis using only each proof's content (ignores the stored
    ``hash``/``prev_hash``/``*_hash`` fields), so any edit to a proof's content, its
    hypothesis, its evidence, or the incident's sid/iid/t/world changes it."""
    head = _genesis_of(chain, genesis)
    for p in chain:
        assert head is not None
        head = link_hash(
            head,
            p.result_hash,
            content_hash(p.conclusion),
            _evidence_of(p),
            _hypothesis_of(p),
            p.kind,
            p.entity_id,
            p.seq,
        )
    return head


# --- ledger graph access ---------------------------------------------------------------

_locks: KeyedLocks[tuple[str, str]] = KeyedLocks()


def _lock(graph: str, iid: str) -> AbstractAsyncContextManager[None]:
    return _locks((graph, iid))


def _dump(obj: Any) -> str:
    return canonical_json(obj)


async def open_incident(sid: str, iid: str, t: int, world_graph: str) -> str:
    """Create (idempotently) the incident's root node in the session ledger."""
    graph = naming.ledger(sid)
    naming.asof(sid, iid, "known")  # validates iid
    if not naming.scope_allows(world_graph, sid=sid):
        raise ScopeError(f"{world_graph} is outside session {sid}")
    await query(
        graph,
        "MERGE (li:LedgerIncident {id: $iid}) "
        "ON CREATE SET li.sid = $sid, li.t = $t, li.world = $world, li.opened_at = $now",
        {"iid": iid, "sid": sid, "t": t, "world": world_graph, "now": time.time()},
    )
    return graph


async def incident_info(graph: str, iid: str) -> dict:
    res = await query(
        graph,
        "MATCH (li:LedgerIncident {id: $iid}) RETURN li",
        {"iid": iid},
        read_only=True,
    )
    if not res.result_set:
        raise ToolError(f"incident {iid} has not been opened in the ledger")
    return dict(res.result_set[0][0].properties)


async def _register(graph: str, iid: str, kind: HypothesisKind, entity_id: str, claim: str):
    args = kind.args(iid, entity_id)
    now = time.time()
    res = await query(
        graph,
        """
        MATCH (li:LedgerIncident {id: $iid})
        OPTIONAL MATCH (prev:Hypothesis)-[:FOR]->(li)
        WITH li, count(prev) + 1 AS seq
        CREATE (h:Hypothesis {incident_id: $iid, seq: seq, kind: $kind, entity_id: $entity_id,
                              claim: $claim, falsifier_tool: $tool, falsifier_args: $args,
                              expected: $expected, state: $state, registered_at: $now})
        SET h.id = 'hyp-' + $iid + '-' + toString(seq)
        CREATE (h)-[:FOR]->(li)
        RETURN h
        """,
        {
            "iid": iid,
            "kind": kind.name,
            "entity_id": entity_id,
            "claim": claim,
            "tool": kind.falsifier_tool,
            "args": _dump(args),
            "expected": _dump(kind.expected),
            "state": REGISTERED,
            "now": now,
        },
    )
    if not res.result_set:
        raise ToolError(f"incident {iid} has not been opened in the ledger")
    return _hypothesis(res.result_set[0][0].properties)


def _hypothesis(p: dict) -> HypothesisRecord:
    try:
        return HypothesisRecord(
            id=p["id"],
            incident_id=p["incident_id"],
            seq=p["seq"],
            kind=p["kind"],
            entity_id=p["entity_id"],
            claim=p["claim"],
            falsifier_tool=p["falsifier_tool"],
            falsifier_args=json.loads(p["falsifier_args"]),
            expected=json.loads(p["expected"]),
            state=p["state"],
            registered_at=p["registered_at"],
        )
    except (KeyError, TypeError, ValueError) as e:
        raise LedgerCorrupt(f"hypothesis {p.get('id')!r} does not parse: {e!r}") from e


async def _last_proof(graph: str, iid: str) -> tuple[str | None, str | None]:
    res = await query(
        graph,
        "MATCH (p:Proof {incident_id: $iid}) RETURN p.id, p.hash ORDER BY p.seq DESC LIMIT 1",
        {"iid": iid},
        read_only=True,
    )
    return (res.result_set[0][0], res.result_set[0][1]) if res.result_set else (None, None)


async def _run_falsifier(ctx: ToolContext, tool: str, args: dict) -> ToolResult:
    """The falsifier runs through the registry (same validation and scope checks as any
    tool call); its result is carried inside the test_hypothesis result, not emitted twice."""
    return await registry.call_tool(tool, args, dataclasses.replace(ctx, emit=None))


def _ledger_for(ctx: ToolContext) -> tuple[str, str, str]:
    graph = ctx.graph_for("ledger")
    if not ctx.allows(graph):
        raise ScopeError(f"test_hypothesis may not touch {graph} from this run's scope")
    if ctx.sid is None or graph != naming.ledger(ctx.sid) or not ctx.iid:
        raise ScopeError("test_hypothesis needs this session's ledger and an incident id")
    return graph, ctx.sid, ctx.iid


def _genesis_inputs(ctx: ToolContext, info: dict) -> tuple[str, int, str]:
    """(sid, t, world) the chain is rooted at: the run's trusted values when the context
    carries them, else the ledger incident's stored (editable) copies."""
    if ctx.trusted_t is None and ctx.trusted_world is None:
        return info["sid"], info["t"], info["world"]
    assert ctx.sid is not None
    t = ctx.trusted_t if ctx.trusted_t is not None else info["t"]
    world = ctx.trusted_world if ctx.trusted_world is not None else info["world"]
    return ctx.sid, int(t), world


async def test_hypothesis(ctx: ToolContext, kind: str, entity_id: str, claim: str) -> ProofRecord:
    """Pre-register, run, and chain one hypothesis (KTD4). See the module docstring."""
    if kind not in KINDS:
        raise ToolError(f"unknown hypothesis kind {kind!r}; expected one of {KIND_NAMES}")
    k = KINDS[kind]
    graph, sid, iid = _ledger_for(ctx)
    async with _lock(graph, iid):
        info = await incident_info(graph, iid)
        # 1. Pre-registration: confirmed in the graph before the falsifier runs (R12).
        h = await _register(graph, iid, k, entity_id, claim)
        # 2. Falsifier.
        try:
            r = await _run_falsifier(ctx, k.falsifier_tool, h.falsifier_args)
        except ToolError as e:
            await query(
                graph,
                "MATCH (h:Hypothesis {id: $hid}) SET h.state = $state, h.error = $err",
                {"hid": h.id, "state": ABANDONED, "err": str(e)[:500]},
            )
            raise ToolError(f"{h.id} abandoned: falsifier {k.falsifier_tool} failed: {e}") from e
        executed_at = time.time()
        # 3. Proof, chained to the previous one.
        conclusion = k.conclusion(r.result)
        holds = k.holds_if(conclusion)
        result_hash = content_hash(r.result)
        conclusion_hash = content_hash(conclusion)
        # Hash the evidence exactly as it will read back from the ledger.
        cypher_s, params_s = _ordered_dump(r.cypher), _ordered_dump(r.params)
        touched = sorted(set(r.nodes_touched))
        touched_s = _dump(touched)
        ev_hash = evidence_hash(
            json.loads(cypher_s), json.loads(params_s), json.loads(touched_s), r.graph
        )
        hyp_hash = hypothesis_hash(
            h.claim, h.expected, h.falsifier_tool, h.falsifier_args, h.kind, h.entity_id
        )
        g_sid, g_t, g_world = _genesis_inputs(ctx, info)
        prev_id, prev_hash = await _last_proof(graph, iid)
        if prev_hash is None:
            prev_hash = genesis_hash(g_sid, iid, g_t, g_world)
        link = link_hash(
            prev_hash, result_hash, conclusion_hash, ev_hash, hyp_hash, kind, entity_id, h.seq
        )
        state = HOLDS if holds else REFUTED
        props = {
            "id": f"prf-{iid}-{h.seq}",
            "incident_id": iid,
            "seq": h.seq,
            "result_hash": result_hash,
            "conclusion": _dump(conclusion),
            "conclusion_hash": conclusion_hash,
            "evidence_hash": ev_hash,
            "hypothesis_hash": hyp_hash,
            "prev_hash": prev_hash,
            "hash": link,
            "holds": holds,
            "cypher": cypher_s,
            "params": params_s,
            "nodes_touched": touched_s,
            "graph": r.graph,
            "executed_at": executed_at,
        }
        prev_match = ", (q:Proof {id: $prev_id})" if prev_id else ""
        prev_create = " CREATE (p)-[:PREV]->(q)" if prev_id else ""
        res = await query(
            graph,
            f"MATCH (h:Hypothesis {{id: $hid}}){prev_match} "
            f"CREATE (p:Proof)-[:TESTS]->(h) SET p += $props, h.state = $state{prev_create} "
            "RETURN p.id",
            {"hid": h.id, "prev_id": prev_id, "props": props, "state": state},
        )
        if not res.result_set:
            raise ToolError(f"could not write the proof for {h.id}")
    return ProofRecord(
        id=props["id"],
        sid=g_sid,
        incident_id=iid,
        t=g_t,
        world=g_world,
        seq=h.seq,
        kind=kind,
        entity_id=entity_id,
        claim=claim,
        state=state,
        holds=holds,
        falsifier_tool=k.falsifier_tool,
        falsifier_args=h.falsifier_args,
        expected=k.expected,
        result_hash=result_hash,
        conclusion=conclusion,
        conclusion_hash=conclusion_hash,
        evidence_hash=ev_hash,
        hypothesis_hash=hyp_hash,
        prev_hash=prev_hash,
        hash=link,
        cypher=r.cypher,
        params=r.params,
        nodes_touched=touched,
        graph=r.graph,
        registered_at=h.registered_at,
        executed_at=executed_at,
        hypothesis_id=h.id,
        prev_id=prev_id,
        result=r.result,
    )


test_hypothesis.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


async def abandon_pending(sid: str, iid: str) -> list[str]:
    """A run stopped before its falsifiers ran: REGISTERED -> ABANDONED. Returns their ids."""
    res = await query(
        naming.ledger(sid),
        "MATCH (h:Hypothesis {incident_id: $iid, state: $from}) "
        "SET h.state = $to, h.abandoned_at = $now RETURN h.id ORDER BY h.seq",
        {"iid": iid, "from": REGISTERED, "to": ABANDONED, "now": time.time()},
    )
    return [r[0] for r in res.result_set]


async def hypotheses(sid: str, iid: str, *, graph: str | None = None) -> list[HypothesisRecord]:
    res = await query(
        graph or naming.ledger(sid),
        "MATCH (h:Hypothesis {incident_id: $iid}) RETURN h ORDER BY h.seq",
        {"iid": iid},
        read_only=True,
    )
    return [_hypothesis(r[0].properties) for r in res.result_set]


async def chain(sid: str, iid: str, *, graph: str | None = None) -> list[ProofRecord]:
    """Every proof of the incident in seq order, as stored."""
    g = graph or naming.ledger(sid)
    info = await incident_info(g, iid)
    res = await query(
        g,
        "MATCH (p:Proof {incident_id: $iid})-[:TESTS]->(h:Hypothesis) "
        "OPTIONAL MATCH (p)-[:PREV]->(q:Proof) "
        "RETURN p, h, q.id ORDER BY p.seq",
        {"iid": iid},
        read_only=True,
    )
    out = []
    for pn, hn, prev_id in res.result_set:
        p, h = pn.properties, _hypothesis(hn.properties)
        try:
            out.append(_proof(p, h, info, iid, prev_id))
        except (KeyError, TypeError, ValueError) as e:
            raise LedgerCorrupt(f"proof {p.get('id')!r} does not parse: {e!r}") from e
    return out


def _proof(p: dict, h: HypothesisRecord, info: dict, iid: str, prev_id: str | None):
    return ProofRecord(
        id=p["id"],
        sid=info["sid"],
        incident_id=iid,
        t=info["t"],
        world=info["world"],
        seq=p["seq"],
        kind=h.kind,
        entity_id=h.entity_id,
        claim=h.claim,
        state=h.state,
        holds=p["holds"],
        falsifier_tool=h.falsifier_tool,
        falsifier_args=h.falsifier_args,
        expected=h.expected,
        result_hash=p["result_hash"],
        conclusion=json.loads(p["conclusion"]),
        conclusion_hash=p["conclusion_hash"],
        evidence_hash=p.get("evidence_hash", ""),
        hypothesis_hash=p.get("hypothesis_hash", ""),
        prev_hash=p["prev_hash"],
        hash=p["hash"],
        cypher=json.loads(p["cypher"]),
        params=json.loads(p["params"]),
        nodes_touched=json.loads(p["nodes_touched"]),
        graph=p["graph"],
        registered_at=h.registered_at,
        executed_at=p["executed_at"],
        hypothesis_id=h.id,
        prev_id=prev_id,
    )


async def head_hash(sid: str, iid: str) -> str | None:
    """Stored hash of the latest proof (None before the first proof)."""
    _, h = await _last_proof(naming.ledger(sid), iid)
    return h


async def anchor(
    sid: str, iid: str, store: Store, *, t: int | None = None, world: str | None = None
) -> str | None:
    """At incident completion: put the chain head hash, with the alert time and world it
    was proven against, in the API-owned SQLite store.

    Pass the run's own ``t``/``world`` whenever the caller knows them: by default they are
    read back from the ledger incident, which anyone with Browser access can edit."""
    head = await head_hash(sid, iid)
    if head is not None:
        if t is None or world is None:
            info = await incident_info(naming.ledger(sid), iid)
            t = int(info["t"]) if t is None else t
            world = info["world"] if world is None else world
        store.put_anchor(
            session_id=sid,
            incident_id=iid,
            head_hash=head,
            t=int(t),
            world=world,
        )
    return head


# --- the rehearsal record ----------------------------------------------------------------

# ``(:Rehearsal)-[:FOR]->(:LedgerIncident)`` is written by remediation outside the hash
# chain; its content hash is sealed in the store when it is written (``rehearsal_seal``, and
# frozen with a PR's anchor) and recomputed by Verify and the proof page.
REHEARSAL_FIELDS = (
    "id",
    "incident_id",
    "change_id",
    "result",
    "still_explains",
    "checked",
    "clone_ms",
    "detail",
    "at",
)
LATEST_REHEARSAL_Q = (
    "MATCH (r:Rehearsal)-[:FOR]->(:LedgerIncident {id: $iid}) "
    "RETURN r ORDER BY r.at DESC, r.id DESC LIMIT 1"
)


def rehearsal_record(props: dict) -> dict:
    return {k: props.get(k) for k in REHEARSAL_FIELDS}


def rehearsal_hash(record: dict) -> str:
    """Content hash of a rehearsal record, over the values as stored in the graph (hash what
    was read back, never the Python values that were written: doubles round-trip as stored)."""
    return content_hash({"rehearsal": rehearsal_record(record)})


async def latest_rehearsal(graph: str, iid: str) -> dict | None:
    """The incident's latest rehearsal record (the one shown and sealed), or None."""
    res = await query(graph, LATEST_REHEARSAL_Q, {"iid": iid}, read_only=True)
    if not res.result_set:
        return None
    return rehearsal_record(dict(res.result_set[0][0].properties))


# --- the registry tool -----------------------------------------------------------------


class HypothesisArgs(ToolInput):
    kind: KindName = Field(
        description=(
            "suspect_qualifies: the entity passes every KTD23 clause; "
            "suspect_necessary: no rival qualifies without it; "
            "no_other_qualifying: absence certificate finds no other qualifying entity; "
            "entity_refuted: the entity fails a KTD23 clause"
        )
    )
    entity_id: str = Field(min_length=1, max_length=128, description="Change or Expiry id")
    claim: str = Field(min_length=1, max_length=500, description="The claim, in one sentence")


@register(
    HypothesisArgs,
    name="test_hypothesis",
    target="ledger",
    read_only=False,
    with_context=True,
)
async def test_hypothesis_tool(
    graph: str, *, ctx: ToolContext, kind: str, entity_id: str, claim: str
) -> ToolResult:
    """Pre-register a hypothesis in the proof ledger, then run its falsifier and chain the
    proof (states: HOLDS or REFUTED). Use for every claim the verdict depends on.

    The falsifier is fixed by ``kind``; the agent only names the entity and the claim."""
    p = await test_hypothesis(ctx, kind, entity_id, claim)
    result = p.as_dict()
    result["falsifier_result"] = p.result
    return ToolResult(
        tool="test_hypothesis",
        result=result,
        cypher=p.cypher,
        params=p.params,
        nodes_touched=[p.hypothesis_id, p.id, *p.nodes_touched],
        graph=graph,
    )


test_hypothesis_tool.__test__ = False  # type: ignore[attr-defined]
