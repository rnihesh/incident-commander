"""Company tools (Ask the graph, KTD1): read-only lookups on the session world over the
company it models, people, teams, services, changes, incidents and decisions.

Every tool runs parameterized Cypher through a ``QueryLog``, so each answer carries the
queries that produced it and the domain ids it touched; the Ask agent cites only those ids.
"Now" is the world clock: ``WORLD_NOW`` or the latest incident opened since (injected
faults move it forward), unless the caller passes ``at``. Valid-time edges (OWNS, ON_CALL)
are read as of that instant.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from ic.graph.schema import WORLD_NOW
from ic.tools.investigate import ON_CALL, ON_CALL_INF, on_call_result
from ic.tools.registry import ToolInput, register
from ic.tools.result import QueryLog, ToolError, ToolResult

DAY = 86_400
EntityKind = Literal["Service", "Team", "Person", "Incident", "Postmortem", "ADR"]
ENTITY_KINDS: tuple[str, ...] = ("Service", "Team", "Person", "Incident", "Postmortem", "ADR")
MAX_TERMS = 12

# Question words that never name an entity (find_entities drops them before matching).
STOP_WORDS = frozenset(
    """
    a about after ago all also an and any are as at be been before by can changed changes
    change caused cause causes current currently day days decide decided decision decisions
    deploy deployed deploys did do does during exist exists for from get had happen happened
    has have how i in incident incidents is it its know last latest list made make many me
    month most my now of on call oncall on-call or our outage outages own owned owner owners
    owns past person please recent recently recommend recommendation recommendations
    responsible service services show since tell team teams that the their them there this to
    today us was we week weeks were what when where which who whom whose why with yesterday
    you
    """.split()
)


def search_terms(text: str) -> list[str]:
    """Lower-cased words (ids keep ``@ - . _``) minus stop words, plus each adjacent pair
    joined by ``-`` ("rate limiter" also matches "rate-limiter") and a plural's singular."""
    words = [w.strip(".-_") for w in re.split(r"[^a-z0-9@._-]+", text.lower())]
    kept = [w for w in words if len(w) >= 2 and w not in STOP_WORDS]
    terms = list(kept)
    terms += [f"{a}-{b}" for a, b in zip(kept, kept[1:], strict=False)]
    terms += [w[:-1] for w in kept if len(w) > 4 and w.endswith("s") and not w.endswith("ss")]
    return list(dict.fromkeys(terms))[:MAX_TERMS]


# --- arguments ---------------------------------------------------------------------------


def _at_field():
    return Field(None, ge=0, description="Instant to ask about, epoch seconds (null = now)")


class FindArgs(ToolInput):
    search: str = Field(
        min_length=1, max_length=200, description="Words or an id, e.g. 'checkout' or 'Ada Park'"
    )
    kinds: list[EntityKind] | None = Field(
        None, max_length=6, description="Only these node kinds (null = all)"
    )
    limit: int = Field(10, ge=1, le=25, description="How many matches to return")


class ServiceProfileArgs(ToolInput):
    service_id: str = Field(min_length=1, max_length=128, description="Service id")
    at: int | None = _at_field()
    limit: int = Field(5, ge=1, le=20, description="Recent changes and incidents to list")


class TeamProfileArgs(ToolInput):
    team_id: str = Field(min_length=1, max_length=128, description="Team id")
    at: int | None = _at_field()


class PersonProfileArgs(ToolInput):
    person_id: str = Field(min_length=1, max_length=128, description="Person id")
    at: int | None = _at_field()
    limit: int = Field(5, ge=1, le=20, description="Recent changes to list")


class DecisionsArgs(ToolInput):
    entity_id: str = Field(
        min_length=1, max_length=128, description="Service, Team, Incident or Postmortem id"
    )
    limit: int = Field(10, ge=1, le=25, description="Decisions of each kind to list")


class HistoryArgs(ToolInput):
    entity_id: str | None = Field(
        None, max_length=128, description="Service, Team or Person id (null = every incident)"
    )
    days: int | None = Field(
        None, ge=1, le=3650, description="Only incidents opened in the last N days (null = all)"
    )
    limit: int = Field(10, ge=1, le=25, description="How many incidents to list")


# --- shared helpers ----------------------------------------------------------------------

CLOCK = "MATCH (i:Incident) RETURN max(i.opened_at)"

LABEL_OF = """
MATCH (n:Service {id: $id}) RETURN 'Service' AS label
UNION ALL MATCH (n:Team {id: $id}) RETURN 'Team' AS label
UNION ALL MATCH (n:Person {id: $id}) RETURN 'Person' AS label
UNION ALL MATCH (n:Incident {id: $id}) RETURN 'Incident' AS label
UNION ALL MATCH (n:Postmortem {id: $id}) RETURN 'Postmortem' AS label
"""

TEAM_SERVICES = """
MATCH (t:Team {id: $team_id})-[o:OWNS]->(s:Service)
WHERE coalesce(o.valid_from, $ninf) <= $at AND $at < coalesce(o.valid_to, $pinf)
RETURN s.id ORDER BY s.id
"""


async def world_clock(log: QueryLog, at: int | None) -> int:
    if at is not None:
        return at
    rows = await log.rows(CLOCK)
    latest = rows[0][0] if rows else None
    return max(WORLD_NOW, int(latest or 0))


async def label_of(log: QueryLog, entity_id: str, allowed: tuple[str, ...]) -> str:
    rows = await log.rows(LABEL_OF, {"id": entity_id})
    labels = [r[0] for r in rows if r[0] in allowed]
    if not labels:
        raise ToolError(
            f"no {' or '.join(allowed)} with id {entity_id!r}; use find_entities to look up ids"
        )
    return labels[0]


async def team_services(log: QueryLog, team_id: str, at: int) -> list[str]:
    rows = await log.rows(TEAM_SERVICES, {"team_id": team_id, "at": at, **ON_CALL_INF})
    return [r[0] for r in rows]


def _person(pid: str | None, name: str | None) -> dict | None:
    return {"person_id": pid, "name": name} if pid else None


# --- find_entities -----------------------------------------------------------------------

FIND = """
MATCH (n) WHERE (n:Service OR n:Team OR n:Person OR n:Incident OR n:Postmortem OR n:ADR)
WITH n, labels(n)[0] AS kind, toLower(coalesce(n.name, n.title, '')) AS text,
     toLower(n.id) AS nid
WHERE kind IN $kinds
WITH n, kind, text, nid, size([t IN $terms WHERE text CONTAINS t OR nid CONTAINS t]) AS hits
WHERE hits > 0
RETURN n.id AS id, kind, coalesce(n.name, n.title, n.id) AS label, hits,
       (text IN $terms OR nid IN $terms) AS exact, coalesce(n.opened_at, n.decided_at) AS at
ORDER BY hits DESC, exact DESC, size(text), id
LIMIT $limit
"""


@register(FindArgs, target="world")
async def find_entities(
    graph: str, search: str, kinds: list[str] | None = None, limit: int = 10
) -> ToolResult:
    """Look up services, teams, people, incidents, postmortems and ADRs by name, title or id
    (case-insensitive word match; "checkout" finds svc-checkout-api@us-east). Use it first
    to turn names in a question into ids for the other tools."""
    log = QueryLog(graph)
    terms = search_terms(search)
    wanted = list(dict.fromkeys(kinds or ENTITY_KINDS))
    rows = await log.rows(FIND, {"terms": terms, "kinds": wanted, "limit": limit})
    matches = [
        {"id": r[0], "kind": r[1], "label": r[2], "hits": r[3], "exact": bool(r[4]), "at": r[5]}
        for r in rows
    ]
    result = {"search": search, "terms": terms, "kinds": wanted, "matches": matches}
    return log.result("find_entities", result, [m["id"] for m in matches])


# --- service_profile ---------------------------------------------------------------------

SERVICE = "MATCH (s:Service {id: $service_id}) RETURN s.name, s.region, s.tier, s.domain"

SERVICE_CHANGES = """
MATCH (c:Change)-[:CHANGES]->(:Service {id: $service_id})
WHERE c.valid_from <= $at
OPTIONAL MATCH (p:Person)-[:AUTHORED]->(c)
RETURN c.id, c.kind, c.summary, c.valid_from, p.id, p.name
ORDER BY c.valid_from DESC, c.id
LIMIT $limit
"""

SERVICE_INCIDENTS = """
MATCH (i:Incident)-[:HAS]->(:Symptom)-[:ON]->(:Service {id: $service_id})
WHERE i.opened_at <= $at
WITH DISTINCT i
OPTIONAL MATCH (i)-[:CAUSED_BY]->(c)
RETURN i.id, i.title, i.opened_at, c.id
ORDER BY i.opened_at DESC, i.id
LIMIT $limit
"""


@register(ServiceProfileArgs, target="world")
async def service_profile(
    graph: str, service_id: str, at: int | None = None, limit: int = 5
) -> ToolResult:
    """A service at an instant: its owning team, who is on call for it, its most recent
    changes (with authors) and the incidents it was part of."""
    log = QueryLog(graph)
    now = await world_clock(log, at)
    rows = await log.rows(SERVICE, {"service_id": service_id})
    if not rows:
        raise ToolError(f"no service {service_id!r}; use find_entities to look up service ids")
    name, region, tier, domain = rows[0]
    oc = await log.rows(ON_CALL, {"service_id": service_id, "at": now, **ON_CALL_INF})
    owner = on_call_result(service_id, now, oc)
    p = {"service_id": service_id, "at": now, "limit": limit}
    changes = await log.rows(SERVICE_CHANGES, p)
    incidents = await log.rows(SERVICE_INCIDENTS, p)
    result = {
        "service": {
            "id": service_id,
            "name": name,
            "region": region,
            "tier": tier,
            "domain": domain,
        },
        "as_of": now,
        "owner": owner["owner"],
        "on_call": owner["on_call"],
        "recent_changes": [
            {
                "id": r[0],
                "kind": r[1],
                "summary": r[2],
                "at": r[3],
                "author": _person(r[4], r[5]),
            }
            for r in changes
        ],
        "incidents": [
            {"id": r[0], "title": r[1], "opened_at": r[2], "cause_id": r[3]} for r in incidents
        ],
    }
    touched = [service_id, *(r[0] for r in oc), *(r[2] for r in oc)]
    touched += [x for r in changes for x in (r[0], r[4])]
    touched += [x for r in incidents for x in (r[0], r[3])]
    return log.result("service_profile", result, touched)


# --- team_profile ------------------------------------------------------------------------

TEAM = "MATCH (t:Team {id: $team_id}) RETURN t.name, t.domain"

TEAM_MEMBERS = """
MATCH (p:Person)-[:MEMBER_OF]->(:Team {id: $team_id})
RETURN p.id, p.name ORDER BY p.id
"""

TEAM_ON_CALL = """
MATCH (p:Person)-[oc:ON_CALL]->(:Team {id: $team_id})
WHERE coalesce(oc.valid_from, $ninf) <= $at AND $at < coalesce(oc.valid_to, $pinf)
RETURN p.id, p.name ORDER BY p.id
"""

TEAM_SERVICES_SHOWN = 25


@register(TeamProfileArgs, target="world")
async def team_profile(graph: str, team_id: str, at: int | None = None) -> ToolResult:
    """A team at an instant: its members, who is on call, and the services it owns."""
    log = QueryLog(graph)
    now = await world_clock(log, at)
    rows = await log.rows(TEAM, {"team_id": team_id})
    if not rows:
        raise ToolError(f"no team {team_id!r}; use find_entities to look up team ids")
    members = await log.rows(TEAM_MEMBERS, {"team_id": team_id})
    on_call = await log.rows(TEAM_ON_CALL, {"team_id": team_id, "at": now, **ON_CALL_INF})
    services = await team_services(log, team_id, now)
    shown = services[:TEAM_SERVICES_SHOWN]
    result = {
        "team": {"id": team_id, "name": rows[0][0], "domain": rows[0][1]},
        "as_of": now,
        "members": [{"person_id": r[0], "name": r[1]} for r in members],
        "on_call": [{"person_id": r[0], "name": r[1]} for r in on_call],
        "service_count": len(services),
        "services": shown,
    }
    touched = [team_id, *(r[0] for r in members), *(r[0] for r in on_call), *shown]
    return log.result("team_profile", result, touched)


# --- person_profile ----------------------------------------------------------------------

PERSON = """
MATCH (p:Person {id: $person_id})
OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
RETURN p.name, t.id, t.name
"""

PERSON_ON_CALL = """
MATCH (:Person {id: $person_id})-[oc:ON_CALL]->(t:Team)
WHERE coalesce(oc.valid_to, $pinf) > $at
RETURN t.id, oc.valid_from, oc.valid_to
ORDER BY oc.valid_from
LIMIT 2
"""

PERSON_CHANGES = """
MATCH (:Person {id: $person_id})-[:AUTHORED]->(c:Change)
WHERE c.valid_from <= $at
OPTIONAL MATCH (c)-[:CHANGES]->(s:Service)
RETURN c.id, c.kind, c.summary, c.valid_from, s.id
ORDER BY c.valid_from DESC, c.id
LIMIT $limit
"""

PERSON_INCIDENTS = """
MATCH (:Person {id: $person_id})-[:AUTHORED]->(c:Change)<-[:CAUSED_BY]-(i:Incident)
RETURN i.id, i.title, c.id
ORDER BY i.opened_at DESC, i.id
LIMIT $limit
"""


@register(PersonProfileArgs, target="world")
async def person_profile(
    graph: str, person_id: str, at: int | None = None, limit: int = 5
) -> ToolResult:
    """A person: their team, whether they are on call now (and their next slot), the
    changes they authored most recently, and incidents their changes caused."""
    log = QueryLog(graph)
    now = await world_clock(log, at)
    rows = await log.rows(PERSON, {"person_id": person_id})
    if not rows:
        raise ToolError(f"no person {person_id!r}; use find_entities to look up people")
    name, team_id, team_name = rows[0]
    p = {"person_id": person_id, "at": now, "limit": limit, **ON_CALL_INF}
    slots = await log.rows(PERSON_ON_CALL, p)
    changes = await log.rows(PERSON_CHANGES, p)
    incidents = await log.rows(PERSON_INCIDENTS, p)
    result = {
        "person": {"id": person_id, "name": name},
        "team": {"team_id": team_id, "team_name": team_name} if team_id else None,
        "as_of": now,
        "on_call_now": any(r[1] is not None and r[1] <= now for r in slots),
        "on_call_slots": [{"team_id": r[0], "from": r[1], "to": r[2]} for r in slots],
        "recent_changes": [
            {"id": r[0], "kind": r[1], "summary": r[2], "at": r[3], "service_id": r[4]}
            for r in changes
        ],
        "incidents_caused": [
            {"incident_id": r[0], "title": r[1], "change_id": r[2]} for r in incidents
        ],
    }
    touched = [person_id, team_id, *(r[0] for r in slots)]
    touched += [x for r in changes for x in (r[0], r[4])]
    touched += [x for r in incidents for x in (r[0], r[2])]
    return log.result("person_profile", result, touched)


# --- decisions_for -----------------------------------------------------------------------

ADRS = """
MATCH (a:ADR)-[:DECIDED]->(s:Service)
WHERE s.id IN $services
RETURN a.id, a.title, a.decided_at, collect(DISTINCT s.id) AS services
ORDER BY a.decided_at DESC, a.id
LIMIT $limit
"""

SERVICE_RECS = """
MATCH (r:Recommendation)-[:TARGETS]->(s:Service)
WHERE s.id IN $services
WITH DISTINCT r
OPTIONAL MATCH (pm:Postmortem)-[:RECOMMENDS]->(r)
OPTIONAL MATCH (pm)-[:ABOUT]->(i:Incident)
OPTIONAL MATCH (r)-[:TARGETS]->(t:Service)
RETURN r.id, r.text, r.status, pm.id, i.id, i.opened_at, collect(DISTINCT t.id) AS targets
ORDER BY i.opened_at DESC, r.id
LIMIT $limit
"""

INCIDENT_PMS = """
MATCH (pm:Postmortem)-[:ABOUT]->(i:Incident)
WHERE i.id = $id OR pm.id = $id
RETURN pm.id, pm.title, i.id, i.opened_at
ORDER BY pm.id
"""

PM_RECS = """
MATCH (pm:Postmortem)-[:RECOMMENDS]->(r:Recommendation)
WHERE pm.id IN $postmortems
OPTIONAL MATCH (pm)-[:ABOUT]->(i:Incident)
OPTIONAL MATCH (r)-[:TARGETS]->(t:Service)
RETURN r.id, r.text, r.status, pm.id, i.id, i.opened_at, collect(DISTINCT t.id) AS targets
ORDER BY r.id
LIMIT $limit
"""


def _recs(rows: list[list]) -> list[dict]:
    return [
        {
            "id": r[0],
            "text": r[1],
            "status": r[2],
            "postmortem_id": r[3],
            "incident_id": r[4],
            "incident_opened_at": r[5],
            "targets": sorted(r[6]),
        }
        for r in rows
    ]


@register(DecisionsArgs, target="world")
async def decisions_for(graph: str, entity_id: str, limit: int = 10) -> ToolResult:
    """What was decided about a service, a team's services, or after an incident: ADRs
    (architecture decisions) and postmortem recommendations with their open/closed status."""
    log = QueryLog(graph)
    kind = await label_of(log, entity_id, ("Service", "Team", "Incident", "Postmortem"))
    result: dict = {"entity_id": entity_id, "kind": kind, "postmortems": []}
    touched: list[str | None] = [entity_id]
    if kind in ("Incident", "Postmortem"):
        pms = await log.rows(INCIDENT_PMS, {"id": entity_id})
        result["postmortems"] = [{"id": r[0], "title": r[1]} for r in pms]
        rows = await log.rows(PM_RECS, {"postmortems": [r[0] for r in pms], "limit": limit})
        result["adrs"] = []
        touched += [x for r in pms for x in (r[0], r[2])]
    else:
        if kind == "Team":
            services = await team_services(log, entity_id, await world_clock(log, None))
        else:
            services = [entity_id]
        p = {"services": services, "limit": limit}
        adrs = await log.rows(ADRS, p)
        result["adrs"] = [
            {"id": r[0], "title": r[1], "decided_at": r[2], "services": sorted(r[3])} for r in adrs
        ]
        rows = await log.rows(SERVICE_RECS, p)
        touched += [r[0] for r in adrs]
    result["recommendations"] = _recs(rows)
    touched += [x for r in rows for x in (r[0], r[3], r[4])]
    return log.result("decisions_for", result, touched)


# --- incident_history --------------------------------------------------------------------

HISTORY_BY = {
    None: "MATCH (i:Incident)",
    "Service": (
        "MATCH (i:Incident)-[:HAS]->(:Symptom)-[:ON]->(s:Service)\nWHERE s.id IN $services\n"
        "WITH DISTINCT i"
    ),
    "Team": (
        "MATCH (i:Incident)-[:HAS]->(:Symptom)-[:ON]->(s:Service)\nWHERE s.id IN $services\n"
        "WITH DISTINCT i"
    ),
    "Person": (
        "MATCH (:Person {id: $entity_id})-[:AUTHORED]->(:Change)<-[:CAUSED_BY]-(i:Incident)\n"
        "WITH DISTINCT i"
    ),
}

HISTORY = """
{match}
WITH i WHERE i.opened_at >= $since AND i.opened_at <= $at
OPTIONAL MATCH (i)-[:CAUSED_BY]->(c)
OPTIONAL MATCH (p:Person)-[:AUTHORED]->(c)
OPTIONAL MATCH (pm:Postmortem)-[:ABOUT]->(i)
OPTIONAL MATCH (i)-[:HAS]->(:Symptom)-[:ON]->(sv:Service)
RETURN i.id, i.title, i.opened_at, i.resolved_at, c.id, c.kind, c.summary, p.id, p.name,
       pm.id, collect(DISTINCT sv.id) AS services
ORDER BY i.opened_at DESC, i.id
LIMIT $limit
"""


@register(HistoryArgs, target="world")
async def incident_history(
    graph: str, entity_id: str | None = None, days: int | None = None, limit: int = 10
) -> ToolResult:
    """Incidents, newest first, with the change or expiry that caused each one (and its
    author), the services that showed symptoms, and the postmortem. Filter by a service,
    a team's services, or a person (incidents their changes caused), and by recency."""
    log = QueryLog(graph)
    now = await world_clock(log, None)
    kind = None
    services: list[str] = []
    if entity_id is not None:
        kind = await label_of(log, entity_id, ("Service", "Team", "Person"))
        services = await team_services(log, entity_id, now) if kind == "Team" else [entity_id]
    since = now - days * DAY if days is not None else ON_CALL_INF["ninf"]
    params = {
        "entity_id": entity_id,
        "services": services,
        "since": since,
        "at": now,
        "limit": limit,
    }
    rows = await log.rows(HISTORY.format(match=HISTORY_BY[kind]), params)
    incidents = [
        {
            "id": r[0],
            "title": r[1],
            "opened_at": r[2],
            "resolved_at": r[3],
            "cause": (
                {"id": r[4], "kind": r[5], "summary": r[6], "author": _person(r[7], r[8])}
                if r[4]
                else None
            ),
            "postmortem_id": r[9],
            "services": sorted(r[10]),
        }
        for r in rows
    ]
    result = {"entity_id": entity_id, "as_of": now, "days": days, "incidents": incidents}
    touched: list[str | None] = [entity_id]
    touched += [x for r in rows for x in (r[0], r[4], r[7], r[9], *r[10])]
    return log.result("incident_history", result, touched)
