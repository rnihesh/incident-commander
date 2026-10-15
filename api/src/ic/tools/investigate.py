"""Investigation tools (KTD3, KTD23): each runs read-only Cypher on the graph the server
picked and returns a ToolResult carrying every query it ran.

Snapshot tools run on a materialized as-of snapshot (``IMPACTS`` exists only there);
``who_is_on_call`` runs on the session world with a valid-time filter, so any instant
can be asked about. All ids in and out are generator domain ids.
"""

from __future__ import annotations

from dataclasses import asdict

from pydantic import Field

from ic.graph.schema import OBSERVED_CALL_THRESHOLD, OPEN_END
from ic.tools import predicate as P
from ic.tools.registry import ToolInput, register
from ic.tools.result import QueryLog, ToolError, ToolResult

MAX_DEPTH = 6  # reach bound for closure and the certificate's reverse reach
SAMPLE = 20  # ids listed for the (large) outside-window cut
BETWEENNESS_SEED = 42
BETWEENNESS_SAMPLES = 512


def _window_field():
    return Field(P.DEFAULT_WINDOW_MINUTES, ge=1, le=7 * 24 * 60, description="Alert window")


class IncidentArgs(ToolInput):
    incident_id: str = Field(min_length=1, max_length=128, description="Incident id, e.g. inc-007")


class WindowArgs(IncidentArgs):
    window_minutes: int = _window_field()


class SuspectArgs(WindowArgs):
    suspect_id: str = Field(min_length=1, max_length=128, description="Change or Expiry id")


class ClosureArgs(ToolInput):
    entity_id: str = Field(min_length=1, max_length=128, description="Change, Expiry or Service")
    max_depth: int = Field(MAX_DEPTH, ge=1, le=12, description="Maximum IMPACTS hops")


class PathArgs(ToolInput):
    entity_id: str = Field(min_length=1, max_length=128, description="Change, Expiry or Service")
    service_id: str = Field(min_length=1, max_length=128, description="Target service id")


class ExonerateArgs(IncidentArgs):
    change_id: str = Field(min_length=1, max_length=128, description="Change (or Expiry) id")


class ServiceArgs(ToolInput):
    service_id: str = Field(min_length=1, max_length=128, description="Service id")


class OnCallArgs(ServiceArgs):
    at: int = Field(ge=0, description="Instant to ask about, epoch seconds")


class ChokepointArgs(ToolInput):
    top_k: int = Field(5, ge=1, le=50, description="How many services to return")


# --- symptoms and candidates -----------------------------------------------------------


@register(IncidentArgs)
async def list_symptoms(graph: str, incident_id: str) -> ToolResult:
    """List the incident's symptoms (service, onset, signature) visible in the snapshot."""
    log = QueryLog(graph)
    facts = await P.incident_facts(log, incident_id)
    result = {
        "incident_id": incident_id,
        "opened_at": facts.opened_at,
        "earliest_onset": facts.earliest_onset,
        "symptoms": facts.symptoms,
    }
    touched = [incident_id, *(s["symptom_id"] for s in facts.symptoms)]
    return log.result("list_symptoms", result, touched + facts.symptomatic_services)


@register(WindowArgs)
async def changes_in_window(
    graph: str, incident_id: str, window_minutes: int = P.DEFAULT_WINDOW_MINUTES
) -> ToolResult:
    """Changes and expiries whose event falls in the alert window before the snapshot time."""
    log = QueryLog(graph)
    facts = await P.incident_facts(log, incident_id)
    lo, hi = facts.window(window_minutes)
    cands = await P.candidates_in_window(log, lo, hi)
    result = {
        "incident_id": incident_id,
        "window": {"from": lo, "to": hi, "minutes": window_minutes},
        "earliest_onset": facts.earliest_onset,
        "candidates": [asdict(c) for c in cands],
    }
    return log.result("changes_in_window", result, [incident_id, *(c.id for c in cands)])


# --- reachability ----------------------------------------------------------------------

CLOSURE = """
MATCH (n:{label} {{id: $entity_id}})
CALL algo.BFS(n, $max_depth, 'IMPACTS') YIELD nodes
RETURN [x IN nodes WHERE x:Service | x.id] AS reachable
"""

CANONICAL_PATH = """
MATCH (a:{label} {{id: $entity_id}}), (b:Service {{id: $service_id}})
WITH a, b
MATCH p = allShortestPaths((a)-[:IMPACTS*]->(b))
WITH [n IN nodes(p) | n.id] AS ids
RETURN ids ORDER BY ids LIMIT 1
"""

DISPLAY_PATH = """
MATCH (a:{label} {{id: $entity_id}}), (b:Service {{id: $service_id}})
CALL algo.SPpaths({{sourceNode: a, targetNode: b, relTypes: ['IMPACTS'],
                    maxLen: $max_len, relDirection: 'outgoing', pathCount: 1}})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.id] AS ids, pathWeight
"""


@register(ClosureArgs)
async def impact_closure(graph: str, entity_id: str, max_depth: int = MAX_DEPTH) -> ToolResult:
    """Services a Change, Expiry or Service can impact (algo.BFS over IMPACTS)."""
    log = QueryLog(graph)
    label = await P.entity_label(log, entity_id, ("Change", "Expiry", "Service"))
    rows = await log.rows(
        CLOSURE.format(label=label), {"entity_id": entity_id, "max_depth": max_depth}
    )
    reachable = sorted(set(rows[0][0])) if rows else []
    result = {
        "entity_id": entity_id,
        "label": label,
        "max_depth": max_depth,
        "reachable_services": reachable,
        "count": len(reachable),
    }
    return log.result("impact_closure", result, [entity_id, *reachable])


async def _path(log: QueryLog, label: str, entity_id: str, service_id: str) -> dict:
    """Canonical shortest IMPACTS path (lexicographically smallest id sequence among the
    equal-length shortest ones) plus the algo.SPpaths path shown in the UI."""
    params = {"entity_id": entity_id, "service_id": service_id}
    rows = await log.rows(CANONICAL_PATH.format(label=label), params)
    canonical_q = log.last_index
    path = rows[0][0] if rows else None
    shown = await log.rows(
        DISPLAY_PATH.format(label=label), {**params, "max_len": max(len(path or []), 2) + 4}
    )
    return {
        "from": entity_id,
        "to": service_id,
        "found": path is not None,
        "path": path,
        "hops": len(path) - 1 if path else None,
        "display_path": shown[0][0] if shown else None,
        "query": canonical_q,
    }


@register(PathArgs)
async def causal_path(graph: str, entity_id: str, service_id: str) -> ToolResult:
    """Shortest IMPACTS path from a Change, Expiry or Service to a service."""
    log = QueryLog(graph)
    label = await P.entity_label(log, entity_id, ("Change", "Expiry", "Service"))
    result = await _path(log, label, entity_id, service_id)
    return log.result("causal_path", result, result["path"] or [entity_id, service_id])


# --- funnel, certificate, necessity, exoneration ---------------------------------------

OUTSIDE_WINDOW = """
MATCH (c:Change) WHERE c.valid_from < $lo
RETURN count(c) AS n, collect(c.id)[..$sample] AS sample
UNION ALL
MATCH (e:Expiry) WHERE e.valid_to < $lo
RETURN count(e) AS n, collect(e.id)[..$sample] AS sample
"""

REVERSE_REACH = """
MATCH (s:Service) WHERE s.id IN $symptomatic
OPTIONAL MATCH (u:Service)-[:IMPACTS*1..{depth}]->(s)
WITH collect(DISTINCT s.id) + collect(DISTINCT u.id) AS ids
UNWIND ids AS id
RETURN DISTINCT id ORDER BY id
"""


@register(WindowArgs)
async def screen_candidates(
    graph: str, incident_id: str, window_minutes: int = P.DEFAULT_WINDOW_MINUTES
) -> ToolResult:
    """Elimination funnel: every change in the window cut down clause by clause (KTD23)."""
    log = QueryLog(graph)
    s = await P.screen(log, incident_id, window_minutes)
    out_rows = await log.rows(OUTSIDE_WINDOW, {"lo": s.lo, "sample": SAMPLE})
    out_q = log.last_index
    n_outside = sum(r[0] for r in out_rows)
    outside_sample = [i for r in out_rows for i in r[1]][:SAMPLE]

    remaining = len(s.verdicts)
    steps = [
        {
            "step": "outside_window",
            "eliminated": outside_sample,
            "eliminated_count": n_outside,
            "truncated": n_outside > len(outside_sample),
            "remaining": remaining,
            "queries": [s.candidates_query, out_q],
        }
    ]
    for clause in P.CLAUSES:
        cut = [v for v in s.verdicts if v.clause == clause]
        remaining -= len(cut)
        steps.append(
            {
                "step": clause,
                "eliminated": [v.id for v in cut],
                "eliminated_count": len(cut),
                "truncated": False,
                "remaining": remaining,
                "queries": [v.query for v in cut],
            }
        )
    result = {
        "incident_id": incident_id,
        "window": {"from": s.lo, "to": s.hi, "minutes": window_minutes},
        "start": n_outside + len(s.verdicts),
        "steps": steps,
        "verdicts": [v.as_dict() for v in s.verdicts],
        "survivors": [v.id for v in s.survivors],
    }
    touched = [incident_id, *(v.id for v in s.verdicts), *s.facts.symptomatic_services]
    return log.result("screen_candidates", result, touched)


async def _verdict_for(log: QueryLog, s: P.Screening, entity_id: str) -> P.Verdict:
    for v in s.verdicts:
        if v.id == entity_id:
            return v
    label = await P.entity_label(log, entity_id, P.ENTITY_LABELS)
    return await P.explain(log, s.facts, entity_id, label)


@register(SuspectArgs)
async def absence_certificate(
    graph: str,
    incident_id: str,
    suspect_id: str,
    window_minutes: int = P.DEFAULT_WINDOW_MINUTES,
) -> ToolResult:
    """Absence certificate: every service that could impact a symptomatic service, every
    change/expiry on them in the window, and the clause each one fails. ``qualifying``
    lists entities other than the suspect that explain the symptoms (empty when clean)."""
    log = QueryLog(graph)
    facts = await P.incident_facts(log, incident_id)
    rows = await log.rows(
        REVERSE_REACH.format(depth=MAX_DEPTH), {"symptomatic": facts.symptomatic_services}
    )
    services = [r[0] for r in rows]
    s = await P.screen(log, incident_id, window_minutes, services=services)
    suspect = await _verdict_for(log, s, suspect_id)
    qualifying = [v.id for v in s.survivors if v.id != suspect_id]
    result = {
        "incident_id": incident_id,
        "suspect_id": suspect_id,
        "window": {"from": s.lo, "to": s.hi, "minutes": window_minutes},
        "reach_depth": MAX_DEPTH,
        "checked_services": services,
        "checked": [v.as_dict() for v in s.verdicts],
        "qualifying": qualifying,
        "suspect": suspect.as_dict(),
        "clean": not qualifying,
    }
    touched = [incident_id, suspect_id, *services, *(v.id for v in s.verdicts)]
    return log.result("absence_certificate", result, touched)


def _farthest(v: P.Verdict) -> str | None:
    reached = [(h, svc) for svc, h in v.hops.items() if h is not None]
    return max(reached)[1] if reached else None


@register(SuspectArgs)
async def necessity(
    graph: str,
    incident_id: str,
    suspect_id: str,
    window_minutes: int = P.DEFAULT_WINDOW_MINUTES,
) -> ToolResult:
    """Necessity: with the suspect excluded, does any rival still explain every symptom?
    ``necessary`` is true when no rival qualifies; otherwise each rival's path is shown."""
    log = QueryLog(graph)
    s = await P.screen(log, incident_id, window_minutes, exclude=[suspect_id])
    label = await P.entity_label(log, suspect_id, P.ENTITY_LABELS)
    suspect = await P.explain(log, s.facts, suspect_id, label)
    rivals = []
    for v in s.survivors:
        target = _farthest(v)
        path = await _path(log, v.entity, v.id, target) if target else None
        rivals.append({**v.as_dict(), "path": path})
    result = {
        "incident_id": incident_id,
        "suspect_id": suspect_id,
        "suspect": suspect.as_dict(),
        "necessary": not rivals,
        "rivals": rivals,
        "checked": [v.id for v in s.verdicts],
    }
    touched = [incident_id, suspect_id, *(v.id for v in s.verdicts)]
    for r in rivals:
        touched += (r["path"] or {}).get("path") or []
    return log.result("necessity", result, touched)


@register(ExonerateArgs)
async def exonerate(graph: str, incident_id: str, change_id: str) -> ToolResult:
    """Answer "was it my deploy?": the causal path if the change explains the incident,
    otherwise an exoneration receipt naming the clause it fails and what was checked."""
    log = QueryLog(graph)
    try:
        facts = await P.incident_facts(log, incident_id)
    except ToolError:
        raise ToolError(
            f"exonerate requires a completed incident: {incident_id} is not in this snapshot"
        ) from None
    label = await P.entity_label(log, change_id, P.ENTITY_LABELS)
    v = await P.explain(log, facts, change_id, label)
    checked = [{**sym, "hop": v.hops.get(sym["service_id"])} for sym in facts.symptoms]
    result: dict = {
        "incident_id": incident_id,
        "change_id": change_id,
        "qualifies": v.ok,
        "verdict": v.as_dict(),
        "checked_services": checked,
    }
    touched = [incident_id, change_id, *facts.symptomatic_services]
    if v.ok:
        target = _farthest(v)
        result["path"] = await _path(log, label, change_id, target) if target else None
        touched += (result["path"] or {}).get("path") or []
    else:
        result["receipt"] = {
            "exonerated": True,
            "failing_clause": v.clause,
            "detail": v.detail,
            "checked_services": facts.symptomatic_services,
        }
    return log.result("exonerate", result, touched)


# --- people and observed traffic --------------------------------------------------------

ON_CALL = """
MATCH (s:Service {id: $service_id})<-[o:OWNS]-(team:Team)
WHERE coalesce(o.valid_from, $ninf) <= $at AND $at < coalesce(o.valid_to, $pinf)
OPTIONAL MATCH (p:Person)-[oc:ON_CALL]->(team)
WHERE coalesce(oc.valid_from, $ninf) <= $at AND $at < coalesce(oc.valid_to, $pinf)
RETURN team.id AS team_id, team.name AS team_name, p.id AS person_id, p.name AS person_name
ORDER BY person_id
"""

ON_CALL_INF = {"ninf": -(2**62), "pinf": OPEN_END * 10}


def on_call_result(service_id: str, at: int, rows: list[list]) -> dict:
    owner = {"team_id": rows[0][0], "team_name": rows[0][1]} if rows else None
    people = [{"person_id": r[2], "name": r[3]} for r in rows if r[2]]
    return {"service_id": service_id, "at": at, "owner": owner, "on_call": people}


@register(OnCallArgs, target="world")
async def who_is_on_call(graph: str, service_id: str, at: int) -> ToolResult:
    """Who was on call at ``at`` for the team owning the service (valid-time filtered)."""
    log = QueryLog(graph)
    rows = await log.rows(ON_CALL, {"service_id": service_id, "at": at, **ON_CALL_INF})
    result = on_call_result(service_id, at, rows)
    touched = [service_id, *(r[0] for r in rows), *(r[2] for r in rows)]
    return log.result("who_is_on_call", result, touched)


OBSERVED = """
MATCH (s:Service {id: $service_id})
MATCH (peer:Service)-[o:OBSERVED_CALL]->(s)
RETURN 'caller' AS direction, peer.id AS peer, o.count AS count, o.first_seen AS first_seen,
       o.last_seen AS last_seen, size([(peer)-[:DEPENDS_ON]->(s) | 1]) > 0 AS declared
UNION ALL
MATCH (s:Service {id: $service_id})
MATCH (s)-[o:OBSERVED_CALL]->(peer:Service)
RETURN 'callee' AS direction, peer.id AS peer, o.count AS count, o.first_seen AS first_seen,
       o.last_seen AS last_seen, size([(s)-[:DEPENDS_ON]->(peer) | 1]) > 0 AS declared
"""


@register(ServiceArgs)
async def observed_calls(graph: str, service_id: str) -> ToolResult:
    """Observed callers and callees of a service with call counts; ``undeclared`` marks
    traffic with no DEPENDS_ON behind it (a hidden coupling)."""
    log = QueryLog(graph)
    rows = await log.rows(OBSERVED, {"service_id": service_id})
    calls = sorted(
        (
            {
                "direction": r[0],
                "peer": r[1],
                "count": r[2],
                "first_seen": r[3],
                "last_seen": r[4],
                "declared": r[5],
                "undeclared": not r[5],
                "impacts": r[2] >= OBSERVED_CALL_THRESHOLD,
            }
            for r in rows
        ),
        key=lambda c: (c["direction"], -c["count"], c["peer"]),
    )
    result = {
        "service_id": service_id,
        "threshold": OBSERVED_CALL_THRESHOLD,
        "calls": calls,
        "undeclared": [c["peer"] for c in calls if c["undeclared"]],
    }
    return log.result("observed_calls", result, [service_id, *(c["peer"] for c in calls)])


BETWEENNESS = """
CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['IMPACTS'],
                       samplingSize: $samples, samplingSeed: $seed})
YIELD node, score
RETURN node.id AS service_id, score
ORDER BY score DESC, service_id
LIMIT $top_k
"""


@register(ChokepointArgs)
async def rank_chokepoints(graph: str, top_k: int = 5) -> ToolResult:
    """Services most failure paths run through (seeded algo.betweenness over IMPACTS)."""
    log = QueryLog(graph)
    params = {"samples": BETWEENNESS_SAMPLES, "seed": BETWEENNESS_SEED, "top_k": top_k}
    rows = await log.rows(BETWEENNESS, params)
    ranked = [{"service_id": r[0], "score": r[1]} for r in rows]
    result = {"top_k": top_k, "seed": BETWEENNESS_SEED, "chokepoints": ranked}
    return log.result("rank_chokepoints", result, [r["service_id"] for r in ranked])
