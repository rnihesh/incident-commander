"""Rollback rehearsal on a cloned world (KTD14, R16): the multiverse check.

``rehearse_rollback(incident_id, change_id)`` never writes to the snapshot it reads. It:

  1. copies the as-true snapshot to ``s:{sid}:rehearse:{iid}`` (tracked before it exists);
  2. records what the change explains *before* the rollback (the KTD23 predicate on the
     clone): the symptomatic services it reaches and the earliest onset among them;
  3. applies the rollback on the clone: the change's ``IMPACTS`` edges are deleted and its
     validity ends (``valid_to = valid_from``); the version it superseded, if any
     (``PREVIOUS_VERSION``), is current again (``valid_to = OPEN_END``);
  4. re-evaluates every other windowed candidate on the clone, twice:

       full     the KTD23 predicate against the whole symptom set (``predicate.EXPLAINS``)
       partial  the same clauses against the symptoms it reaches (``SUBSET_EXPLAINS``)

  5. deletes the clone (always, also when a step fails).

``cleared`` holds iff the change explained the symptoms before the rollback, no remaining
candidate qualifies for the full symptom set, and none still explains part of what the
rolled-back change explained. Partial clauses, first failure reported:

  overlap      it reaches a symptomatic service the rolled-back change explained
  after_onset  its event precedes the earliest onset among the symptoms it reaches
  origin       its own service is symptomatic and among the first of those to fail
  independent  its origin is not a downstream echo of the rolled-back fault: its own
               service is not one the rolled-back change explained, or it started failing
               no later than the earliest symptom the rolled-back change explained
  order        nearer hops start no later (within the symptoms it reaches)
  signature    its kind can produce every signature among the symptoms it reaches

``independent`` is what separates a second fault from the timing-matched decoys every
injection writes on symptomatic callers of the root (KTD12): on their own branch those
decoys look like a cause, but their service failed after the root's and is downstream of
it, so propagation from the rolled-back change already accounts for them.

Every Cypher statement (writes on the clone included) is returned in order, so the
rehearsal replays on a fresh copy of the snapshot. ``qualifies_subset`` is the pure-Python
twin of ``SUBSET_EXPLAINS`` (tests hold the two to agreement, like ``world.causality``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from textwrap import dedent
from typing import Any, Protocol

import networkx as nx
from pydantic import Field

from ic.graph import naming
from ic.graph.client import copy_graph, delete_graph, query
from ic.graph.schema import COMPATIBLE_SIGNATURES, OPEN_END
from ic.locks import KeyedLocks
from ic.tools import predicate as P
from ic.tools.investigate import _path
from ic.tools.registry import ScopeError, ToolContext, ToolInput, register
from ic.tools.result import QueryLog, ToolResult

SUBSET_CLAUSES = ("overlap", "after_onset", "origin", "independent", "order", "signature")

# The KTD23 clauses on the symptoms the entity reaches. {label} is Change or Expiry.
SUBSET_EXPLAINS = """
MATCH (c:{label} {{id: $entity_id}})-[:CHANGES|EXPIRES_ON]->(home:Service)
MATCH (:Incident {{id: $incident_id}})-[:HAS]->(sym:Symptom)-[:ON]->(s:Service)
WITH c, home, sym, s, shortestPath((c)-[:IMPACTS*]->(s)) AS path
WITH c, home,
     CASE WHEN c:Expiry THEN c.valid_to ELSE c.valid_from END AS event_time,
     collect({{service: s.id, onset: sym.onset, signature: sym.signature,
              hop: length(path)}}) AS syms
WITH c, home, event_time, [x IN syms WHERE x.hop IS NOT NULL] AS sub
WITH c, home, event_time, sub,
     reduce(m = null, x IN sub |
            CASE WHEN m IS NULL OR x.onset < m THEN x.onset ELSE m END) AS earliest,
     reduce(m = null, x IN sub |
            CASE WHEN x.service = home.id AND (m IS NULL OR x.onset < m)
                 THEN x.onset ELSE m END) AS home_onset
WITH c, home, event_time, sub, earliest, home_onset,
     [x IN sub WHERE x.service IN $explained | x.service] AS overlap,
     [a IN sub WHERE any(b IN sub WHERE a.hop < b.hop AND a.onset > b.onset)] AS out_of_order,
     [x IN sub WHERE NOT x.signature IN coalesce($compatible[c.kind], []) | x.signature]
       AS bad_signatures
RETURN c.id AS id, c.kind AS kind, home.id AS service_id, event_time, earliest, home_onset,
       sub, overlap, bad_signatures,
       CASE
         WHEN size(overlap) = 0 THEN 'overlap'
         WHEN event_time >= earliest THEN 'after_onset'
         WHEN home_onset IS NULL OR home_onset > earliest THEN 'origin'
         WHEN home.id IN $explained AND home_onset > $anchor_onset THEN 'independent'
         WHEN size(out_of_order) > 0 THEN 'order'
         WHEN size(bad_signatures) > 0 THEN 'signature'
       END AS failed_clause
"""

# Rollback on the clone, in order. {label} is Change or Expiry.
END_IMPACTS = "MATCH (c:{label} {{id: $entity_id}})-[e:IMPACTS]->() DELETE e"
END_CHANGE = (
    "MATCH (c:Change {id: $entity_id}) "
    "SET c.valid_to = c.valid_from, c.rolled_back = true RETURN c.valid_from"
)
END_EXPIRY = "MATCH (c:Expiry {id: $entity_id}) SET c.renewed = true RETURN c.valid_to"
RESTORE_PREVIOUS = (
    "MATCH (:Change {id: $entity_id})-[:PREVIOUS_VERSION]->(q:Change) "
    "SET q.valid_to = $open_end RETURN q.id, q.value"
)

_locks: KeyedLocks[str] = KeyedLocks()


class RehearseArgs(ToolInput):
    incident_id: str = Field(min_length=1, max_length=128, description="Incident id, e.g. inc-007")
    change_id: str = Field(
        min_length=1, max_length=128, description="Change (or Expiry) to roll back"
    )


# --- the pure-Python twin ----------------------------------------------------------------


class _Entity(Protocol):
    id: str
    service_id: str
    event_time: int
    sig_kind: str


class _Symptom(Protocol):
    service_id: str
    onset: int
    signature: str


def qualifies_subset(
    entity: _Entity,
    g: nx.DiGraph,
    symptoms: list[_Symptom],
    explained: set[str],
    anchor_onset: int | None,
) -> tuple[bool, str | None, list[str]]:
    """(partially explains, first failing clause or None, overlapping services), with the
    same clauses as ``SUBSET_EXPLAINS``; ``g`` is the post-rollback IMPACTS graph."""
    hops = nx.single_source_shortest_path_length(g, entity.id) if entity.id in g else {}
    sub = [s for s in symptoms if s.service_id in hops]
    overlap = sorted({s.service_id for s in sub if s.service_id in explained})
    if not overlap:
        return False, "overlap", overlap
    earliest = min(s.onset for s in sub)
    if entity.event_time >= earliest:
        return False, "after_onset", overlap
    home = [s.onset for s in sub if s.service_id == entity.service_id]
    if not home or min(home) > earliest:
        return False, "origin", overlap
    if entity.service_id in explained and anchor_onset is not None and min(home) > anchor_onset:
        return False, "independent", overlap
    for a in sub:
        for b in sub:
            if hops[a.service_id] < hops[b.service_id] and a.onset > b.onset:
                return False, "order", overlap
    allowed = {str(x) for x in COMPATIBLE_SIGNATURES.get(entity.sig_kind, ())}
    if {s.signature for s in sub} - allowed:
        return False, "signature", overlap
    return True, None, overlap


# --- the tool ----------------------------------------------------------------------------


@dataclass
class _Partial:
    ok: bool
    clause: str | None
    services: list[str]  # overlap with what the rolled-back change explained
    hops: dict[str, int]  # reached symptomatic service -> hop
    detail: str
    query: int


@dataclass
class _Rollback:
    label: str
    impacts_removed: int = 0
    previous_version: str | None = None
    restored_value: Any = None
    queries: list[int] = field(default_factory=list)


async def _write(log: QueryLog, cypher: str, params: dict) -> Any:
    """A write on the clone, recorded in the tool's evidence like every read."""
    log.cypher.append(dedent(cypher).strip())
    log.params.append(dict(params))
    return await query(log.graph, log.cypher[-1], log.params[-1])


async def _rollback(log: QueryLog, label: str, entity_id: str) -> _Rollback:
    out = _Rollback(label)
    params = {"entity_id": entity_id}
    res = await _write(log, END_IMPACTS.format(label=label), params)
    out.impacts_removed = int(getattr(res, "relationships_deleted", 0) or 0)
    out.queries.append(log.last_index)
    await _write(log, END_CHANGE if label == "Change" else END_EXPIRY, params)
    out.queries.append(log.last_index)
    if label == "Change":
        res = await _write(log, RESTORE_PREVIOUS, {**params, "open_end": OPEN_END})
        out.queries.append(log.last_index)
        if res.result_set:
            out.previous_version, out.restored_value = res.result_set[0]
    return out


async def partial(
    log: QueryLog,
    incident_id: str,
    entity_id: str,
    label: str,
    explained: list[str],
    anchor_onset: int | None,
) -> _Partial:
    """``SUBSET_EXPLAINS`` for one candidate on the (rolled-back) clone."""
    params = {
        "entity_id": entity_id,
        "incident_id": incident_id,
        "explained": explained,
        "anchor_onset": anchor_onset,
        "compatible": P.COMPATIBLE,
    }
    rows = await log.rows(SUBSET_EXPLAINS.format(label=label), params)
    q = log.last_index
    if not rows:
        return _Partial(False, "overlap", [], {}, f"{entity_id} reaches no symptom", q)
    _, kind, home, event_time, earliest, home_onset, sub, overlap, bad, clause = rows[0]
    hops = {x["service"]: x["hop"] for x in sorted(sub, key=lambda x: x["service"])}
    services = sorted(set(overlap))
    detail = {
        None: f"still explains {', '.join(services)}",
        "overlap": "reaches none of the symptoms the rolled-back change explained",
        "after_onset": f"event at {event_time} is not before onset {earliest}",
        "origin": f"{home} shows no symptom"
        if home_onset is None
        else f"{home} is not the first of the services it reaches to fail",
        "independent": f"{home} failed after the rolled-back change's origin "
        f"({home_onset} > {anchor_onset}): a downstream echo",
        "order": "onsets out of hop order on the symptoms it reaches",
        "signature": f"{kind} cannot produce {', '.join(sorted(set(bad)))}",
    }.get(clause, clause)
    return _Partial(clause is None, clause, services, hops, detail, q)


def _farthest(hops: dict[str, int | None], among: list[str]) -> str | None:
    reached = [(h, s) for s, h in hops.items() if h is not None and s in among]
    return max(reached)[1] if reached else None


def _scope(ctx: ToolContext, graph: str, incident_id: str) -> tuple[str, str]:
    if not ctx.sid:
        raise ScopeError("rehearse_rollback needs a session to clone the world into")
    source = ctx.snapshot_true or graph  # KTD14: rehearse on what was actually true at t
    clone = naming.rehearse(ctx.sid, incident_id)
    for g in (source, clone):
        if not ctx.allows(g):
            raise ScopeError(f"rehearse_rollback may not touch {g} from this run's scope")
    return source, clone


@register(RehearseArgs, with_context=True, read_only=False)
async def rehearse_rollback(
    graph: str, *, ctx: ToolContext, incident_id: str, change_id: str
) -> ToolResult:
    """Rehearse rolling back a change on a cloned as-true snapshot and re-check every
    candidate: ``cleared`` only if nothing left explains the symptoms, in full or in part.

    The snapshot itself is never written; the clone is deleted before the tool returns."""
    source, clone = _scope(ctx, graph, incident_id)
    lock = _locks(clone)
    async with lock:
        if ctx.track_graph is not None:
            ctx.track_graph(clone)
        try:
            return await _rehearse(source, clone, incident_id, change_id)
        finally:
            await delete_graph(clone)


async def _rehearse(source: str, clone: str, incident_id: str, change_id: str) -> ToolResult:
    t0 = time.perf_counter()
    await copy_graph(source, clone)
    log = QueryLog(clone)
    label = await P.entity_label(log, change_id, P.ENTITY_LABELS)
    facts = await P.incident_facts(log, incident_id)
    lo, hi = facts.window(P.DEFAULT_WINDOW_MINUTES)

    # What the change explains before the rollback (KTD23 on the clone).
    before = await P.explain(log, facts, change_id, label)
    explained = sorted(s for s, h in before.hops.items() if h is not None)
    onsets = [s["onset"] for s in facts.symptoms if s["service_id"] in explained]
    anchor_onset = min(onsets) if onsets else None

    rb = await _rollback(log, label, change_id)
    clone_ms = round((time.perf_counter() - t0) * 1000, 1)

    cands = await P.candidates_in_window(log, lo, hi, exclude=[change_id])
    cands_q = log.last_index
    verdicts, still = [], []
    for c in cands:
        full = await P.explain(log, facts, c.id, c.entity)
        row: dict[str, Any] = {
            "id": c.id,
            "entity": c.entity,
            "kind": c.kind,
            "service_id": c.service_id,
            "qualifies": full.ok,
            "clause": full.clause,
            "query": full.query,
            "partial": None,
            "partial_clause": None,
            "partial_query": None,
            "services": [],
        }
        if full.ok:
            hops = {s: h for s, h in full.hops.items() if h is not None}
            row["services"] = sorted(hops)
            detail = "qualifies for the full symptom set"
        else:
            pr = await partial(log, incident_id, c.id, c.entity, explained, anchor_onset)
            hops, detail = pr.hops, pr.detail
            row.update(
                partial=pr.ok,
                partial_clause=pr.clause,
                partial_query=pr.query,
                services=pr.services,
            )
        row["detail"] = detail
        verdicts.append(row)
        if full.ok or row["partial"]:
            target = _farthest(hops, row["services"])
            path = await _path(log, c.entity, c.id, target) if target else None
            still.append(
                {
                    "entity_id": c.id,
                    "entity": c.entity,
                    "kind": c.kind,
                    "service_id": c.service_id,
                    "full": full.ok,
                    "services": row["services"],
                    "path": (path or {}).get("path") or [c.id],
                    "query": row["query"] if full.ok else row["partial_query"],
                    "path_query": (path or {}).get("query"),
                }
            )

    cleared = before.ok and not still
    if not before.ok:
        detail = (
            f"{change_id} does not explain the symptoms (clause {before.clause}: "
            f"{before.detail}); rolling it back cannot be shown to clear the fault."
        )
    elif still:
        s0 = still[0]
        detail = (
            f"After rolling back {change_id}, {s0['entity_id']} still explains "
            f"{'every symptom' if s0['full'] else ', '.join(s0['services'])} via "
            f"{' > '.join(s0['path'])}."
        )
        if len(still) > 1:
            detail += f" {len(still) - 1} more remaining explanation(s)."
    else:
        prev = f", restored {rb.previous_version}" if rb.previous_version else ""
        detail = (
            f"Cloned the as-true snapshot, ended {change_id}{prev}. None of {len(cands)} "
            f"remaining candidates explains any of the {len(facts.symptoms)} symptoms."
        )
    result = {
        "incident_id": incident_id,
        "change_id": change_id,
        "cleared": cleared,
        "still_explains": still,
        "checked": len(cands),
        "clone_ms": clone_ms,
        "clone": clone,
        "source": source,
        "window": {"from": lo, "to": hi},
        "before": {
            "qualifies": before.ok,
            "clause": before.clause,
            "explained": explained,
            "anchor_onset": anchor_onset,
            "query": before.query,
        },
        "rollback": {
            "label": rb.label,
            "impacts_removed": rb.impacts_removed,
            "previous_version": rb.previous_version,
            "restored_value": rb.restored_value,
            "queries": rb.queries,
        },
        "candidates_query": cands_q,
        "verdicts": verdicts,
        "detail": detail,
    }
    touched = [incident_id, change_id, rb.previous_version, *(c.id for c in cands)]
    for s in still:
        touched += s["path"]
    return log.result("rehearse_rollback", result, touched + facts.symptomatic_services)


def surviving_text(still: list[dict]) -> list[str]:
    """One line per surviving explanation, for pages and the ledger record."""
    out = []
    for s in still:
        what = "every symptom" if s["full"] else ", ".join(s["services"])
        out.append(f"{s['entity_id']} explains {what} via {' > '.join(s['path'])}")
    return out
