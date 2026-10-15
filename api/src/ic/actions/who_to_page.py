"""Who to page (KTD20, R19): on-call at T for the suspect's owning team, then the change
author, then the resolver of the closest precedent, then the alert service's on-call."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ic.tools.investigate import ON_CALL, ON_CALL_INF, on_call_result
from ic.tools.result import QueryLog

SUSPECT_SERVICE = """
MATCH (c {id: $id})-[:CHANGES|EXPIRES_ON]->(s:Service)
WHERE c:Change OR c:Expiry
RETURN s.id
"""
AUTHOR = """
MATCH (p:Person)-[:AUTHORED]->(c:Change {id: $id})
RETURN p.id, p.name ORDER BY p.id LIMIT 1
"""
RESOLVER = """
MATCH (:Incident {id: $id})-[:REMEDIATED_BY]->(:Change)<-[:AUTHORED]-(p:Person)
RETURN p.id, p.name ORDER BY p.id LIMIT 1
"""


@dataclass
class PageTarget:
    person_id: str | None
    name: str | None
    reason: str


async def _on_call(log: QueryLog, service_id: str, at: int) -> dict | None:
    rows = await log.rows(ON_CALL, {"service_id": service_id, "at": at, **ON_CALL_INF})
    res = on_call_result(service_id, at, rows)
    return res["on_call"][0] | {"team": res["owner"]["team_name"]} if res["on_call"] else None


async def choose(
    world_graph: str,
    incident: Any,
    suspect_change_id: str | None,
    precedents: Sequence[dict] | None,
) -> PageTarget:
    """``incident`` needs ``opened_at`` (T) and ``service_id`` (the alert's service).
    ``precedents`` are find_precedents results, closest first."""
    log = QueryLog(world_graph)
    at = int(incident.opened_at)

    if suspect_change_id:
        rows = await log.rows(SUSPECT_SERVICE, {"id": suspect_change_id})
        if rows:
            oc = await _on_call(log, rows[0][0], at)
            if oc:
                return PageTarget(
                    oc["person_id"],
                    oc["name"],
                    f"on-call at T for {oc['team']}, which owns {rows[0][0]} "
                    f"(suspect {suspect_change_id})",
                )
        rows = await log.rows(AUTHOR, {"id": suspect_change_id})
        if rows:
            return PageTarget(
                rows[0][0], rows[0][1], f"author of suspect change {suspect_change_id}"
            )

    for p in precedents or []:
        rows = await log.rows(RESOLVER, {"id": p["incident_id"]})
        if rows:
            return PageTarget(
                rows[0][0], rows[0][1], f"resolver of closest precedent {p['incident_id']}"
            )

    svc = getattr(incident, "service_id", None)
    if svc:
        oc = await _on_call(log, svc, at)
        if oc:
            return PageTarget(
                oc["person_id"],
                oc["name"],
                f"on-call at T for {oc['team']}, owner of alert service {svc} (no suspect owner,"
                " author or precedent resolver found)",
            )
    return PageTarget(None, None, "no on-call, author or precedent resolver found")
