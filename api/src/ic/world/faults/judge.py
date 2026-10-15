"""Judge mode (U14, KTD11): grading a verdict against the revealed answer and the
target catalog the judge picks an injection target from.
"""

from __future__ import annotations

from typing import Literal

import networkx as nx

from ic.graph.schema import (
    WORLD_NOW,
)
from ic.world.faults.common import TRAINING_CLASSES, _impact_services, _rows
from ic.world.faults.heldout import FAULT_CLASSES

Comparison = Literal["match", "partial", "miss", "no_claim"]
ALERT_PATH_MIN_CALLERS = 2  # _plan needs >= 2 symptomatic services upstream of the target
ALERT_PATH_HOPS = 3  # ... within this many IMPACTS hops
CHANGE_KIND_CLASS = {str(k): c for c, k in TRAINING_CLASSES.items()}


def compare(
    answer: dict, outcome: str | None, suspect_id: str | None, claimed: str | None
) -> tuple[Comparison, str]:
    """Grade a verdict against the revealed answer (KTD11): ``match`` = the same root entity
    id; ``partial`` = same fault class, different root; ``no_claim`` = INCONCLUSIVE, FAILED
    or no verdict; ``miss`` = a DIAGNOSED verdict wrong on both counts."""
    if outcome != "DIAGNOSED" or not suspect_id:
        return "no_claim", f"the agent made no claim ({outcome or 'no verdict'})"
    if suspect_id == answer["root_id"]:
        return "match", f"the agent named the committed root {suspect_id}"
    if claimed is not None and claimed == answer["fault_class"]:
        return (
            "partial",
            f"same fault class ({claimed}) but the agent named {suspect_id}, "
            f"not {answer['root_id']}",
        )
    return "miss", f"the agent named {suspect_id} ({claimed or 'unknown class'})"


async def claimed_class(world_graph: str, entity_id: str | None) -> str | None:
    """The fault class a verdict's root entity implies: a Change's kind (deploy, config,
    flag) or ``expiry`` for an Expiry. An undeclared coupling's root is a Change, so a
    correct coupling diagnosis grades as a match by id, never by class."""
    if not entity_id:
        return None
    rows = await _rows(world_graph, "MATCH (c:Change {id: $id}) RETURN c.kind", {"id": entity_id})
    if rows:
        return CHANGE_KIND_CLASS.get(str(rows[0][0]))
    rows = await _rows(world_graph, "MATCH (e:Expiry {id: $id}) RETURN e.id", {"id": entity_id})
    return "expiry" if rows else None


async def reference_time(world_graph: str) -> int:
    """The world's "now" for judge targeting: the latest change or incident, or WORLD_NOW.
    The next injection's alert time lands 3-6 h after it."""
    c = (await _rows(world_graph, "MATCH (c:Change) RETURN max(c.valid_from)"))[0][0]
    i = (await _rows(world_graph, "MATCH (i:Incident) RETURN max(i.opened_at)"))[0][0]
    return max(WORLD_NOW, c or 0, i or 0)


def _alert_path_reason(reach: int) -> str | None:
    if reach >= ALERT_PATH_MIN_CALLERS:
        return None
    if reach == 0:
        return "nothing depends on this service, so a fault here raises no symptom upstream"
    return (
        f"only {reach} service sits upstream within {ALERT_PATH_HOPS} hops; an alert path "
        f"needs at least {ALERT_PATH_MIN_CALLERS} symptomatic services"
    )


async def target_catalog(world_graph: str) -> dict:
    """Every service of a world with whether a fault there can raise symptoms upstream
    (callers reachable within the injector's hop limit), for the judge's target picker:
    ``{t_ref, services, edges}`` with edges in the IMPACTS direction (callee -> caller)."""
    t = await reference_time(world_graph)
    g = await _impact_services(world_graph, t)
    rows = await _rows(
        world_graph,
        "MATCH (s:Service) "
        "OPTIONAL MATCH (s)-[:HAS_CONFIG]->(c:Config) WITH s, count(c) AS nc "
        "OPTIONAL MATCH (s)-[:HAS_FLAG]->(f:Flag) "
        "RETURN s.id, s.name, s.region, s.tier, s.domain, s.x, s.y, nc, count(f) "
        "ORDER BY s.id",
    )
    out = []
    for sid, name, region, tier, domain, x, y, n_cfg, n_flag in rows:
        reach = 0
        if sid in g:
            dist = nx.single_source_shortest_path_length(g, sid, cutoff=ALERT_PATH_HOPS)
            reach = sum(1 for d in dist.values() if d >= 1)
        reason = _alert_path_reason(reach)
        unavailable: dict[str, str] = {}
        if reason:
            for fc in FAULT_CLASSES:
                if fc != "undeclared_coupling":  # that class creates its own call edge
                    unavailable[fc] = reason
        if not n_cfg:
            unavailable.setdefault("config_change", "this service has no config keys")
        if not n_flag:
            unavailable.setdefault("flag_flip", "this service has no feature flags")
        out.append(
            {
                "id": sid,
                "name": name,
                "region": region,
                "tier": tier,
                "domain": domain,
                "x": x,
                "y": y,
                "upstream_services": reach,
                "has_upstream_path": reason is None,
                "reason": reason,
                "unavailable": unavailable,
            }
        )
    edges = [{"source": a, "target": b} for a, b in sorted(g.edges)]
    return {"t_ref": t, "services": out, "edges": edges}


async def alert_path_check(world_graph: str, service_id: str, fault_class: str) -> str | None:
    """None when a ``fault_class`` fault on ``service_id`` can produce an alert path, else
    the reason it cannot (unknown service, too few callers, no config keys or flags)."""
    catalog = (await target_catalog(world_graph))["services"]
    entry = next((s for s in catalog if s["id"] == service_id), None)
    if entry is None:
        return f"unknown service {service_id!r}"
    return entry["unavailable"].get(fault_class)
