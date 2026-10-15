"""Fragility map (U9, KTD9, R13): which single edit would flip a DIAGNOSED verdict.

``fragility_map(incident_id, root_id)`` starts from ``verify_claim`` on the incident's
snapshot. Only a VERIFIED root has a verdict to flip; otherwise nothing is copied. It then
tries at most ``MAX_EDITS`` single edits, each in IMPACTS terms on its own throwaway copy of
the snapshot (``naming.storm(sid, ...)``, tracked before it exists, deleted in ``finally``),
and re-runs ``verify_claim`` for the root on the copy. An edit flips the verdict when the
root is no longer VERIFIED: REFUTED on a clause, or a RIVAL because something new qualifies.

  remove_edge          each service-to-service hop of the root's canonical causal path
                       (to the alerting service, else the farthest symptom). IMPACTS a > b
                       is materialized from b's DEPENDS_ON and OBSERVED_CALL on a, and the
                       judge's world-level ``remove_edge`` (from b to a) deletes both, so the
                       copy deletes the hop and every relationship backing it; ``sources``
                       names them. The first hop (root > its service) has no tamper form.
  add_deploy_upstream  a deploy, effective ``DEPLOY_MINUTES_BEFORE_ONSET`` before the
                       earliest onset and recorded before the alert, on the first-failing
                       service at or upstream of the nearest eliminated candidate's service:
                       a Change with CHANGES and the IMPACTS edge the snapshot builder
                       derives from it
  change_timestamp     the root Change moved just past the earliest onset

Each edit carries the exact body ``POST /api/judge/{incident_id}/tamper`` accepts, so the
judge can apply it to the world and re-derive. The snapshot itself is never written.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from textwrap import dedent
from typing import Any

from pydantic import Field

from ic.graph import naming
from ic.graph.client import copy_graph, delete_graph, query
from ic.graph.schema import OPEN_END
from ic.locks import KeyedLocks
from ic.tools import predicate as P
from ic.tools.claims import verify_claim
from ic.tools.investigate import _path
from ic.tools.registry import ScopeError, ToolContext, ToolInput, register
from ic.tools.result import QueryLog, ToolResult

MAX_EDITS = 10
BUDGET_S = 20.0  # wall clock for one map, checked between edits (the session lock is held)
DEPLOY_MINUTES_BEFORE_ONSET = 5  # the tamper form's default, well inside the alert window
SHIFT_PAST_ONSET_S = 60
KNOWN_RECORD_S = 60  # as the tamper route records a deploy "known at alert"
REL_SOURCES = ("DEPENDS_ON", "OBSERVED_CALL")

ALERT_SERVICE = "MATCH (i:Incident {id: $incident_id}) RETURN i.alert_service_id"

# Which world relationships back the IMPACTS hop a > b (b depends on / calls a).
HOP_SOURCES = """
MATCH (:Service {id: $caller})-[e]->(:Service {id: $callee})
WHERE type(e) IN $types
RETURN DISTINCT type(e) ORDER BY type(e)
"""

# IMPACTS distance from one service to several others (the source itself excluded).
DISTANCES = """
MATCH (a:Service {id: $source}), (b:Service)
WHERE b.id IN $targets AND b.id <> $source
WITH a, b, shortestPath((a)-[:IMPACTS*]->(b)) AS p
WHERE p IS NOT NULL
RETURN b.id, length(p)
"""

# Edits on the copy, in IMPACTS terms.
DROP_HOP = "MATCH (:Service {id: $a})-[e:IMPACTS]->(:Service {id: $b}) DELETE e"
DROP_HOP_SOURCES = (
    "MATCH (:Service {id: $b})-[e:DEPENDS_ON|OBSERVED_CALL]->(:Service {id: $a}) DELETE e"
)
PLANT_DEPLOY = (
    "MATCH (s:Service {id: $service_id}) "
    "CREATE (c:Change {id: $id, kind: 'deploy', service_id: $service_id, summary: $summary, "
    "valid_from: $vf, valid_to: $vt, recorded_at: $rec})-[:CHANGES]->(s) "
    "CREATE (c)-[:IMPACTS]->(s)"
)
MOVE_CHANGE = "MATCH (c:Change {id: $id}) SET c.valid_from = $vf"

_locks: KeyedLocks[str] = KeyedLocks()


class FragilityArgs(ToolInput):
    incident_id: str = Field(min_length=1, max_length=128, description="Incident id, e.g. inc-007")
    root_id: str = Field(
        min_length=1, max_length=128, description="The diagnosed root cause (Change or Expiry)"
    )


@dataclass
class _Edit:
    kind: str  # the tamper action
    label: str
    statements: list[tuple[str, dict]]
    tamper: dict | None
    prediction: dict
    extra: dict[str, Any] = field(default_factory=dict)  # shown beside the edit
    created: dict[str, Any] = field(default_factory=dict)  # ids the edit creates on the copy


# --- planning (read-only, on the snapshot) -----------------------------------------------


async def _distances(log: QueryLog, source: str, targets: list[str]) -> dict[str, int]:
    out = {source: 0} if source in targets else {}
    others = sorted(set(targets) - {source})
    if others:
        rows = await log.rows(DISTANCES, {"source": source, "targets": others})
        out.update({sid: int(d) for sid, d in rows})
    return out


async def _hop_edits(log: QueryLog, path: list[str]) -> list[_Edit]:
    """One edit per service-to-service hop on the causal path."""
    edits = []
    for a, b in zip(path[1:], path[2:], strict=False):
        rows = await log.rows(HOP_SOURCES, {"caller": b, "callee": a, "types": list(REL_SOURCES)})
        sources = [r[0] for r in rows]
        note = (
            f"the tamper deletes {' and '.join(sources)} from {b} to {a}: "
            f"{'both sources' if len(sources) > 1 else 'the only source'} of this IMPACTS hop"
            if sources
            else f"no DEPENDS_ON or OBSERVED_CALL from {b} to {a}: no tamper form reaches it"
        )
        p = {"a": a, "b": b}
        edits.append(
            _Edit(
                kind="remove_edge",
                label=f"Remove the hop {a} > {b} ({b} depends on {a})",
                statements=[(DROP_HOP, p), (DROP_HOP_SOURCES, p)],
                tamper={"action": "remove_edge", "from_service_id": b, "to_service_id": a}
                if sources
                else None,
                prediction={"exact": bool(sources), "sources": sources, "note": note},
                extra={"hop": {"from": a, "to": b}},
            )
        )
    return edits


async def _deploy_edit(
    log: QueryLog,
    s: P.Screening,
    root: P.Verdict,
    alert: str | None,
    path: list[str],
) -> _Edit | None:
    """A deploy on the first-failing service at or upstream of the nearest eliminated
    candidate's service (nearest: fewest IMPACTS hops from the root's service, then
    closest in time to the root)."""
    eliminated = [v for v in s.verdicts if not v.ok and v.id != root.id and v.service_id]
    if not eliminated or root.service_id is None:
        return None
    dist = await _distances(log, root.service_id, [v.service_id for v in eliminated])
    near = sorted(
        (v for v in eliminated if v.service_id in dist),
        key=lambda v: (dist[v.service_id], abs((v.event_time or 0) - root.event_time), v.id),
    )
    earliest = s.facts.earliest_onset
    origins = sorted({x["service_id"] for x in s.facts.symptoms if x["onset"] == earliest})
    pick = None
    for v in near:
        up = {}
        for o in origins:  # how far each first-failing service is from v's service
            d = await _distances(log, o, [v.service_id])
            if v.service_id in d:
                up[o] = d[v.service_id]
        if up:
            pick = (v, min(up, key=lambda o: (up[o], o)))
            break
    if pick is None:
        if not near:
            return None
        pick = (near[0], near[0].service_id)  # tried and reported, though it cannot qualify
    cand, target = pick
    if alert and target != alert and target not in path:
        if alert not in await _distances(log, target, [alert]):
            return None  # the tamper route would refuse it: not upstream of the alert
    t = s.facts.snapshot_t if s.facts.snapshot_t is not None else s.hi
    vf = earliest - DEPLOY_MINUTES_BEFORE_ONSET * 60
    planted = "planted-deploy"
    params = {
        "service_id": target,
        "id": planted,
        "summary": "fragility: planted deploy",
        "vf": vf,
        "vt": OPEN_END,
        "rec": min(vf + KNOWN_RECORD_S, t),
    }
    return _Edit(
        kind="add_deploy_upstream",
        label=(
            f"Add a deploy on {target}, {DEPLOY_MINUTES_BEFORE_ONSET} min before the onset "
            f"(at or upstream of eliminated {cand.id} on {cand.service_id})"
        ),
        statements=[(PLANT_DEPLOY, params)],
        tamper={
            "action": "add_deploy_upstream",
            "service_id": target,
            "minutes_before_onset": DEPLOY_MINUTES_BEFORE_ONSET,
            "as_if_known_at_alert": True,
        },
        prediction={
            "exact": True,
            "note": "the tamper creates the same deploy (its own id) recorded before the alert",
        },
        extra={
            "near": {
                "id": cand.id,
                "service_id": cand.service_id,
                "clause": cand.clause,
                "hops_from_root_service": dist[cand.service_id],
            },
        },
        created={"planted_id": planted},
    )


def _timestamp_edit(s: P.Screening, root: P.Verdict) -> _Edit | None:
    if root.entity != "Change":
        return None  # the tamper form moves changes only
    earliest = s.facts.earliest_onset
    t = s.facts.snapshot_t
    vf = earliest + SHIFT_PAST_ONSET_S
    if t is not None and vf > t:
        vf = earliest  # at the onset still fails after_onset, and stays in the snapshot
    params = {"id": root.id, "vf": vf}
    return _Edit(
        kind="change_timestamp",
        label=f"Move {root.id} to {vf - earliest} s after the earliest onset",
        statements=[(MOVE_CHANGE, params)],
        tamper={"action": "change_timestamp", "change_id": root.id, "new_valid_from": vf},
        prediction={"exact": True, "note": "the tamper sets the same valid_from in the world"},
    )


# --- trying edits on copies --------------------------------------------------------------


def _copy_name(sid: str, iid: str, k: int) -> str:
    tag = iid if len(iid) <= 48 else hashlib.sha256(iid.encode()).hexdigest()[:16]
    return naming.storm(sid, f"fragility-{k}-{tag}")


async def _try(
    source: str, clone: str, incident_id: str, root_id: str, edit: _Edit
) -> dict[str, Any]:
    t0 = time.perf_counter()
    await copy_graph(source, clone)
    copy_ms = round((time.perf_counter() - t0) * 1000, 1)
    statements = []
    for cypher, params in edit.statements:
        text = dedent(cypher).strip()
        await query(clone, text, params)
        statements.append({"cypher": text, "params": params})
    r = await verify_claim(clone, incident_id, root_id)
    res = r.result
    return {
        "kind": edit.kind,
        "label": edit.label,
        **edit.extra,
        "edit": {"statements": statements, **edit.created},
        "tamper": edit.tamper,
        "prediction": edit.prediction,
        "verdict": res["verdict"],
        "clause": res["clause"],
        "detail": res["detail"],
        "query_text": res["query_text"],
        "rivals": [x["id"] for x in res["rivals"]],
        "flips": res["verdict"] != "VERIFIED",
        "copy_ms": copy_ms,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
        "clone": clone,
    }


def _scope(ctx: ToolContext, graph: str) -> str:
    if not ctx.sid:
        raise ScopeError("fragility_map needs a session to copy the snapshot into")
    if not ctx.allows(graph):
        raise ScopeError(f"fragility_map may not touch {graph} from this run's scope")
    return ctx.sid


@register(FragilityArgs, with_context=True, read_only=False)
async def fragility_map(
    graph: str, *, ctx: ToolContext, incident_id: str, root_id: str
) -> ToolResult:
    """Try at most ten single edits (remove a causal hop, plant a deploy upstream, move the
    root past the onset), each on its own throwaway copy of the snapshot, and report which
    would flip a VERIFIED root. The snapshot itself is never written. Past ``BUDGET_S`` no
    further edit is started: the rest count as skipped and ``out_of_time`` is set."""
    sid = _scope(ctx, graph)
    t0 = time.perf_counter()
    log = QueryLog(graph)
    base = (await verify_claim(graph, incident_id, root_id)).result
    s = await P.screen(log, incident_id)
    out: dict[str, Any] = {
        "incident_id": incident_id,
        "root_id": root_id,
        "source": graph,
        "applicable": base["verdict"] == "VERIFIED",
        "baseline": {k: base[k] for k in ("verdict", "clause", "detail")}
        | {"rivals": [x["id"] for x in base["rivals"]]},
        "earliest_onset": s.facts.earliest_onset,
        "t": s.facts.snapshot_t,
        "path": [],
        "tried": [],
        "flips": [],
        "copies": 0,
        "skipped": 0,
        "out_of_time": False,
        "max_edits": MAX_EDITS,
        "copy_ms": None,
    }
    if not out["applicable"]:
        out["detail"] = f"{root_id} is {base['verdict']}, not VERIFIED: no verdict to flip"
        return log.result("fragility_map", out, [incident_id, root_id])

    root = next(v for v in s.verdicts if v.id == root_id)
    rows = await log.rows(ALERT_SERVICE, {"incident_id": incident_id})
    alert = rows[0][0] if rows and rows[0][0] else None
    target = alert if alert and root.hops.get(alert) is not None else None
    if target is None:  # the farthest symptom the root reaches
        target = max((h, sv) for sv, h in root.hops.items() if h is not None)[1]
    path = (await _path(log, root.entity, root_id, target))["path"] or [root_id]
    out["path"] = path

    hops = await _hop_edits(log, path)
    others = [
        e
        for e in (await _deploy_edit(log, s, root, alert, path), _timestamp_edit(s, root))
        if e is not None
    ]
    planned = len(hops) + len(others)
    edits = (hops[: max(0, MAX_EDITS - len(others))] + others)[:MAX_EDITS]
    out["skipped"] = planned - len(edits)

    async with _locks(f"{sid}:{incident_id}"):
        for k, edit in enumerate(edits):
            if time.perf_counter() - t0 > BUDGET_S:  # stop with what was tried so far
                out["out_of_time"] = True
                out["skipped"] += len(edits) - k
                break
            clone = _copy_name(sid, incident_id, k)
            if not ctx.allows(clone):
                raise ScopeError(f"fragility_map may not touch {clone} from this run's scope")
            if ctx.track_graph is not None:
                ctx.track_graph(clone)  # before it exists: a crash cannot leak it
            out["copies"] += 1
            try:
                out["tried"].append(await _try(graph, clone, incident_id, root_id, edit))
            finally:
                await delete_graph(clone)

    out["flips"] = [e for e in out["tried"] if e["flips"]]
    if out["tried"]:
        out["copy_ms"] = round(sum(e["copy_ms"] for e in out["tried"]) / len(out["tried"]), 1)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    why = f"the {BUDGET_S:g} s time budget" if out["out_of_time"] else f"the cap of {MAX_EDITS}"
    out["detail"] = (
        f"{len(out['flips'])} of {len(out['tried'])} single edits would flip {root_id} "
        f"(each on its own copy of the snapshot; {out['skipped']} skipped over {why})."
    )
    touched = [incident_id, root_id, *path]
    touched += [e["near"]["id"] for e in out["tried"] if "near" in e]
    return log.result("fragility_map", out, touched)
