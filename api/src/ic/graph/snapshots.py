"""As-of snapshot views (KTD2) with derived IMPACTS edges (KTD9).

Variable-length WHERE filters apply after traversal, so each view is materialized:
copy the source graph, prune to time ``t``, then derive IMPACTS edges.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ic.graph import naming
from ic.graph.client import copy_graph, delete_graph, query
from ic.graph.schema import OBSERVED_CALL_THRESHOLD, OPEN_END

_BITEMPORAL_RELS = ("DEPENDS_ON", "OWNS", "ON_CALL")
_NEG_INF = -(2**62)


@dataclass
class SnapshotHandles:
    true_graph: str
    known_graph: str
    t: int
    build_ms: float


async def create_session_world(sid: str) -> str:
    name = naming.session_world(sid)
    await copy_graph(naming.seed_world(), name)
    return name


async def _prune(g: str, t: int, view: str) -> None:
    p = {"t": t, "ninf": _NEG_INF, "pinf": OPEN_END * 10}
    stmts = []
    for rel in _BITEMPORAL_RELS:
        stmts.append(
            f"MATCH ()-[e:{rel}]->() WHERE NOT (coalesce(e.valid_from, $ninf) <= $t "
            "AND $t < coalesce(e.valid_to, $pinf)) DELETE e"
        )
    stmts += [
        "MATCH (c:Change) WHERE c.valid_from > $t DETACH DELETE c",
        "MATCH ()-[e:OBSERVED_CALL]->() WHERE e.first_seen > $t DELETE e",
        "MATCH (s:Symptom) WHERE s.onset > $t DETACH DELETE s",
        "MATCH (r:Recommendation)<-[:RECOMMENDS]-(:Postmortem)-[:ABOUT]->(i:Incident) "
        "WHERE i.opened_at > $t DETACH DELETE r",
        "MATCH (pm:Postmortem)-[:ABOUT]->(i:Incident) WHERE i.opened_at > $t DETACH DELETE pm",
        "MATCH (i:Incident)-[:HAS]->(s:Symptom) WHERE i.opened_at > $t DETACH DELETE s",
        "MATCH (i:Incident) WHERE i.opened_at > $t DETACH DELETE i",
        "MATCH (r:Recommendation) WHERE r.opened_at > $t DETACH DELETE r",
    ]
    if view == "known":
        stmts.append(
            "MATCH (c:Change) WHERE c.recorded_at IS NULL OR c.recorded_at > $t DETACH DELETE c"
        )
        for rel in _BITEMPORAL_RELS:
            stmts.append(
                f"MATCH ()-[e:{rel}]->() WHERE e.recorded_at IS NULL OR e.recorded_at > $t DELETE e"
            )
    for s in stmts:
        await query(g, s, p)


async def _materialize_impacts(g: str, t: int, view: str) -> None:
    known = " AND e.recorded_at IS NOT NULL AND e.recorded_at <= $t" if view == "known" else ""
    p = {"t": t, "thr": OBSERVED_CALL_THRESHOLD}
    stmts = [
        "MATCH (c:Change)-[:CHANGES]->(s:Service) MERGE (c)-[:IMPACTS]->(s)",
        "MATCH (e:Expiry)-[:EXPIRES_ON]->(s:Service) "
        f"WHERE e.valid_to <= $t{known} MERGE (e)-[:IMPACTS]->(s)",
        "MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) MERGE (b)-[:IMPACTS]->(a)",
        "MATCH (a:Service)-[o:OBSERVED_CALL]->(b:Service) "
        "WHERE o.count >= $thr AND o.first_seen <= $t MERGE (b)-[:IMPACTS]->(a)",
    ]
    for s in stmts:
        await query(g, s, p)


async def _build_one(source: str, target: str, t: int, view: str, source_name: str) -> None:
    t0 = time.perf_counter()
    await copy_graph(source, target)
    await _prune(target, t, view)
    await _materialize_impacts(target, t, view)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    await query(
        target,
        "CREATE (:SnapshotMeta {t: $t, view: $view, source: $source, build_ms: $ms})",
        {"t": t, "view": view, "source": source_name, "ms": ms},
    )


async def build_view(source_graph: str, target_graph: str, t: int, view: str) -> None:
    """One materialized as-of view under a caller-chosen name (benchmark worlds use
    ``naming.bench`` names). ``view`` is ``true`` or ``known``."""
    if view not in naming.VIEWS:
        raise ValueError(f"invalid view: {view!r}")
    await _build_one(source_graph, target_graph, t, view, source_graph)


async def build_snapshots(
    source_graph: str, sid: str, iid: str, t: int, *, kind: str = "asof"
) -> SnapshotHandles:
    names = {"asof": naming.asof, "verify": naming.verify, "pr_verify": naming.pr_verify}
    if kind not in names:
        raise ValueError(f"invalid snapshot kind: {kind!r}")
    name = names[kind]
    true_g, known_g = name(sid, iid, "true"), name(sid, iid, "known")
    t0 = time.perf_counter()
    await asyncio.gather(
        _build_one(source_graph, true_g, t, "true", source_graph),
        _build_one(source_graph, known_g, t, "known", source_graph),
    )
    return SnapshotHandles(true_g, known_g, t, round((time.perf_counter() - t0) * 1000, 1))


async def delete_snapshots(handles: SnapshotHandles) -> None:
    await delete_graph(handles.true_graph)
    await delete_graph(handles.known_graph)
