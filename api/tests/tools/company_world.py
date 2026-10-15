"""A tiny hand-built company for the Ask-the-graph tests (company tools, ask agent, route).

Clock: no incident is opened after WORLD_NOW, so the tools' world clock is WORLD_NOW.

    team-pay "Payments Core"     owns checkout-api@us-east, checkout@eu-west
                                 (team-search owned checkout-api until 100 days ago)
    team-search "Search Platform" owns search-api@us-east, rate-limiter@us-east

    on call now: per-0001 Ada Park (team-pay), per-0003 Cy Moss (team-search)
    per-0002 Ben Ito was on call for team-pay last week

    chg-001 deploy checkout-api   by per-0001  2 days ago
    chg-002 config checkout-api   by per-0002  20 days ago  -> caused inc-001
    chg-003 deploy rate-limiter   by per-0003  1 day ago
    chg-004 deploy search-api     by per-0003  5 days ago   -> caused inc-002

    inc-001 (20 days ago) pm-001: rec-001 open, rec-002 closed (checkout-api)
    inc-002 (5 days ago)  pm-002: rec-003 open (search-api)
    adr-001 decided checkout-api@us-east and checkout@eu-west
"""

from __future__ import annotations

from ic.graph.client import query
from ic.graph.schema import OPEN_END, WORLD_NOW

DAY = 86_400
NOW = WORLD_NOW

CHECKOUT_API = "svc-checkout-api@us-east"
CHECKOUT = "svc-checkout@eu-west"
SEARCH = "svc-search-api@us-east"
LIMITER = "svc-rate-limiter@us-east"

TEAMS = [("team-pay", "Payments Core", "commerce"), ("team-search", "Search Platform", "platform")]
SERVICES = [
    (CHECKOUT_API, "checkout-api", "us-east", "team-pay"),
    (CHECKOUT, "checkout", "eu-west", "team-pay"),
    (SEARCH, "search-api", "us-east", "team-search"),
    (LIMITER, "rate-limiter", "us-east", "team-search"),
]
PEOPLE = [
    ("per-0001", "Ada Park", "team-pay"),
    ("per-0002", "Ben Ito", "team-pay"),
    ("per-0003", "Cy Moss", "team-search"),
]
# (person, team, valid_from, valid_to)
ON_CALL = [
    ("per-0002", "team-pay", NOW - 10 * DAY, NOW - 3 * DAY),
    ("per-0001", "team-pay", NOW - 3 * DAY, NOW + 4 * DAY),
    ("per-0003", "team-search", NOW - 7 * DAY, NOW + 7 * DAY),
]
# (id, kind, service, summary, valid_from, author)
CHANGES = [
    (
        "chg-001",
        "deploy",
        CHECKOUT_API,
        "Deploy v1.2.0: new cart endpoint",
        NOW - 2 * DAY,
        "per-0001",
    ),
    ("chg-002", "config", CHECKOUT_API, "Set timeout_ms to 900", NOW - 20 * DAY, "per-0002"),
    ("chg-003", "deploy", LIMITER, "Deploy v3.0.0: new limits", NOW - 1 * DAY, "per-0003"),
    ("chg-004", "deploy", SEARCH, "Deploy v2.1.0: query planner", NOW - 5 * DAY, "per-0003"),
]
# (id, title, opened_at, cause, symptom service, postmortem)
INCIDENTS = [
    (
        "inc-001",
        "Failing requests (5xx) on checkout-api",
        NOW - 20 * DAY + 3600,
        "chg-002",
        CHECKOUT_API,
        "pm-001",
    ),
    ("inc-002", "Latency on search-api", NOW - 5 * DAY + 3600, "chg-004", SEARCH, "pm-002"),
]
# (id, postmortem, text, status, target)
RECOMMENDATIONS = [
    (
        "rec-001",
        "pm-001",
        "Validate checkout-api config changes before rollout",
        "open",
        CHECKOUT_API,
    ),
    ("rec-002", "pm-001", "Alert on checkout-api 5xx rate", "closed", CHECKOUT_API),
    ("rec-003", "pm-002", "Add a circuit breaker in front of search-api", "open", SEARCH),
]
ADR = ("adr-001", "ADR-001: Move checkout-api to active-active", NOW - 200 * DAY)


async def write_company(graph: str) -> None:
    async def run(cypher: str, **params) -> None:
        await query(graph, cypher, params)

    await run(
        "UNWIND $rows AS r CREATE (:Team {id: r[0], name: r[1], domain: r[2]})",
        rows=[list(t) for t in TEAMS],
    )
    await run(
        "UNWIND $rows AS r MATCH (t:Team {id: r[3]}) "
        "CREATE (t)-[:OWNS {valid_from: $vf, valid_to: $open, recorded_at: $vf}]->"
        "(:Service {id: r[0], name: r[1], region: r[2], tier: 1, domain: t.domain})",
        rows=[list(s) for s in SERVICES],
        vf=NOW - 100 * DAY,
        open=OPEN_END,
    )
    await run(
        "MATCH (t:Team {id: 'team-search'}), (s:Service {id: $sid}) "
        "CREATE (t)-[:OWNS {valid_from: $vf, valid_to: $vt, recorded_at: $vf}]->(s)",
        sid=CHECKOUT_API,
        vf=NOW - 400 * DAY,
        vt=NOW - 100 * DAY,
    )
    await run(
        "UNWIND $rows AS r MATCH (t:Team {id: r[2]}) "
        "CREATE (:Person {id: r[0], name: r[1]})-[:MEMBER_OF]->(t)",
        rows=[list(p) for p in PEOPLE],
    )
    await run(
        "UNWIND $rows AS r MATCH (p:Person {id: r[0]}), (t:Team {id: r[1]}) "
        "CREATE (p)-[:ON_CALL {valid_from: r[2], valid_to: r[3], recorded_at: r[2]}]->(t)",
        rows=[list(o) for o in ON_CALL],
    )
    await run(
        "UNWIND $rows AS r MATCH (s:Service {id: r[2]}), (p:Person {id: r[5]}) "
        "CREATE (p)-[:AUTHORED]->(:Change {id: r[0], kind: r[1], service_id: r[2], "
        "summary: r[3], valid_from: r[4], valid_to: $open, recorded_at: r[4]})-[:CHANGES]->(s)",
        rows=[list(c) for c in CHANGES],
        open=OPEN_END,
    )
    await run(
        "UNWIND $rows AS r MATCH (c:Change {id: r[3]}), (s:Service {id: r[4]}) "
        "CREATE (i:Incident {id: r[0], title: r[1], opened_at: r[2], resolved_at: r[2] + 3600})"
        "-[:CAUSED_BY]->(c) "
        "CREATE (i)-[:HAS]->(:Symptom {id: r[0] + '-s', service_id: r[4], onset: r[2] - 60, "
        "signature: 'error_5xx'})-[:ON]->(s) "
        "CREATE (:Postmortem {id: r[5], title: r[1], body: 'Root cause was ' + r[3], "
        "symptoms_text: r[1]})-[:ABOUT]->(i)",
        rows=[list(i) for i in INCIDENTS],
    )
    await run(
        "UNWIND $rows AS r MATCH (pm:Postmortem {id: r[1]}), (s:Service {id: r[4]}) "
        "CREATE (pm)-[:RECOMMENDS]->(:Recommendation {id: r[0], text: r[2], status: r[3]})"
        "-[:TARGETS]->(s)",
        rows=[list(r) for r in RECOMMENDATIONS],
    )
    await run(
        "MATCH (s:Service) WHERE s.id IN $sids "
        "MERGE (a:ADR {id: $id, title: $title, body: 'We decided.', decided_at: $at}) "
        "CREATE (a)-[:DECIDED]->(s)",
        sids=[CHECKOUT_API, CHECKOUT],
        id=ADR[0],
        title=ADR[1],
        at=ADR[2],
    )


async def counts(graph: str) -> tuple[int, int]:
    """(nodes, edges): the no-writes checks compare these before and after."""
    n = (await query(graph, "MATCH (n) RETURN count(n)", None, read_only=True)).result_set
    e = (await query(graph, "MATCH ()-[e]->() RETURN count(e)", None, read_only=True)).result_set
    return n[0][0], e[0][0]


async def all_ids(graph: str) -> set[str]:
    rows = (await query(graph, "MATCH (n) RETURN n.id", None, read_only=True)).result_set
    return {r[0] for r in rows}
