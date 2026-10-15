"""Read the oracle's inputs (KTD23) back out of a snapshot graph.

Used by tests and, later, the benchmark to evaluate ``causality`` on a real snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from ic.graph.client import query


@dataclass(frozen=True)
class SymptomRow:
    service_id: str
    onset: int
    signature: str


@dataclass(frozen=True)
class WindowEntity:
    id: str
    service_id: str
    event_time: int
    sig_kind: str
    node_type: str  # "Change" | "Expiry"


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


async def read_impact_graph(snapshot_graph: str) -> nx.DiGraph:
    """IMPACTS edges of a snapshot as a digraph over domain ids."""
    g = nx.DiGraph()
    for a, b in await _rows(snapshot_graph, "MATCH (a)-[:IMPACTS]->(b) RETURN a.id, b.id"):
        g.add_edge(a, b)
    return g


async def read_symptoms(snapshot_graph: str, incident_id: str) -> list[SymptomRow]:
    rows = await _rows(
        snapshot_graph,
        "MATCH (:Incident {id: $iid})-[:HAS]->(s:Symptom)-[:ON]->(v:Service) "
        "RETURN v.id, s.onset, s.signature ORDER BY s.onset, v.id",
        {"iid": incident_id},
    )
    return [SymptomRow(v, o, sig) for v, o, sig in rows]


async def read_window_entities(
    snapshot_graph: str, earliest_onset: int, window_minutes: int = 120
) -> list[WindowEntity]:
    """Changes (event time = valid_from) and expiries (valid_to) from ``window_minutes``
    before the earliest onset up to the snapshot time. Entities after the onset are
    included on purpose so the oracle can show they fail ``after_onset``."""
    lo = earliest_onset - window_minutes * 60
    meta = await _rows(snapshot_graph, "MATCH (m:SnapshotMeta) RETURN m.t LIMIT 1")
    hi = meta[0][0] if meta else 2**62
    p = {"lo": lo, "hi": hi}
    out = [
        WindowEntity(i, s, t, k, "Change")
        for i, s, t, k in await _rows(
            snapshot_graph,
            "MATCH (c:Change)-[:CHANGES]->(s:Service) WHERE c.valid_from >= $lo AND "
            "c.valid_from <= $hi RETURN c.id, s.id, c.valid_from, c.kind ORDER BY c.id",
            p,
        )
    ]
    out += [
        WindowEntity(i, s, t, k, "Expiry")
        for i, s, t, k in await _rows(
            snapshot_graph,
            "MATCH (e:Expiry)-[:EXPIRES_ON]->(s:Service) WHERE e.valid_to >= $lo AND "
            "e.valid_to <= $hi RETURN e.id, s.id, e.valid_to, e.kind ORDER BY e.id",
            p,
        )
    ]
    return out
