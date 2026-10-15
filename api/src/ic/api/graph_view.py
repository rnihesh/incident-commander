"""Incident snapshots on demand and the subgraph the console draws from them."""

from __future__ import annotations

from fastapi import HTTPException

from ic.api.state import AppState
from ic.graph import naming
from ic.graph.client import graph_exists, query
from ic.graph.snapshots import build_snapshots


async def _incident_time(s: AppState, sid: str, iid: str) -> int:
    """The alert time to rebuild snapshots at: the anchored t, else the incident's."""
    anchor = s.store.get_anchor(sid, iid)
    if anchor and anchor.get("t") is not None:
        return int(anchor["t"])
    rows = (
        await query(
            naming.session_world(sid),
            "MATCH (i:Incident {id: $iid}) RETURN i.opened_at",
            {"iid": iid},
            read_only=True,
        )
    ).result_set
    if not rows or rows[0][0] is None:
        raise HTTPException(404, "unknown incident")
    return int(rows[0][0])


class SnapshotsNotReady(Exception):
    """The incident's run is still building or using its snapshots; retry shortly."""


async def _ensure_snapshots(s: AppState, sid: str, iid: str) -> tuple[str, str]:
    """The incident's as-of graph names (true, known), rebuilding them when missing.

    Never rebuilds while the incident's run is in flight: the run owns its snapshots, and
    rebuilding (copy deletes the target first) would replace graphs triage is querying."""
    world = naming.session_world(sid)
    true_g, known_g = (naming.asof(sid, iid, v) for v in ("true", "known"))
    async with s.lock_for(f"snap:{sid}:{iid}"):
        if not (await graph_exists(true_g) and await graph_exists(known_g)):
            if s.runs.active_for(sid, iid):
                raise SnapshotsNotReady(iid)
            t = await _incident_time(s, sid, iid)
            s.sessions.track_graph(sid, true_g)
            s.sessions.track_graph(sid, known_g)
            await build_snapshots(world, sid, iid, t)
    return true_g, known_g


GRAPH_MAX_HOPS = 6
GRAPH_MAX_NODES = 400
GRAPH_CHANGE_WINDOW_S = 120 * 60


async def _ro(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


async def _graph_payload(known_g: str, iid: str) -> dict:
    """The subgraph the console draws, from the as-known snapshot: symptomatic services plus
    everything in their reverse IMPACTS reach (<= 6 hops), nearest first, capped."""
    meta = await _ro(known_g, "MATCH (m:SnapshotMeta) RETURN m.t LIMIT 1")
    t = int(meta[0][0]) if meta else 0
    sym_rows = await _ro(
        known_g,
        "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom)-[:ON]->(s:Service) "
        "RETURN s.id, y.onset, y.signature ORDER BY y.onset, y.id",
        {"iid": iid},
    )
    symptoms: dict[str, tuple[int | None, str | None]] = {}
    for sid_, onset, sig in sym_rows:
        symptoms.setdefault(sid_, (onset, sig))  # earliest onset first
    alert_rows = await _ro(
        known_g, "MATCH (i:Incident {id: $iid}) RETURN i.alert_service_id", {"iid": iid}
    )
    alert = alert_rows[0][0] if alert_rows else None

    hop: dict[str, int] = {}
    seeds = sorted(symptoms)
    if alert and alert not in symptoms:
        seeds.append(alert)
    for sid_ in seeds:
        hop[sid_] = 0
    frontier = list(seeds)
    for d in range(1, GRAPH_MAX_HOPS + 1):
        if not frontier or len(hop) >= GRAPH_MAX_NODES:
            break
        rows = await _ro(
            known_g,
            "MATCH (u:Service)-[:IMPACTS]->(s:Service) WHERE s.id IN $f RETURN DISTINCT u.id",
            {"f": frontier},
        )
        frontier = sorted({r[0] for r in rows} - hop.keys())
        room = GRAPH_MAX_NODES - len(hop)
        frontier = frontier[:room]
        for u in frontier:
            hop[u] = d
    if len(hop) > GRAPH_MAX_NODES:  # more symptomatic services than the cap: keep the first
        keep = sorted(hop, key=lambda i: (hop[i], i))[:GRAPH_MAX_NODES]
        hop = {i: hop[i] for i in keep}
    ids = sorted(hop, key=lambda i: (hop[i], i))

    node_rows = await _ro(
        known_g,
        "MATCH (s:Service) WHERE s.id IN $ids RETURN s.id, s.name, s.region, s.tier, s.x, s.y",
        {"ids": ids},
    )
    by_id = {r[0]: r for r in node_rows}
    nodes = []
    for i in ids:
        if i not in by_id:
            continue
        _, name, region, tier, x, y = by_id[i]
        onset, sig = symptoms.get(i, (None, None))
        nodes.append(
            {
                "id": i,
                "name": name,
                "region": region,
                "tier": tier,
                "x": x,
                "y": y,
                "symptomatic": i in symptoms,
                "onset": onset,
                "signature": sig,
                "alerting": i == alert,
            }
        )
    kept = [n["id"] for n in nodes]
    edges = [
        {"source": r[0], "target": r[1]}
        for r in await _ro(
            known_g,
            "MATCH (a:Service)-[:IMPACTS]->(b:Service) "
            "WHERE a.id IN $ids AND b.id IN $ids RETURN DISTINCT a.id, b.id ORDER BY a.id, b.id",
            {"ids": kept},
        )
    ]
    changes = [
        {"id": r[0], "service_id": r[1], "kind": r[2], "valid_from": r[3], "recorded_at": r[4]}
        for r in await _ro(
            known_g,
            "MATCH (c:Change)-[:CHANGES]->(s:Service) "
            "WHERE s.id IN $ids AND $lo <= c.valid_from AND c.valid_from <= $t "
            "RETURN c.id, s.id, c.kind, c.valid_from, c.recorded_at "
            "ORDER BY c.valid_from DESC, c.id",
            {"ids": kept, "lo": t - GRAPH_CHANGE_WINDOW_S, "t": t},
        )
    ]
    return {
        "incident_id": iid,
        "t": t,
        "alert_service_id": alert,
        "nodes": nodes,
        "edges": edges,
        "changes": changes,
    }
