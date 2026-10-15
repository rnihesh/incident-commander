"""A tiny hand-built company used by the tool tests, written with parameterized Cypher.

Topology (caller -> callee DEPENDS_ON; failure travels the other way over IMPACTS):

    web -> api -> db -> disk          cache ..> db   (observed only, undeclared)
    web -> api-b -> db                billing         (isolated; old dep on db ended)

Every incident is opened at OPENED and the snapshots are taken at SNAP_T.

Candidates in the alert window of an incident whose earliest onset is T0+100:
    c-bad   deploy  db    T0-600   the culprit for latency/5xx incidents
    c-late  deploy  db    T0+150   after onset
    c-disk  deploy  disk  T0-300   healthy dependency (origin)
    c-cfg   config  db    T0-200   cannot produce latency or tls_error (signature)
    c-api   deploy  api   T0-100   api is not the first to fail (origin)
    c-unk   deploy  web   T0-50    recorded late: true view only
    x-cert  cert    db    expires T0-300
    c-old   deploy  db    T0-10000 outside the 120-minute window
"""

from __future__ import annotations

from ic.graph.client import query
from ic.graph.schema import OBSERVED_CALL_THRESHOLD, OPEN_END

T0 = 1_000_000
LONG_AGO = T0 - 1_000_000
OPENED = T0 + 400
SNAP_T = T0 + 1000

SERVICES = ["db", "api", "api-b", "web", "disk", "billing", "cache"]
# (caller, callee, valid_from, valid_to)
DEPENDS = [
    ("api", "db", LONG_AGO, OPEN_END),
    ("api-b", "db", LONG_AGO, OPEN_END),
    ("web", "api", LONG_AGO, OPEN_END),
    ("web", "api-b", LONG_AGO, OPEN_END),
    ("db", "disk", LONG_AGO, OPEN_END),
    ("billing", "db", LONG_AGO, T0 - 1000),  # ended before the incident
]
OBSERVED = [("cache", "db", OBSERVED_CALL_THRESHOLD + 30), ("api", "db", 500)]
# (id, kind, service, valid_from, recorded_at)
CHANGES = [
    ("c-bad", "deploy", "db", T0 - 600, T0 - 540),
    ("c-late", "deploy", "db", T0 + 150, T0 + 160),
    ("c-disk", "deploy", "disk", T0 - 300, T0 - 290),
    ("c-cfg", "config", "db", T0 - 200, T0 - 190),
    ("c-api", "deploy", "api", T0 - 100, T0 - 90),
    ("c-unk", "deploy", "web", T0 - 50, T0 + 5000),
    ("c-old", "deploy", "db", T0 - 10_000, T0 - 9_990),
]
# (id, kind, service, valid_from, valid_to)
EXPIRIES = [("x-cert", "cert", "db", LONG_AGO, T0 - 300)]
# incident -> [(service, onset, signature)]
INCIDENTS = {
    "inc-main": [
        ("db", T0 + 100, "latency"),
        ("api", T0 + 200, "latency"),
        ("web", T0 + 300, "latency"),
    ],
    "inc-two": [("db", T0 + 100, "error_5xx"), ("api", T0 + 200, "error_5xx")],
    "inc-exp": [("db", T0 + 100, "tls_error"), ("api", T0 + 200, "tls_error")],
    "inc-cov": [("db", T0 + 100, "latency"), ("billing", T0 + 150, "latency")],
    "inc-tie": [
        ("db", T0 + 100, "latency"),
        ("api", T0 + 250, "latency"),
        ("web", T0 + 250, "latency"),
    ],
    "inc-order": [
        ("db", T0 + 100, "latency"),
        ("api", T0 + 300, "latency"),
        ("web", T0 + 200, "latency"),
    ],
}
FUTURE_INCIDENT = "inc-future"  # opened after SNAP_T: absent from every snapshot
# (person, valid_from, valid_to) on call for team-core
ON_CALL = [("per-ann", LONG_AGO, T0), ("per-bob", T0, OPEN_END)]


async def _run(graph: str, cypher: str, **params) -> None:
    await query(graph, cypher, params)


async def _common(graph: str) -> None:
    """Services, entities and incidents: shared by the raw world and the IMPACTS-only graph."""
    await _run(graph, "UNWIND $ids AS id CREATE (:Service {id: id, name: id})", ids=SERVICES)
    await _run(
        graph,
        "UNWIND $rows AS r MATCH (s:Service {id: r[2]}) "
        "CREATE (:Change {id: r[0], kind: r[1], summary: r[0], valid_from: r[3], "
        "valid_to: $open, recorded_at: r[4]})-[:CHANGES]->(s)",
        rows=[list(c) for c in CHANGES],
        open=OPEN_END,
    )
    await _run(
        graph,
        "UNWIND $rows AS r MATCH (s:Service {id: r[2]}) "
        "CREATE (:Expiry {id: r[0], kind: r[1], summary: r[0], valid_from: r[3], "
        "valid_to: r[4], recorded_at: r[3]})-[:EXPIRES_ON]->(s)",
        rows=[list(e) for e in EXPIRIES],
    )
    rows = [
        [iid, f"{iid}-{n}", svc, onset, sig, OPENED]
        for iid, syms in INCIDENTS.items()
        for n, (svc, onset, sig) in enumerate(syms)
    ] + [[FUTURE_INCIDENT, "inc-future-0", "db", SNAP_T + 4000, "latency", SNAP_T + 4100]]
    await _run(
        graph,
        "UNWIND $rows AS r MATCH (s:Service {id: r[2]}) "
        "MERGE (i:Incident {id: r[0]}) ON CREATE SET i.opened_at = r[5] "
        "CREATE (i)-[:HAS]->(:Symptom {id: r[1], service_id: r[2], onset: r[3], signature: r[4]})"
        "-[:ON]->(s)",
        rows=rows,
    )


async def write_world(graph: str) -> None:
    """The raw bitemporal world (what the snapshot builder starts from)."""
    await _common(graph)
    await _run(
        graph,
        "UNWIND $rows AS r MATCH (a:Service {id: r[0]}), (b:Service {id: r[1]}) "
        "CREATE (a)-[:DEPENDS_ON {valid_from: r[2], valid_to: r[3], recorded_at: r[2]}]->(b)",
        rows=[list(d) for d in DEPENDS],
    )
    await _run(
        graph,
        "UNWIND $rows AS r MATCH (a:Service {id: r[0]}), (b:Service {id: r[1]}) "
        "CREATE (a)-[:OBSERVED_CALL {first_seen: $lo, last_seen: $hi, count: r[2]}]->(b)",
        rows=[list(o) for o in OBSERVED],
        lo=LONG_AGO,
        hi=SNAP_T,
    )
    await _run(
        graph,
        "CREATE (t:Team {id: 'team-core', name: 'Core'}) WITH t "
        "MATCH (s:Service) "
        "CREATE (t)-[:OWNS {valid_from: $lo, valid_to: $open, recorded_at: $lo}]->(s)",
        lo=LONG_AGO,
        open=OPEN_END,
    )
    await _run(
        graph,
        "MATCH (t:Team {id: 'team-core'}) UNWIND $rows AS r "
        "CREATE (p:Person {id: r[0], name: r[0]})-[:MEMBER_OF]->(t), "
        "(p)-[:ON_CALL {valid_from: r[1], valid_to: r[2], recorded_at: r[1]}]->(t)",
        rows=[list(o) for o in ON_CALL],
    )


async def write_impacts_only(graph: str) -> None:
    """Nodes plus IMPACTS edges written directly (no snapshot builder): the predicate's
    pure input. Mirrors what the builder derives at SNAP_T in the true view."""
    await _common(graph)
    edges = [(callee, caller) for caller, callee, vf, vt in DEPENDS if vf <= SNAP_T < vt]
    edges += [(callee, caller) for caller, callee, n in OBSERVED if n >= OBSERVED_CALL_THRESHOLD]
    await _run(
        graph,
        "UNWIND $rows AS r MATCH (a:Service {id: r[0]}), (b:Service {id: r[1]}) "
        "MERGE (a)-[:IMPACTS]->(b)",
        rows=[list(e) for e in edges],
    )
    await _run(graph, "MATCH (c:Change)-[:CHANGES]->(s) CREATE (c)-[:IMPACTS]->(s)")
    await _run(graph, "MATCH (e:Expiry)-[:EXPIRES_ON]->(s) CREATE (e)-[:IMPACTS]->(s)")
