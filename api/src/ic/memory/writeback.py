"""Memory write-back (U13, KTD8, R20): what a resolved incident teaches the session world.

Runs at RESOLVED (after a CLEARED rehearsal) or after a judge reveal ``match`` (mid-run only
from RESOLVED, or ACTING / PR_OPEN after a CLEARED rehearsal); never after INCONCLUSIVE,
REFUSED or FAILED. Into the SESSION WORLD (never ``seed:world``) it writes:

  * ``(:Incident)`` ``status = 'resolved'``, ``resolved_at``, ``-[:CAUSED_BY]->`` the root;
  * a rollback ``(:Change)`` of the root's kind restoring the previous value (``valid_from``
    = resolution time, ``PREVIOUS_VERSION`` -> root, root ``valid_to`` ends there), linked
    ``(i)-[:REMEDIATED_BY]->``; an Expiry root gets no rollback change;
  * a ``(:Postmortem)`` (generated text, embedding from ``get_embedder``) ``-[:ABOUT]->`` the
    incident, and, per newly learned coupling, an open ``(:Recommendation)`` to declare it;
  * ``LEARNED_DEPENDS_ON`` (caller -> callee) for every hop on a shortest causal path that is
    observed traffic without a DEPENDS_ON (a coupling the declared topology did not show).

Each learned-edge update is ONE atomic ``MERGE ... ON CREATE SET ... ON MATCH SET``:
created at ``INITIAL_CONFIDENCE``; a confirmation moves ``c`` to ``c + (1 - c) * 0.5``; a
contradiction (both ends symptomatic again, the rollback cleared the symptoms, and the edge
is on no shortest causal path) halves it. An incident updates an edge at most once.

Learned edges are memory: snapshots keep them, ``recall`` ranks hypotheses with them, and
nothing that produces a proof reads them (they never become IMPACTS).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Literal

from ic.agent.loop import Emit, call_emit
from ic.agent.triage import REQUIRED_KINDS, check_verdict
from ic.api.events import Agent, EventType
from ic.graph import naming
from ic.graph.client import graph_exists, query
from ic.graph.schema import OPEN_END
from ic.incidents import state as st
from ic.proof import ledger
from ic.world import text
from ic.world.embed import get_embedder

log = logging.getLogger(__name__)

INITIAL_CONFIDENCE = 0.6
CONFIRM_RATE = 0.5  # c -> c + (1 - c) * CONFIRM_RATE
CONTRADICT_FACTOR = 0.5  # c -> c * CONTRADICT_FACTOR
BLOCKED = frozenset({st.State.INCONCLUSIVE, st.State.REFUSED, st.State.FAILED})
Trigger = Literal["resolved", "reveal"]

ROOT_Q = """
MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service)
OPTIONAL MATCH (c)-[:TOUCHES]->(x)
RETURN 'Change' AS label, c.kind AS kind, c.summary AS summary, c.value AS value,
       c.prev_value AS prev_value, s.id AS service, s.name AS name, x.id AS touches
UNION ALL
MATCH (c:Expiry {id: $id})-[:EXPIRES_ON]->(s:Service)
RETURN 'Expiry' AS label, c.kind AS kind, c.summary AS summary, null AS value,
       null AS prev_value, s.id AS service, s.name AS name, null AS touches
"""

INCIDENT_Q = """
MATCH (i:Incident {id: $iid})
OPTIONAL MATCH (i)-[:HAS]->(y:Symptom)-[:ON]->(s:Service)
WITH i, y, s ORDER BY y.onset, s.id
RETURN i.title, i.opened_at, collect(s.id), collect(s.name), collect(y.signature)
"""

# Atomic claim on the world: exactly one write-back per incident.
CLAIM_Q = """
MATCH (i:Incident {id: $iid}) WHERE i.memory_written IS NULL
SET i.memory_written = true, i.status = 'resolved', i.resolved_at = $at
RETURN count(i)
"""

CAUSED_BY_Q = (
    "MATCH (i:Incident {{id: $iid}}), (c:{label} {{id: $root}}) MERGE (i)-[:CAUSED_BY]->(c)"
)

ROLLBACK_Q = """
MATCH (root:Change {id: $root})-[:CHANGES]->(s:Service), (i:Incident {id: $iid})
CREATE (rb:Change {id: $rb, kind: root.kind, service_id: s.id, summary: $summary,
                   valid_from: $at, valid_to: $open_end, recorded_at: $at,
                   value: $value, prev_value: root.value, rollback_of: root.id,
                   incident_id: $iid})
CREATE (rb)-[:CHANGES]->(s)
CREATE (rb)-[:PREVIOUS_VERSION]->(root)
CREATE (i)-[:REMEDIATED_BY]->(rb)
SET root.valid_to = $at
RETURN rb.id
"""
ROLLBACK_TOUCHES_Q = (
    "MATCH (rb:Change {{id: $rb}}), (x:{label} {{id: $touches}}) CREATE (rb)-[:TOUCHES]->(x)"
)

POSTMORTEM_Q = """
MATCH (i:Incident {id: $iid})
CREATE (pm:Postmortem {id: $id, title: $title, body: $body, symptoms_text: $symptoms,
                       written_at: $at, generated: true})
CREATE (pm)-[:ABOUT]->(i)
RETURN pm.id
"""
POSTMORTEM_EMBED_Q = "MATCH (pm:Postmortem {id: $id}) SET pm.embedding = vecf32($vec)"

RECOMMENDATION_Q = """
MATCH (pm:Postmortem {id: $pm}), (a:Service {id: $caller}), (b:Service {id: $callee})
CREATE (r:Recommendation {id: $id, text: $text, status: 'open', opened_at: $at,
                          generated: true})
CREATE (pm)-[:RECOMMENDS]->(r)
CREATE (r)-[:TARGETS]->(a)
CREATE (r)-[:TARGETS]->(b)
RETURN r.id
"""

# Hops (callee -> caller over IMPACTS) on some shortest causal path from the root to a
# symptomatic service, with whether DEPENDS_ON declares them and OBSERVED_CALL shows them.
HOPS_Q = """
MATCH (c:{label} {{id: $root}})
MATCH (:Incident {{id: $iid}})-[:HAS]->(:Symptom)-[:ON]->(s:Service)
WITH DISTINCT c, s
MATCH p = allShortestPaths((c)-[:IMPACTS*]->(s))
WITH nodes(p) AS ns
UNWIND range(0, size(ns) - 2) AS k
WITH ns[k] AS x, ns[k + 1] AS y
WHERE x:Service AND y:Service
WITH DISTINCT x, y
RETURN x.id AS callee, y.id AS caller,
       size([(y)-[:DEPENDS_ON]->(x) | 1]) > 0 AS declared,
       size([(y)-[:OBSERVED_CALL]->(x) | 1]) > 0 AS observed
"""

EXISTING_LEARNED_Q = """
MATCH (a:Service)-[l:LEARNED_DEPENDS_ON]->(b:Service)
WHERE a.id IN $symptomatic AND b.id IN $symptomatic
RETURN a.id, b.id
"""

CONFIRM_Q = """
MATCH (a:Service {id: $caller}), (b:Service {id: $callee})
MERGE (a)-[l:LEARNED_DEPENDS_ON]->(b)
ON CREATE SET l.confidence = $initial, l.confirmations = 0, l.contradictions = 0,
              l.first_learned_incident = $iid, l.first_learned_at = $at,
              l.updated_at = $at, l.last_incident = $iid, l.created = true
ON MATCH SET l.confidence = CASE WHEN l.last_incident = $iid THEN l.confidence
                                 ELSE l.confidence + (1.0 - l.confidence) * $rate END,
             l.confirmations = CASE WHEN l.last_incident = $iid THEN l.confirmations
                                    ELSE coalesce(l.confirmations, 0) + 1 END,
             l.updated_at = $at, l.last_incident = $iid, l.created = false
RETURN l.confidence, l.confirmations, l.contradictions, l.first_learned_incident, l.created
"""

CONTRADICT_Q = """
MATCH (:Service {id: $caller})-[l:LEARNED_DEPENDS_ON]->(:Service {id: $callee})
SET l.confidence = CASE WHEN l.last_incident = $iid THEN l.confidence
                        ELSE l.confidence * $factor END,
    l.contradictions = CASE WHEN l.last_incident = $iid THEN l.contradictions
                            ELSE coalesce(l.contradictions, 0) + 1 END,
    l.updated_at = $at, l.last_incident = $iid
RETURN l.confidence, l.confirmations, l.contradictions, l.first_learned_incident
"""

LAST_REHEARSAL_Q = (
    "MATCH (r:Rehearsal)-[:FOR]->(:LedgerIncident {id: $iid}) "
    "RETURN r.result ORDER BY r.at DESC LIMIT 1"
)


class WriteBackRefused(Exception):
    """The incident is not in a state that may teach memory anything."""


@dataclass
class WriteBackResult:
    incident_id: str
    trigger: str
    cause_id: str
    resolved_at: int
    rollback_change: str | None = None
    postmortem_id: str | None = None
    recommendations: list[str] = field(default_factory=list)
    learned: list[dict] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        new = [e for e in self.learned if e["action"] == "created"]
        conf = [e for e in self.learned if e["action"] == "confirmed"]
        bad = [e for e in self.learned if e["action"] == "contradicted"]
        bits = [f"{self.incident_id} resolved, caused by {self.cause_id}"]
        if self.rollback_change:
            bits.append(f"remediated by {self.rollback_change}")
        if self.postmortem_id:
            bits.append(f"postmortem {self.postmortem_id}")
        for e in new:
            bits.append(
                f"learned {e['caller']} -> {e['callee']} (confidence {e['confidence']:.2f})"
            )
        for e in conf:
            bits.append(
                f"confirmed {e['caller']} -> {e['callee']} (confidence {e['confidence']:.2f})"
            )
        for e in bad:
            bits.append(
                f"contradicted {e['caller']} -> {e['callee']} (confidence {e['confidence']:.2f})"
            )
        if not self.learned:
            bits.append("no hidden dependency on the causal path")
        return "; ".join(bits) + "."

    def payload(self) -> dict:
        """The ``memory`` event (``MemoryData`` in web/lib/payloads.ts, plus extras)."""
        return {
            "written": self.written,
            "detail": self.detail,
            "incident_id": self.incident_id,
            "trigger": self.trigger,
            "cause_id": self.cause_id,
            "resolved_at": self.resolved_at,
            "rollback_change": self.rollback_change,
            "postmortem_id": self.postmortem_id,
            "recommendations": self.recommendations,
            "learned": self.learned,
            "notes": self.notes,
        }


# --- helpers -----------------------------------------------------------------------------


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


async def _write(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params)).result_set


async def _next_id(graph: str, label: str, prefix: str, width: int) -> str:
    ids = [r[0] for r in await _rows(graph, f"MATCH (n:{label}) RETURN n.id")]
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    n = max((int(m.group(1)) for i in ids if i and (m := pat.match(str(i)))), default=0)
    return f"{prefix}-{n + 1:0{width}d}"


async def ledger_suspect(sid: str, iid: str) -> str | None:
    """The one entity whose ledger proofs pass the DIAGNOSED gate (None otherwise)."""
    chain = await ledger.chain(sid, iid)
    cands = sorted({p.entity_id for p in chain if p.kind in REQUIRED_KINDS and p.holds})
    ok = [e for e in cands if check_verdict(chain, "DIAGNOSED", e, []).ok]
    return ok[0] if len(ok) == 1 else None


async def check_eligible(sid: str, iid: str, trigger: Trigger) -> tuple[st.State | None, str]:
    """(ledger state, suspect) or ``WriteBackRefused``. Write-back needs a proven suspect;
    at RESOLVED it also needs the last rehearsal to have CLEARED."""
    lg = naming.ledger(sid)
    state = await st.get_state(lg, iid)
    if state is None:
        raise WriteBackRefused(f"{iid} has no ledger state")
    if state in BLOCKED:
        raise WriteBackRefused(f"{iid} is {state.value}: nothing is written back")
    if state == st.State.LEARNED:
        raise WriteBackRefused(f"{iid} was already written back")
    if trigger == "resolved":
        if state != st.State.RESOLVED:
            raise WriteBackRefused(f"{iid} is {state.value}, not RESOLVED")
        last = await _rows(lg, LAST_REHEARSAL_Q, {"iid": iid})
        if not last or last[0][0] != "CLEARED":
            raise WriteBackRefused(f"{iid} has no CLEARED rehearsal")
    elif trigger != "reveal":
        raise ValueError(f"unknown trigger {trigger!r}")
    suspect = await ledger_suspect(sid, iid)
    if suspect is None:
        raise WriteBackRefused(f"the ledger does not prove a single suspect for {iid}")
    return state, suspect


async def causal_hops(snapshot: str, iid: str, label: str, root: str) -> list[dict]:
    rows = await _rows(snapshot, HOPS_Q.format(label=label), {"iid": iid, "root": root})
    return [
        {"callee": callee, "caller": caller, "declared": bool(d), "observed": bool(o)}
        for callee, caller, d, o in rows
    ]


def plan_updates(
    hops: list[dict], existing: set[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(confirm, contradict) as (caller, callee) pairs.

    Confirm: every hop on a shortest causal path that is observed traffic without a
    DEPENDS_ON (created when new), and every existing learned edge on such a path.
    Contradict: an existing learned edge whose two ends were both symptomatic (``existing``
    holds only those) while the cleared explanation used no path through it."""
    on_path = {(h["caller"], h["callee"]) for h in hops}
    hidden = {(h["caller"], h["callee"]) for h in hops if h["observed"] and not h["declared"]}
    return sorted(hidden | (existing & on_path)), sorted(existing - on_path)


async def confirm_edge(world: str, caller: str, callee: str, iid: str, at: int) -> dict:
    rs = await _write(
        world,
        CONFIRM_Q,
        {
            "caller": caller,
            "callee": callee,
            "iid": iid,
            "at": at,
            "initial": INITIAL_CONFIDENCE,
            "rate": CONFIRM_RATE,
        },
    )
    conf, n_ok, n_bad, first, created = rs[0]
    return {
        "caller": caller,
        "callee": callee,
        "action": "created" if created else "confirmed",
        "confidence": round(float(conf), 6),
        "confirmations": int(n_ok),
        "contradictions": int(n_bad),
        "first_learned_incident": first,
    }


async def contradict_edge(world: str, caller: str, callee: str, iid: str, at: int) -> dict:
    rs = await _write(
        world,
        CONTRADICT_Q,
        {"caller": caller, "callee": callee, "iid": iid, "at": at, "factor": CONTRADICT_FACTOR},
    )
    conf, n_ok, n_bad, first = rs[0]
    return {
        "caller": caller,
        "callee": callee,
        "action": "contradicted",
        "confidence": round(float(conf), 6),
        "confirmations": int(n_ok or 0),
        "contradictions": int(n_bad or 0),
        "first_learned_incident": first,
    }


def _postmortem_text(
    iid: str,
    names: list[str],
    sigs: list[str],
    root: dict,
    remediation: str,
    minutes: int,
    coupling: tuple[str, str] | None,
) -> tuple[str, str, str]:
    rng = random.Random(f"postmortem:{iid}")
    cause_kind = str(root["kind"] or "deploy")
    if cause_kind not in text.CAUSE_PHRASES:
        cause_kind = "deploy"
    sigs = [s for s in sigs if s in text.SIGNATURE_PHRASES] or ["error_5xx"]
    facts = text.PostmortemFacts(
        root=names[0] if names else root["name"],
        others=list(dict.fromkeys(names[1:])),
        signatures=sigs,
        cause_kind=cause_kind,
        cause_service=root["name"] or root["service"],
        detail=root["summary"] or root["kind"] or "",
        remediation=remediation,
        minutes=minutes,
        coupling=coupling,
    )
    return text.postmortem(rng, facts)


async def _embed(texts: list[str]) -> list[float] | None:
    try:
        vecs = await asyncio.to_thread(get_embedder().embed, texts)
        return vecs[0]
    except Exception:  # an embedding outage must not lose the rest of the write-back
        log.warning("postmortem embedding failed", exc_info=True)
        return None


# --- the write-back ----------------------------------------------------------------------


async def write_back(
    *,
    sid: str,
    iid: str,
    resolved_at: int,
    trigger: Trigger = "resolved",
    emit: Emit | None = None,
    snapshot: str | None = None,
) -> WriteBackResult:
    """Write the resolved incident into the session world and emit a ``memory`` event.

    Raises ``WriteBackRefused`` (nothing written) unless the incident qualifies; see the
    module docstring. ``snapshot`` defaults to the run's as-known snapshot (the view the
    proofs used), falling back to the as-true one."""
    state, cause_id = await check_eligible(sid, iid, trigger)
    world = naming.session_world(sid)
    at = int(resolved_at)
    roots = await _rows(world, ROOT_Q, {"id": cause_id})
    if not roots:
        raise WriteBackRefused(f"{cause_id} is not in the session world")
    label, kind, summary, value, prev, svc, svc_name, touches = roots[0]
    root = {"kind": kind, "summary": summary, "service": svc, "name": svc_name}
    claimed = await _write(world, CLAIM_Q, {"iid": iid, "at": at})
    if not claimed or claimed[0][0] != 1:
        raise WriteBackRefused(f"{iid} was already written back (or is not in the world)")

    out = WriteBackResult(iid, trigger, cause_id, at)
    out.written.append(f"Incident {iid}: status resolved, resolved_at {at}")
    await _write(world, CAUSED_BY_Q.format(label=label), {"iid": iid, "root": cause_id})
    out.written.append(f"({iid})-[:CAUSED_BY]->({cause_id})")

    # Rollback change: same kind, restores the previous value, valid from the resolution.
    remediation = f"rolled back {cause_id}"
    if label == "Change":
        rb = await _next_id(world, "Change", "chg", 5)
        restore = prev if prev is not None else ""
        rb_summary = f"Rollback {summary or cause_id} (restore {restore or 'previous'})"
        await _write(
            world,
            ROLLBACK_Q,
            {
                "root": cause_id,
                "iid": iid,
                "rb": rb,
                "summary": rb_summary,
                "at": at,
                "open_end": OPEN_END,
                "value": restore,
            },
        )
        if touches and kind in ("config", "flag"):
            target = "Config" if kind == "config" else "Flag"
            await _write(
                world, ROLLBACK_TOUCHES_Q.format(label=target), {"rb": rb, "touches": touches}
            )
        out.rollback_change = rb
        remediation = rb_summary
        out.written.append(f"({iid})-[:REMEDIATED_BY]->({rb}) {rb_summary}")
    else:
        out.notes.append(f"{cause_id} is an {label}: no rollback change to record")

    # Learned couplings, from the causal paths on the snapshot the proofs used.
    title, opened_at, sym_ids, sym_names, sigs = (await _rows(world, INCIDENT_Q, {"iid": iid}))[0]
    symptomatic = sorted(set(sym_ids))
    snap = snapshot
    if snap is None:
        for v in ("known", "true"):
            name = naming.asof(sid, iid, v)
            if await graph_exists(name):
                snap = name
                break
    coupling_names: tuple[str, str] | None = None
    new_edges: list[dict] = []
    if snap is None:
        out.notes.append("no as-of snapshot left: learned dependencies not updated")
    else:
        hops = await causal_hops(snap, iid, label, cause_id)
        existing = {
            (a, b) for a, b in await _rows(world, EXISTING_LEARNED_Q, {"symptomatic": symptomatic})
        }
        confirm, contradict = plan_updates(hops, existing)
        for caller, callee in confirm:
            e = await confirm_edge(world, caller, callee, iid, at)
            out.learned.append(e)
            if e["action"] == "created":
                new_edges.append(e)
        for caller, callee in contradict:
            out.learned.append(await contradict_edge(world, caller, callee, iid, at))
        for e in out.learned:
            out.written.append(
                f"({e['caller']})-[:LEARNED_DEPENDS_ON {{confidence: {e['confidence']:.3f}}}]->"
                f"({e['callee']}) {e['action']}"
            )
        names = {
            r[0]: r[1]
            for r in await _rows(
                world,
                "MATCH (s:Service) WHERE s.id IN $ids RETURN s.id, s.name",
                {"ids": sorted({x for e in out.learned for x in (e["caller"], e["callee"])})},
            )
        }
        firm = [e for e in out.learned if e["action"] != "contradicted"]
        if firm:
            coupling_names = (
                names.get(firm[0]["caller"], firm[0]["caller"]),
                names.get(firm[0]["callee"], firm[0]["callee"]),
            )

    # Postmortem (generated) with an embedding for precedent search.
    minutes = max(0, (at - int(opened_at or at)) // 60)
    pm_title, body, symptoms_text = _postmortem_text(
        iid, list(sym_names), list(sigs), root, remediation, minutes, coupling_names
    )
    pm_title = title or pm_title
    pm = await _next_id(world, "Postmortem", "pm", 3)
    await _write(
        world,
        POSTMORTEM_Q,
        {
            "iid": iid,
            "id": pm,
            "title": pm_title,
            "body": body,
            "symptoms": symptoms_text,
            "at": at,
        },
    )
    vec = await _embed([f"{pm_title}\n{symptoms_text}\n{body}"])
    if vec is not None:
        await _write(world, POSTMORTEM_EMBED_Q, {"id": pm, "vec": vec})
    out.postmortem_id = pm
    out.written.append(f"(:Postmortem {pm})-[:ABOUT]->({iid})")

    # The postmortem recommends declaring each newly learned dependency.
    for e in new_edges:
        rid = await _next_id(world, "Recommendation", "rec", 3)
        rec_text = (
            f"Declare the dependency of {e['caller']} on {e['callee']} in the service "
            f"catalog: it carried {iid}'s failure but only showed up in observed traffic"
        )
        await _write(
            world,
            RECOMMENDATION_Q,
            {
                "pm": pm,
                "caller": e["caller"],
                "callee": e["callee"],
                "id": rid,
                "text": rec_text,
                "at": at,
            },
        )
        out.recommendations.append(rid)
        out.written.append(f"(:Postmortem {pm})-[:RECOMMENDS]->(:Recommendation {rid})")

    if state == st.State.RESOLVED:
        await st.transition(naming.ledger(sid), iid, st.State.LEARNED)
    await call_emit(emit, EventType.MEMORY, out.payload(), Agent.SYSTEM)
    if state == st.State.RESOLVED:
        await call_emit(emit, EventType.STATE, {"state": st.State.LEARNED.value}, Agent.SYSTEM)
    return out


# While the run is still in flight, a reveal may only write back from these states (with
# the latest rehearsal CLEARED where the state does not imply it).
REVEAL_IN_FLIGHT = frozenset({st.State.ACTING, st.State.PR_OPEN, st.State.RESOLVED})


async def reveal_may_write(sid: str, iid: str, run_finished: bool) -> bool:
    """A reveal opens at the verdict, while the run may still be rehearsing: writing then
    would teach memory from an incident that can still end REFUSED. Mid-run, only RESOLVED,
    or ACTING / PR_OPEN after a CLEARED rehearsal, qualify; anything else is deferred to the
    run's own RESOLVED write-back."""
    if run_finished:
        return True
    lg = naming.ledger(sid)
    state = await st.get_state(lg, iid)
    if state not in REVEAL_IN_FLIGHT:
        return False
    if state == st.State.RESOLVED:
        return True
    last = await _rows(lg, LAST_REHEARSAL_Q, {"iid": iid})
    return bool(last) and last[0][0] == "CLEARED"


async def write_back_after_reveal(
    *,
    sid: str,
    iid: str,
    match: bool,
    resolved_at: int,
    emit: Emit | None = None,
    run_finished: bool = False,
) -> WriteBackResult | None:
    """Judge mode hook (KTD8): a reveal ``match`` may teach memory even when the run did not
    reach RESOLVED. Anything but a match, or a blocked incident, writes nothing; while the
    run is in flight (``run_finished`` False) see ``reveal_may_write``."""
    if not match:
        return None
    if not await reveal_may_write(sid, iid, run_finished):
        return None
    try:
        return await write_back(
            sid=sid, iid=iid, resolved_at=resolved_at, trigger="reveal", emit=emit
        )
    except WriteBackRefused:
        return None
