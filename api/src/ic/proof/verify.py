"""Verify (KTD4, R14): re-run every HOLDS/REFUTED proof against the *current* session world.

``:verify:`` snapshots are rebuilt at the incident's alert time ``t`` and deleted afterwards.
``pr=True`` verifies the PR copies instead (the public proof page and commit status re-run,
KTD19): the ledger is ``pr:{sid}:{iid}:ledger``, the snapshots are built from
``pr:{sid}:{iid}:world`` into ``pr:{sid}:{iid}:verify:{view}``, and the live session is
never read, so an edit to the session world after the PR opened does not change the PR's
proof status.
Each proof's falsifier runs twice. The falsifier (tool + args) is *derived* from the hashed
``kind`` and ``entity_id`` exactly as ``test_hypothesis`` derives it; the copy stored on the
Hypothesis node is only compared against it (a mismatch makes that proof TAMPERED):

  as-known  (what the system knew at ``t``) -- compared by result hash
  as-true   (what was actually valid at ``t``) -- decides whether the conclusion still holds

  VERIFIED   as-known result hash unchanged and the as-true conclusion holds as recorded
  DRIFTED    as-known result changed, but the as-true conclusion holds as recorded
  FALSIFIED  the as-true conclusion flipped (a refuted entity that now qualifies included),
             or an entity the conclusion recorded as a rival / qualifying explanation no
             longer qualifies as-true (an INCONCLUSIVE verdict's evidence no longer stands)
  TAMPERED   (per proof) the stored falsifier differs from the one its kind implies

The responsible nodes are the ids that entered or left the conclusion (qualifying, rivals,
or the entity itself when its verdict flipped) followed by ids that entered or left the
nodes the falsifier touched. A responsible Change/Expiry with ``recorded_at`` after ``t``
or missing (a hand edit), or a Service with such a DEPENDS_ON edge, is late-arriving.

Chain integrity is recomputed from the stored proofs (links, evidence and hypothesis
hashes), rooted at a genesis hash over ``sid:iid:t:world``. The SQLite anchor (API-owned;
the ledger graph is editable by anyone with Browser access) holds the head hash and the
``t``/``world`` the chain was proven against; Verify replays at the anchored ``t``/``world``.
``pr=True`` uses the anchor frozen when the PR copies were made (``pr_anchor``), never the
session's ``chain_anchor``: a later re-anchor of the session cannot move a PR's proof.

The latest ``(:Rehearsal)`` record is outside the chain; its content hash is sealed in the
store when remediation writes it (``rehearsal_seal``; ``pr_anchor.rehearsal_hash`` for the
PR copy). A sealed record that is missing or differs, or a record that was never sealed, is
TAMPERED.

One Verify per (sid, iid, pr) at a time, process-wide: every caller (the console route,
remediation's Verify-before-acting, the proof page) builds and deletes the same
``:verify:`` graph names, so they serialize on a module lock for the whole build, check and
delete. ``track_graph`` (when given) records those names for the session cleanup before
they are built.

Overall status, worst first::

  TAMPERED    broken link, anchor mismatch, ledger t/world differ from the anchor, an
              unparseable ledger node, an edited or unsealed rehearsal record, or any proof
              TAMPERED
  FALSIFIED   any proof FALSIFIED
  UNANCHORED  no anchor row for (sid, iid): the chain may be fabricated wholesale, so it
              can never be VERIFIED or DRIFTED
  DRIFTED     any proof DRIFTED
  VERIFIED    every proof VERIFIED, chain intact and matching the anchor
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from pydantic import BaseModel, Field

from ic.actions import pr_store
from ic.graph import naming
from ic.graph.client import query
from ic.graph.snapshots import build_snapshots, delete_snapshots
from ic.locks import KeyedLocks
from ic.proof import ledger as L
from ic.proof.canonical import canonical_json, content_hash
from ic.store import Store
from ic.tools.registry import ScopeError, ToolContext, call_tool
from ic.tools.result import ToolError, ToolResult

Status = Literal["VERIFIED", "DRIFTED", "UNANCHORED", "FALSIFIED", "TAMPERED"]
_RANK = {"VERIFIED": 0, "DRIFTED": 1, "UNANCHORED": 2, "FALSIFIED": 3, "TAMPERED": 4}
# Conclusion keys that list the entities a REFUTED proof showed qualifying.
ENTITY_KEYS = ("rivals", "qualifying")

_locks: KeyedLocks[tuple[str, str, bool]] = KeyedLocks()


def _lock(sid: str, iid: str, pr: bool) -> AbstractAsyncContextManager[None]:
    return _locks((sid, iid, pr))


class ProofCheck(BaseModel):
    seq: int
    proof_id: str
    kind: str
    entity_id: str
    claim: str
    recorded_state: str  # HOLDS | REFUTED
    status: Status
    responsible: list[str] = Field(default_factory=list)
    late_arriving: bool = False
    late_ids: list[str] = Field(default_factory=list)
    recorded_conclusion: dict
    known_conclusion: dict | None = None
    true_conclusion: dict | None = None
    hash_matches: bool = False
    error: str | None = None
    tampered_reason: str | None = None


class HypothesisSummary(BaseModel):
    seq: int
    hypothesis_id: str
    kind: str
    entity_id: str
    claim: str
    state: str


class VerifyReport(BaseModel):
    sid: str
    incident_id: str
    t: int
    status: Status
    chain_ok: bool
    broken_at: int | None = None
    anchor_ok: bool | None = None  # None: never anchored
    stored_head: str | None = None
    recomputed_head: str | None = None
    anchored_head: str | None = None
    rehearsal_ok: bool | None = None  # None: no rehearsal recorded (and none sealed)
    proofs: list[ProofCheck]
    tampered_reasons: list[str] = Field(default_factory=list)
    abandoned: list[HypothesisSummary] = Field(default_factory=list)
    pending: list[HypothesisSummary] = Field(default_factory=list)
    snapshot_ms: float = 0.0
    ms: float = 0.0


async def _rerun(tool: str, args: dict, ctx: ToolContext) -> tuple[ToolResult | None, str | None]:
    try:
        return await call_tool(tool, args, ctx), None
    except ToolError as e:
        return None, str(e)


def _conclusion_ids(c: dict | None) -> set[str]:
    if not c:
        return set()
    return {i for v in c.values() if isinstance(v, list) for i in v if isinstance(i, str)}


async def _late(world: str, t: int, ids: list[str]) -> list[str]:
    if not ids:
        return []
    p = {"ids": ids, "t": t}
    late: set[str] = set()
    for label in ("Change", "Expiry"):
        res = await query(
            world,
            f"MATCH (n:{label}) WHERE n.id IN $ids "
            "AND (n.recorded_at IS NULL OR n.recorded_at > $t) RETURN n.id",
            p,
            read_only=True,
        )
        late |= {r[0] for r in res.result_set}
    res = await query(
        world,
        "MATCH (s:Service)-[e:DEPENDS_ON]-(:Service) WHERE s.id IN $ids "
        "AND (e.recorded_at IS NULL OR e.recorded_at > $t) RETURN DISTINCT s.id",
        p,
        read_only=True,
    )
    late |= {r[0] for r in res.result_set}
    return sorted(late)


def _lost_entities(recorded: dict, now: dict | None) -> set[str]:
    """Entities the recorded conclusion listed as rivals / qualifying that ``now`` lacks."""
    lost: set[str] = set()
    for key in ENTITY_KEYS:
        if isinstance(recorded.get(key), list):
            lost |= set(recorded[key]) - set((now or {}).get(key) or [])
    return lost


def _tampered(p: L.ProofRecord, reason: str) -> ProofCheck:
    return ProofCheck(
        seq=p.seq,
        proof_id=p.id,
        kind=p.kind,
        entity_id=p.entity_id,
        claim=p.claim,
        recorded_state=p.state,
        status="TAMPERED",
        recorded_conclusion=p.conclusion,
        tampered_reason=reason,
    )


async def _check(
    p: L.ProofRecord, iid: str, ctx_known: ToolContext, ctx_true: ToolContext, world: str, t: int
) -> ProofCheck:
    kind = L.KINDS.get(p.kind)
    if kind is None:
        return _tampered(p, f"unknown hypothesis kind {p.kind!r}")
    # The falsifier is a function of the hashed kind + entity id (+ this incident), never
    # the stored, unhashed copy: that copy is only checked against it.
    tool, args = kind.falsifier_tool, kind.args(iid, p.entity_id)
    if p.falsifier_tool != tool or canonical_json(p.falsifier_args) != canonical_json(args):
        return _tampered(
            p,
            f"stored falsifier {p.falsifier_tool}({canonical_json(p.falsifier_args)}) differs "
            f"from the one kind {p.kind!r} implies: {tool}({canonical_json(args)})",
        )
    rk, err_k = await _rerun(tool, args, ctx_known)
    rt, err_t = await _rerun(tool, args, ctx_true)
    ck = kind.conclusion(rk.result) if rk else None
    ct = kind.conclusion(rt.result) if rt else None
    hash_matches = rk is not None and content_hash(rk.result) == p.result_hash

    if ct is None or kind.holds_if(ct) != p.holds or _lost_entities(p.conclusion, ct):
        status: Status = "FALSIFIED"
    elif hash_matches:
        status = "VERIFIED"
    else:
        status = "DRIFTED"

    responsible: list[str] = []
    if status != "VERIFIED":
        recorded = _conclusion_ids(p.conclusion)
        now = _conclusion_ids(ct) | _conclusion_ids(ck)
        first = recorded ^ now
        if ct is None or ct.get("ok") != p.conclusion.get("ok"):
            first.add(p.entity_id)
        old = set(p.nodes_touched)
        new = set(rt.nodes_touched if rt else []) | set(rk.nodes_touched if rk else [])
        rest = (old ^ new) - first
        responsible = sorted(first) + sorted(rest)
    late_ids = await _late(world, t, responsible)
    return ProofCheck(
        seq=p.seq,
        proof_id=p.id,
        kind=p.kind,
        entity_id=p.entity_id,
        claim=p.claim,
        recorded_state=p.state,
        status=status,
        responsible=responsible,
        late_arriving=bool(late_ids),
        late_ids=late_ids,
        recorded_conclusion=p.conclusion,
        known_conclusion=ck,
        true_conclusion=ct,
        hash_matches=hash_matches,
        error=err_t or err_k,
    )


def _summary(h: L.HypothesisRecord) -> HypothesisSummary:
    return HypothesisSummary(
        seq=h.seq,
        hypothesis_id=h.id,
        kind=h.kind,
        entity_id=h.entity_id,
        claim=h.claim,
        state=h.state,
    )


def _trusted_frame(info: dict, anchor: dict | None) -> tuple[Any, Any, list[str]]:
    """The (t, world) to replay at: the anchored values when the anchor has them (legacy
    rows do not; the genesis hash still binds the ledger's copies to the anchored head)."""
    t, world, reasons = info.get("t"), info.get("world"), []
    if anchor is not None:
        if anchor.get("t") is not None:
            if t != anchor["t"]:
                reasons.append(f"ledger t={t!r} differs from the anchored t={anchor['t']}")
            t = anchor["t"]
        if anchor.get("world") is not None:
            if world != anchor["world"]:
                reasons.append(f"ledger world={world!r} differs from the anchored world")
            world = anchor["world"]
    return t, world, reasons


async def _rehearsal_check(graph: str, iid: str, sealed: str | None) -> tuple[bool | None, str]:
    """(ok, reason): the latest rehearsal record against its sealed content hash."""
    rec = await L.latest_rehearsal(graph, iid)
    if rec is None:
        if sealed is None:
            return None, ""
        return False, "the sealed rehearsal record is missing from the ledger"
    if sealed is None:
        return False, f"rehearsal record {rec.get('id')!r} was never sealed"
    if L.rehearsal_hash(rec) != sealed:
        return False, f"rehearsal record {rec.get('id')!r} differs from the one sealed"
    return True, ""


async def verify_incident(
    sid: str,
    iid: str,
    store: Store,
    *,
    pr: bool = False,
    track_graph: Callable[[str], Any] | None = None,
) -> VerifyReport:
    """Re-run every HOLDS/REFUTED proof of ``iid`` on fresh ``:verify:`` snapshots (of the
    PR copies when ``pr``; see the module docstring). Serialized per (sid, iid, pr)."""
    async with _lock(sid, iid, pr):
        return await _verify(sid, iid, store, pr=pr, track_graph=track_graph)


async def _verify(
    sid: str, iid: str, store: Store, *, pr: bool, track_graph: Callable[[str], Any] | None
) -> VerifyReport:
    t0 = time.perf_counter()
    graph = naming.pr_ledger(sid, iid) if pr else naming.ledger(sid)
    info = await L.incident_info(graph, iid)
    anchor = pr_store.pr_anchor(store, sid, iid) if pr else store.get_anchor(sid, iid)
    anchored = anchor["head_hash"] if anchor else None
    t_raw, world, reasons = _trusted_frame(info, anchor)
    if info.get("sid") != sid:
        reasons.append(f"ledger sid={info.get('sid')!r} differs from session {sid}")

    def tampered(why: str, **kw) -> VerifyReport:
        return VerifyReport(
            sid=sid,
            incident_id=iid,
            t=t_raw if isinstance(t_raw, int) else 0,
            status="TAMPERED",
            chain_ok=False,
            anchor_ok=False if anchor else None,
            anchored_head=anchored,
            proofs=[],
            tampered_reasons=[*reasons, why],
            ms=round((time.perf_counter() - t0) * 1000, 1),
            **kw,
        )

    if not isinstance(world, str) or not naming.scope_allows(world, sid=sid):
        if anchor is None:
            raise ScopeError(f"ledger incident {iid} points outside session {sid}")
        return tampered(f"world {world!r} is outside session {sid}")
    try:
        t = int(t_raw)
    except (TypeError, ValueError):
        return tampered(f"alert time {t_raw!r} is not an integer")
    try:
        full_chain = await L.chain(sid, iid, graph=graph)
        hyps = await L.hypotheses(sid, iid, graph=graph)
    except L.LedgerCorrupt as e:
        return tampered(str(e))

    genesis = L.genesis_hash(sid, iid, t, world)
    proofs = [p for p in full_chain if p.state in (L.HOLDS, L.REFUTED)]
    broken_at = L.verify_chain_integrity(full_chain, genesis)
    stored_head = full_chain[-1].hash if full_chain else None
    recomputed = L.recompute_head(full_chain, genesis)
    anchor_ok = None if anchored is None else anchored == recomputed
    if pr and anchor is None:  # no frozen anchor: UNANCHORED already says it all
        rehearsal_ok, rehearsal_why = None, ""
    else:
        sealed = (
            (anchor or {}).get("rehearsal_hash") if pr else pr_store.rehearsal_seal(store, sid, iid)
        )
        rehearsal_ok, rehearsal_why = await _rehearsal_check(graph, iid, sealed)

    # The genesis hash binds the anchored world *name*; the facts come from its PR copy.
    source = naming.pr_world(sid, iid) if pr else world
    if track_graph is not None:  # before they exist: a crash mid-Verify cannot leak them
        for v in naming.VIEWS:
            track_graph(naming.pr_verify(sid, iid, v) if pr else naming.verify(sid, iid, v))
    handles = await build_snapshots(source, sid, iid, t, kind="pr_verify" if pr else "verify")
    try:
        ctx_known = ToolContext(
            sid=sid,
            iid=iid,
            world_graph=source,
            snapshot_true=handles.true_graph,
            snapshot_known=handles.known_graph,
            view="known",
        )
        ctx_true = ToolContext(
            sid=sid,
            iid=iid,
            world_graph=source,
            snapshot_true=handles.true_graph,
            snapshot_known=handles.known_graph,
            view="true",
        )
        checks = [await _check(p, iid, ctx_known, ctx_true, source, t) for p in proofs]
    finally:
        await delete_snapshots(handles)

    chain_ok = broken_at is None
    if broken_at is not None:
        reasons.append(f"chain broken at seq {broken_at}")
    if anchor_ok is False:
        reasons.append("recomputed head differs from the anchored head")
    if rehearsal_ok is False:
        reasons.append(rehearsal_why)
    reasons += [f"seq {c.seq}: {c.tampered_reason}" for c in checks if c.tampered_reason]
    worst = max((c.status for c in checks), key=_RANK.__getitem__, default="VERIFIED")
    if reasons:
        status: Status = "TAMPERED"
    elif anchor is None:
        # Never anchored: nothing outside the editable graph vouches for this chain.
        status = max(worst, "UNANCHORED", key=_RANK.__getitem__)
    else:
        status = worst
    return VerifyReport(
        sid=sid,
        incident_id=iid,
        t=t,
        status=status,
        chain_ok=chain_ok,
        broken_at=broken_at,
        anchor_ok=anchor_ok,
        stored_head=stored_head,
        recomputed_head=recomputed,
        anchored_head=anchored,
        rehearsal_ok=rehearsal_ok,
        proofs=checks,
        tampered_reasons=reasons,
        abandoned=[_summary(h) for h in hyps if h.state == L.ABANDONED],
        pending=[_summary(h) for h in hyps if h.state == L.REGISTERED],
        snapshot_ms=handles.build_ms,
        ms=round((time.perf_counter() - t0) * 1000, 1),
    )
