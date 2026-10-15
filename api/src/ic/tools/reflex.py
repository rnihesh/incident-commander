"""Reflex answer (KTD16, R30): what the UI shows within 100 ms of an alert, before any LLM
call and before snapshots exist.

Pure Cypher on the *session world* with valid-time filters at ``t``. Variable-length
filters apply after traversal, which is fine at these small hop bounds. The answer is
labelled ``pre_snapshot`` and is superseded once the proof-carrying funnel runs.
"""

from __future__ import annotations

from pydantic import Field

from ic.tools.investigate import ON_CALL, ON_CALL_INF, on_call_result
from ic.tools.predicate import DEFAULT_WINDOW_MINUTES
from ic.tools.registry import ToolInput, register
from ic.tools.result import QueryLog, ToolResult

BLAST_HOPS = 4
SUSPECT_HOPS = 3
TOP_SUSPECTS = 3

_VALID_AT_T = (
    "all(e IN relationships(p) WHERE coalesce(e.valid_from, $ninf) <= $t "
    "AND $t < coalesce(e.valid_to, $pinf))"
)

# The upstream area where the cause must be: services the alerting service depends on
# transitively. (The alert fires on the most customer-facing service, so the services that
# depend on it are ~none; "who it depends on" is the number that scopes the search.)
SEARCH_SPACE = f"""
MATCH p = (a:Service {{id: $service_id}})-[:DEPENDS_ON*1..{BLAST_HOPS}]->(u:Service)
WHERE u <> a AND {_VALID_AT_T}
RETURN count(DISTINCT u) AS search_space
"""

# Services already showing symptoms on the incident at t.
SYMPTOMATIC = """
MATCH (:Incident {id: $incident_id})-[:HAS]->(y:Symptom)
WHERE y.onset <= $t
RETURN count(DISTINCT y.service_id) AS blast_radius
"""

# Most recent changes on the alerting service or anything it depends on (<= 3 hops).
# The zero-length path includes the alerting service itself. (Written without
# OPTIONAL MATCH + UNWIND of a collected list: FalkorDB then drops rows when an indexed
# property filter follows.)
TOP_SUSPECTS_Q = f"""
MATCH p = (a:Service {{id: $service_id}})-[:DEPENDS_ON*0..{SUSPECT_HOPS}]->(s:Service)
WHERE {_VALID_AT_T}
WITH DISTINCT s
MATCH (c:Change)-[:CHANGES]->(s)
WHERE $lo <= c.valid_from AND c.valid_from <= $t
RETURN c.id AS id, c.kind AS kind, s.id AS service_id, c.valid_from AS valid_from,
       c.summary AS summary
ORDER BY valid_from DESC, id
LIMIT {TOP_SUSPECTS}
"""


class ReflexArgs(ToolInput):
    alert_service_id: str = Field(min_length=1, max_length=128, description="Alerting service")
    t: int = Field(ge=0, description="Alert time, epoch seconds")
    window_minutes: int = Field(DEFAULT_WINDOW_MINUTES, ge=1, le=7 * 24 * 60)
    incident_id: str | None = Field(
        None, max_length=64, description="Incident whose symptoms give the blast radius"
    )


@register(ReflexArgs, target="world")
async def reflex(
    world_graph: str,
    alert_service_id: str,
    t: int,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    incident_id: str | None = None,
) -> ToolResult:
    """Pre-snapshot reflex: search space (upstream services), blast radius (services already
    symptomatic), top recent suspects, and who is on call at t."""
    log = QueryLog(world_graph)
    base = {"service_id": alert_service_id, "t": t, **ON_CALL_INF}
    space = await log.rows(SEARCH_SPACE, base)
    blast = await log.rows(SYMPTOMATIC, {"incident_id": incident_id, "t": t}) if incident_id else []
    suspects = await log.rows(TOP_SUSPECTS_Q, {**base, "lo": t - window_minutes * 60})
    on_call = await log.rows(ON_CALL, {**base, "at": t})
    result = {
        "pre_snapshot": True,
        "alert_service_id": alert_service_id,
        "t": t,
        "window_minutes": window_minutes,
        "search_space": space[0][0] if space else 0,
        "blast_radius": blast[0][0] if blast else 0,
        "top_suspects": [
            {"id": r[0], "kind": r[1], "service_id": r[2], "valid_from": r[3], "summary": r[4]}
            for r in suspects
        ],
        "on_call": on_call_result(alert_service_id, t, on_call),
    }
    touched = [
        alert_service_id,
        *(r[0] for r in suspects),
        *(r[2] for r in suspects),
        *(r[2] for r in on_call),
    ]
    return log.result("reflex", result, touched)
