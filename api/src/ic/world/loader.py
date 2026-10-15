"""Bulk-load a World into FalkorDB.

Builds into a temporary graph (indexes first, then UNWIND batches), then replaces the
target graph with GRAPH.COPY so readers never see a half-built ``seed:world``.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from ic.graph.client import get_db
from ic.graph.schema import Label, Rel, index_statements
from ic.world.embed import Embedder, get_embedder
from ic.world.model import World

BATCH = 5000
SEED_GRAPH = "seed:world"

# label -> (World attribute, fields that are references/lists and so stay out of node props)
NODE_SPECS: list[tuple[Label, str, set[str]]] = [
    (Label.SERVICE, "services", set()),
    (Label.TEAM, "teams", set()),
    (Label.PERSON, "people", {"team_id"}),
    (Label.CONFIG, "configs", {"service_id"}),
    (Label.FLAG, "flags", {"service_id", "config_id"}),
    (Label.CHANGE, "changes", {"author_id", "previous_id", "touches_id", "target_key"}),
    (Label.EXPIRY, "expiries", set()),
    (Label.INCIDENT, "incidents", {"cause_id", "remediation_id", "coupling", "decoy_ids"}),
    (Label.SYMPTOM, "symptoms", {"incident_id"}),
    (Label.POSTMORTEM, "postmortems", {"incident_id"}),
    (Label.RECOMMENDATION, "recommendations", {"postmortem_id", "target_ids"}),
    (Label.ADR, "adrs", {"service_ids"}),
]

EdgeKey = tuple[Rel, Label, Label]


def node_rows(world: World) -> dict[Label, list[dict]]:
    rows = {}
    for label, attr, skip in NODE_SPECS:
        rows[label] = [
            {k: v for k, v in asdict(o).items() if k not in skip and v is not None}
            for o in getattr(world, attr)
        ]
    return rows


def edge_rows(world: World) -> dict[EdgeKey, list[dict]]:
    """{(rel, from_label, to_label): [{f, t, p}, ...]} with f/t domain ids and p edge props."""
    out: dict[EdgeKey, list[dict]] = {}

    def add(rel: Rel, fl: Label, f: str, tl: Label, t: str, **props: int) -> None:
        out.setdefault((rel, fl, tl), []).append({"f": f, "t": t, "p": props})

    S, T, P, C = Label.SERVICE, Label.TEAM, Label.PERSON, Label.CHANGE
    for d in world.dependencies:
        add(
            Rel.DEPENDS_ON,
            S,
            d.caller_id,
            S,
            d.callee_id,
            valid_from=d.valid_from,
            valid_to=d.valid_to,
            recorded_at=d.recorded_at,
        )
    for o in world.observed_calls:
        add(
            Rel.OBSERVED_CALL,
            S,
            o.caller_id,
            S,
            o.callee_id,
            first_seen=o.first_seen,
            last_seen=o.last_seen,
            count=o.count,
        )
    for o in world.ownerships:
        add(
            Rel.OWNS,
            T,
            o.team_id,
            S,
            o.service_id,
            valid_from=o.valid_from,
            valid_to=o.valid_to,
            recorded_at=o.recorded_at,
        )
    for p in world.people:
        add(Rel.MEMBER_OF, P, p.id, T, p.team_id)
    for s in world.oncall:
        add(
            Rel.ON_CALL,
            P,
            s.person_id,
            T,
            s.team_id,
            valid_from=s.valid_from,
            valid_to=s.valid_to,
            recorded_at=s.recorded_at,
        )
    flag_or_config = {f.id: Label.FLAG for f in world.flags} | {
        c.id: Label.CONFIG for c in world.configs
    }
    for c in world.changes:
        add(Rel.CHANGES, C, c.id, S, c.service_id)
        add(Rel.AUTHORED, P, c.author_id, C, c.id)
        if c.touches_id:
            add(Rel.TOUCHES, C, c.id, flag_or_config[c.touches_id], c.touches_id)
        if c.previous_id:
            add(Rel.PREVIOUS_VERSION, C, c.id, C, c.previous_id)
    for f in world.flags:
        add(Rel.GATED_BY, Label.FLAG, f.id, Label.CONFIG, f.config_id)
        add(Rel.HAS_FLAG, S, f.service_id, Label.FLAG, f.id)
    for c in world.configs:
        add(Rel.HAS_CONFIG, S, c.service_id, Label.CONFIG, c.id)
    for e in world.expiries:
        add(Rel.EXPIRES_ON, Label.EXPIRY, e.id, S, e.service_id)
    for s in world.symptoms:
        add(Rel.HAS, Label.INCIDENT, s.incident_id, Label.SYMPTOM, s.id)
        add(Rel.ON, Label.SYMPTOM, s.id, S, s.service_id)
    for i in world.incidents:
        cause_label = C if i.cause_kind == "change" else Label.EXPIRY
        add(Rel.CAUSED_BY, Label.INCIDENT, i.id, cause_label, i.cause_id)
        add(Rel.REMEDIATED_BY, Label.INCIDENT, i.id, C, i.remediation_id)
    for p in world.postmortems:
        add(Rel.ABOUT, Label.POSTMORTEM, p.id, Label.INCIDENT, p.incident_id)
    for r in world.recommendations:
        add(Rel.RECOMMENDS, Label.POSTMORTEM, r.postmortem_id, Label.RECOMMENDATION, r.id)
        for sid in r.target_ids:
            add(Rel.TARGETS, Label.RECOMMENDATION, r.id, S, sid)
    for a in world.adrs:
        for sid in a.service_ids:
            add(Rel.DECIDED, Label.ADR, a.id, S, sid)
    return out


def _chunks(rows: list, size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


async def _delete_if_exists(db, name: str) -> None:
    if name in await db.list_graphs():
        await db.select_graph(name).delete()


async def load_world(
    world: World, graph_name: str = SEED_GRAPH, embedder: Embedder | None = None
) -> None:
    """Replace ``graph_name`` with ``world``. Idempotent: re-running never duplicates data."""
    embedder = embedder or get_embedder()
    db = get_db()
    building = f"{graph_name}:building"
    await _delete_if_exists(db, building)
    g = db.select_graph(building)
    try:
        for stmt in index_statements(embedder.dims):
            await g.query(stmt)

        nodes = node_rows(world)
        pm_texts = [
            f"{p['title']}\n{p['symptoms_text']}\n{p['body']}" for p in nodes[Label.POSTMORTEM]
        ]
        vectors = await asyncio.to_thread(embedder.embed, pm_texts)
        for row, vec in zip(nodes[Label.POSTMORTEM], vectors, strict=True):
            row["embedding"] = vec

        for label, rows in nodes.items():
            q = f"UNWIND $rows AS r CREATE (n:{label}) SET n += r"
            if label == Label.POSTMORTEM:  # the vector index needs a vecf32 property
                q += ", n.embedding = vecf32(r.embedding)"
            for batch in _chunks(rows):
                await g.query(q, {"rows": batch})

        for (rel, fl, tl), rows in edge_rows(world).items():
            q = (
                f"UNWIND $rows AS r MATCH (a:{fl} {{id: r.f}}), (b:{tl} {{id: r.t}}) "
                f"CREATE (a)-[e:{rel}]->(b) SET e += r.p"
            )
            for batch in _chunks(rows):
                await g.query(q, {"rows": batch})

        await _delete_if_exists(db, graph_name)
        await g.copy(graph_name)
    finally:
        await _delete_if_exists(db, building)
