"""Alert-storm collapse (U17, KTD13): hundreds of pages -> a handful of explained incidents.

``collapse_storm`` runs on a storm's as-of snapshot and keeps every query it ran:

  a. pages -> symptomatic services: a service's symptom is its earliest page (onset) and
     the page signature(s); repeats and flapping duplicates collapse onto it.
  b. symptom groups by connected blast area: a symptomatic service's group is itself plus
     every symptomatic service it reaches over IMPACTS (<= ``STORM_REACH_DEPTH`` hops).
     Each Change/Expiry on the service inside the group's alert window is checked with the
     KTD23 predicate (``ic.tools.predicate``'s clauses, symptoms read from the pages).
  c. an alert-candidate bipartite graph (candidate -[:EXPLAINS]-> page) is written to the
     scratch graph ``s:{sid}:storm:{storm_id}`` and grouped with ``algo.WCC``: each weakly
     connected component is one independent incident.
  d. a greedy set cover per component over the candidates' reach (pages they explain) picks
     the minimal explaining set. KTD13 joint causes: a flag flip and the config change its
     flag is GATED_BY, both qualifying in the window, where *each alone* was active during
     a symptom-free interval longer than the delay between "both active" and the first
     symptom. Neither can then explain the pages alone; the pair is one set-cover unit and
     the component is ``jointly_caused``. A change with no such interval stays a single
     cause even when its partner was in the window (so a harmless earlier flip never makes
     a false joint).
  e. seeded ``algo.betweenness`` over the IMPACTS subgraph of the storm area (symptomatic
     services and what reaches them within ``AREA_HOPS``), copied into the scratch graph,
     names the chokepoints.
  f. the scratch graph is deleted before the tool returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from textwrap import dedent

from pydantic import Field

from ic.graph import naming
from ic.graph.client import delete_graph, query
from ic.graph.schema import OPEN_END
from ic.locks import KeyedLocks
from ic.tools import predicate as P
from ic.tools.investigate import BETWEENNESS, BETWEENNESS_SAMPLES, BETWEENNESS_SEED, CANONICAL_PATH
from ic.tools.registry import ScopeError, ToolContext, ToolInput, register
from ic.tools.result import QueryLog, ToolError, ToolResult

STORM_REACH_DEPTH = 6  # same bound the storm generator keeps faults apart by
AREA_HOPS = 2
AREA_MAX = 400
TOP_CHOKEPOINTS = 5
SOLO_LOOKBACK_S = 7 * 86400  # how far back "alone and symptom-free" evidence is looked for
SOLO_CHECKS = 4  # intervals tried per candidate before giving up
_locks: KeyedLocks[str] = KeyedLocks()

STORM = """
MATCH (st:Storm {id: $storm_id})
OPTIONAL MATCH (m:SnapshotMeta)
RETURN st.id AS storm_id, st.started_at AS started_at, st.ended_at AS ended_at,
       max(m.t) AS snapshot_t
"""

PAGES = """
MATCH (:Storm {id: $storm_id})<-[:PART_OF]-(a:Alert)-[:FOR]->(s:Service)
RETURN a.id AS alert_id, s.id AS service_id, a.fired_at AS fired_at, a.signature AS signature
ORDER BY fired_at, alert_id
"""

BLAST = """
MATCH (s:Service) WHERE s.id IN $symptomatic
OPTIONAL MATCH (s)-[:IMPACTS*1..{depth}]->(t:Service) WHERE t.id IN $symptomatic
RETURN s.id AS service_id, collect(DISTINCT t.id) AS reaches
ORDER BY service_id
"""

# The KTD23 predicate with the storm's pages as the symptoms: one symptom per (service,
# signature) in the group, onset = its earliest page. The clause logic is predicate.EXPLAINS
# verbatim from its second WITH on.
_EXPLAINS_SPLIT = "WITH c, home,\n"
STORM_EXPLAINS = (
    """
MATCH (c:{label} {{id: $entity_id}})-[:CHANGES|EXPIRES_ON]->(home:Service)
MATCH (:Storm {{id: $storm_id}})<-[:PART_OF]-(a:Alert)-[:FOR]->(s:Service)
WHERE s.id IN $group
WITH c, home, s, a.signature AS signature, min(a.fired_at) AS onset
WITH c, home, s, {{onset: onset, signature: signature}} AS sym,
     shortestPath((c)-[:IMPACTS*]->(s)) AS path
"""
    + _EXPLAINS_SPLIT
    + P.EXPLAINS.split(_EXPLAINS_SPLIT, 1)[1]
)

PARTNERS = """
MATCH (f:Change)-[:TOUCHES]->(flag:Flag)-[:GATED_BY]->(cfg:Config)<-[:TOUCHES]-(c:Change)
WHERE f.id IN $ids AND c.id IN $ids AND f.kind = 'flag' AND c.kind = 'config'
MATCH (f)-[:CHANGES]->(s:Service)<-[:CHANGES]-(c)
RETURN f.id AS flag_change, c.id AS config_change, flag.id AS flag, cfg.id AS config,
       s.id AS service_id
ORDER BY flag_change, config_change
"""

CHAINS = """
MATCH (c:Change)-[:TOUCHES]->(x) WHERE x.id IN $targets AND $lo <= c.valid_from
  AND c.valid_from <= $hi
RETURN x.id AS target, c.id AS change_id, c.value AS value, c.prev_value AS prev_value,
       c.valid_from AS valid_from
ORDER BY target, valid_from, change_id
"""

SYMPTOM_FREE = """
MATCH (s:Service) WHERE s.id IN $services
OPTIONAL MATCH (y:Symptom)-[:ON]->(s) WHERE $lo <= y.onset AND y.onset < $hi
WITH s, count(y) AS symptoms
OPTIONAL MATCH (a:Alert)-[:FOR]->(s) WHERE $lo <= a.fired_at AND a.fired_at < $hi
WITH s, symptoms, count(a) AS pages
RETURN sum(symptoms) AS symptoms, sum(pages) AS pages
"""

AREA = """
MATCH (s:Service) WHERE s.id IN $symptomatic
OPTIONAL MATCH (u:Service)-[:IMPACTS*1..{hops}]->(s)
WITH collect(DISTINCT s) + collect(DISTINCT u) AS area
UNWIND area AS n
WITH DISTINCT n
RETURN n.id AS id, n.name AS name, n.x AS x, n.y AS y
ORDER BY id
LIMIT $cap
"""

AREA_EDGES = """
MATCH (a:Service)-[:IMPACTS]->(b:Service) WHERE a.id IN $ids AND b.id IN $ids
RETURN a.id AS source, b.id AS target ORDER BY source, target
"""

# --- scratch-graph writes (s:{sid}:storm:{storm_id}) -------------------------------------
W_INDEX = [
    "CREATE INDEX FOR (k:Candidate) ON (k.id)",
    "CREATE INDEX FOR (s:Service) ON (s.id)",
]
W_CANDIDATES = """
UNWIND $candidates AS k
CREATE (:Candidate {id: k.id, service_id: k.service_id})
"""
W_ALERTS = """
UNWIND $pages AS p
CREATE (a:Alert {id: p.id, service_id: p.service_id})
WITH a, p
UNWIND p.candidates AS cid
MATCH (k:Candidate {id: cid})
CREATE (k)-[:EXPLAINS]->(a)
"""
WCC = """
CALL algo.WCC({nodeLabels: ['Alert', 'Candidate'], relationshipTypes: ['EXPLAINS']})
YIELD node, componentId
RETURN CASE WHEN node:Alert THEN 'Alert' ELSE 'Candidate' END AS kind, node.id AS id,
       componentId
ORDER BY componentId, kind, id
"""
W_AREA_NODES = """
UNWIND $ids AS id
CREATE (:Service {id: id})
"""
W_AREA_EDGES = """
UNWIND $edges AS e
MATCH (a:Service {id: e.source}), (b:Service {id: e.target})
CREATE (a)-[:IMPACTS]->(b)
"""


class StormArgs(ToolInput):
    storm_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Storm id, e.g. storm-001")


class _Log(QueryLog):
    """A QueryLog that also records the writes on the storm's scratch graph."""

    def __init__(self, graph: str, scratch: str):
        super().__init__(graph)
        self.scratch = scratch
        self.scratch_queries: list[int] = []

    async def write(self, cypher: str, params: dict | None = None) -> list[list]:
        self.cypher.append(dedent(cypher).strip())
        self.params.append(dict(params or {}))
        self.scratch_queries.append(self.last_index)
        res = await query(self.scratch, self.cypher[-1], self.params[-1])
        return res.result_set


@dataclass
class _Checked:
    cand: P.Candidate
    group: list[str]
    ok: bool
    clause: str | None
    detail: str
    hops: dict[str, int | None]
    query: int
    solo: dict | None = None  # alone-and-symptom-free evidence when it has a partner
    partner: str | None = None

    def as_dict(self) -> dict:
        c = self.cand
        return {
            "id": c.id,
            "entity": c.entity,
            "kind": c.kind,
            "service_id": c.service_id,
            "event_time": c.event_time,
            "summary": c.summary,
            "group": self.group,
            "qualifies": self.ok,
            "clause": self.clause,
            "detail": self.detail,
            "query": self.query,
            "partner": self.partner,
            "alone": None if self.partner is None else {"sufficient": self.solo is None},
            "solo": self.solo,
        }


@dataclass
class _Unit:
    ids: tuple[str, ...]
    reach: frozenset[str]  # page ids
    joint: dict | None = None


@dataclass
class _Component:
    pages: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


async def _explain(
    log: QueryLog, storm_id: str, group: list[str], c: P.Candidate
) -> tuple[bool, str | None, str, dict, int]:
    params = {
        "entity_id": c.id,
        "storm_id": storm_id,
        "group": group,
        "compatible": P.COMPATIBLE,
    }
    rows = await log.rows(STORM_EXPLAINS.format(label=c.entity), params)
    q = log.last_index
    if not rows:
        return False, "origin", f"{c.id} is not attached to any service", {}, q
    _, kind, home, event_time, earliest, syms, unreached, bad, clause = rows[0]
    hops = {x["service"]: x["hop"] for x in sorted(syms, key=lambda x: x["service"])}
    detail = P._detail(clause, kind, home, event_time, earliest, syms, unreached, bad)
    return clause is None, clause, detail, hops, q


def _timeline(changes: list[list], lo: int, hi: int) -> list[tuple[int, int, str]]:
    """[(from, to, value)] covering [lo, hi) for one version chain."""
    if not changes:
        return []
    segs, cur, t = [], changes[0][3], lo
    for _, _, value, _, vf in changes:
        if vf >= hi:
            break
        if vf > t:
            segs.append((t, vf, cur))
            t = vf
        cur = value
    segs.append((t, hi, cur))
    return segs


def _state(segs: list[tuple[int, int, str]], t: int) -> str | None:
    for a, b, v in segs:
        if a <= t < b:
            return v
    return None


def _alone(
    own: list[tuple[int, int, str]], other: list[tuple[int, int, str]], own_v: str, other_v: str
) -> list[tuple[int, int]]:
    """Maximal intervals where ``own`` holds ``own_v`` and ``other`` does not hold ``other_v``."""
    cuts = sorted({p for a, b, _ in own + other for p in (a, b)})
    out: list[tuple[int, int]] = []
    for a, b in zip(cuts, cuts[1:], strict=False):
        if _state(own, a) == own_v and _state(other, a) not in (other_v, None):
            if out and out[-1][1] == a:
                out[-1] = (out[-1][0], b)
            else:
                out.append((a, b))
    return out


async def _solo_evidence(
    log: QueryLog,
    pair: list,
    checked: dict[str, _Checked],
    onset: dict[str, int],
) -> dict[str, dict | None]:
    """For a (flag flip, config change) pair: the interval where each alone was active and
    no symptom or page appeared on the group, longer than the both-active-to-symptom lead."""
    f_id, c_id, flag_id, cfg_id, _ = pair
    f, c = checked[f_id], checked[c_id]
    t0 = min(onset[s] for s in f.group)
    lead = t0 - max(f.cand.event_time, c.cand.event_time)
    lo = t0 - SOLO_LOOKBACK_S
    rows = await log.rows(CHAINS, {"targets": [flag_id, cfg_id], "lo": lo, "hi": t0})
    chains_q = log.last_index
    chain = {tid: _timeline([r for r in rows if r[0] == tid], lo, t0) for tid in (flag_id, cfg_id)}
    value = {r[1]: r[2] for r in rows}
    out: dict[str, dict | None] = {}
    for me, other, mine, theirs in ((f, c, flag_id, cfg_id), (c, f, cfg_id, flag_id)):
        spans = _alone(chain[mine], chain[theirs], value[me.cand.id], value[other.cand.id])
        spans = sorted((s for s in spans if s[1] - s[0] > lead), key=lambda s: (s[0] - s[1], s))
        found = None
        for a, b in spans[:SOLO_CHECKS]:
            free = await log.rows(SYMPTOM_FREE, {"services": me.group, "lo": a, "hi": b})
            if not sum(v or 0 for v in (free[0] if free else [])):
                found = {
                    "from": a,
                    "to": b,
                    "seconds": b - a,
                    "lead_s": lead,
                    "partner": other.cand.id,
                    "queries": [chains_q, log.last_index],
                }
                break
        out[me.cand.id] = found
    return out


def _cover(component: _Component, units: list[_Unit]) -> tuple[list[_Unit], set[str]]:
    """Greedy weighted set cover: most newly explained pages per entity first."""
    uncovered = set(component.pages)
    chosen: list[_Unit] = []
    while uncovered:
        scored = [(len(u.reach & uncovered) / len(u.ids), u) for u in units]
        scored = [(g, u) for g, u in scored if g > 0]
        if not scored:
            break
        _, best = min(scored, key=lambda x: (-x[0], len(x[1].ids), x[1].ids))
        chosen.append(best)
        uncovered -= best.reach
    # Reverse delete: drop any pick the others already cover (smallest reach first), so a
    # change that explains one corner never survives next to a unit covering it all.
    for u in sorted(chosen, key=lambda u: (len(u.reach), u.ids)):
        rest = [o for o in chosen if o is not u]
        if rest and u.reach <= frozenset().union(*(o.reach for o in rest)):
            chosen = rest
    return chosen, uncovered


@register(StormArgs, with_context=True, read_only=False)
async def collapse_storm(graph: str, *, ctx: ToolContext, storm_id: str) -> ToolResult:
    """Collapse an alert storm into independent incidents: group pages by blast area and
    explaining change (algo.WCC), find each incident's minimal explaining set (flagging
    joint flag+config causes) and rank chokepoints (seeded algo.betweenness).

    Runs on the storm's snapshot; its scratch graph is deleted before it returns."""
    if not ctx.sid:
        raise ScopeError("collapse_storm needs a session for its scratch graph")
    scratch = naming.storm(ctx.sid, storm_id)
    if not ctx.allows(scratch):
        raise ScopeError(f"collapse_storm may not touch {scratch} from this run's scope")
    lock = _locks(scratch)
    async with lock:
        if ctx.track_graph is not None:
            ctx.track_graph(scratch)
        try:
            await delete_graph(scratch)
            return await _collapse(graph, scratch, storm_id)
        finally:
            await delete_graph(scratch)


async def _collapse(graph: str, scratch: str, storm_id: str) -> ToolResult:
    log = _Log(graph, scratch)
    rows = await log.rows(STORM, {"storm_id": storm_id})
    if not rows or rows[0][0] is None:
        raise ToolError(f"storm {storm_id} is not in this snapshot")
    _, started_at, ended_at, snap_t = rows[0]
    hi = snap_t if snap_t is not None else OPEN_END
    pages = await log.rows(PAGES, {"storm_id": storm_id})
    if not pages:
        raise ToolError(f"storm {storm_id} has no pages in this snapshot")

    # a. pages -> symptomatic services
    onset: dict[str, int] = {}
    sigs: dict[str, set[str]] = {}
    by_service: dict[str, list[str]] = {}
    for aid, svc, at, sig in pages:
        onset[svc] = min(onset.get(svc, at), at)
        sigs.setdefault(svc, set()).add(sig)
        by_service.setdefault(svc, []).append(aid)
    symptomatic = sorted(onset, key=lambda s: (onset[s], s))

    # b. blast groups and KTD23 per candidate, restricted to its service's group
    blast = await log.rows(BLAST.format(depth=STORM_REACH_DEPTH), {"symptomatic": symptomatic})
    group = {r[0]: sorted({r[0], *r[1]}, key=lambda s: (onset[s], s)) for r in blast}
    window_s = P.DEFAULT_WINDOW_MINUTES * 60
    lo_all = min(onset.values()) - window_s
    cands = await P.candidates_in_window(log, lo_all, hi, services=symptomatic)
    checked: dict[str, _Checked] = {}
    outside: list[str] = []
    for c in cands:
        grp = group[c.service_id]
        if c.event_time < min(onset[s] for s in grp) - window_s:
            outside.append(c.id)
            continue
        ok, clause, detail, hops, q = await _explain(log, storm_id, grp, c)
        checked[c.id] = _Checked(c, grp, ok, clause, detail, hops, q)
    qualifying = [k for k in checked.values() if k.ok]

    # d (prep). joint pairs: flag flip + the config change its flag is GATED_BY
    pairs = []
    if qualifying:
        ids = [k.cand.id for k in qualifying if k.cand.entity == "Change"]
        pairs = await log.rows(PARTNERS, {"ids": ids})
    for pair in pairs:
        evidence = await _solo_evidence(log, pair, checked, onset)
        for eid, ev in evidence.items():
            k = checked[eid]
            if k.partner is None or (k.solo is None and ev is not None):
                k.partner = pair[1] if eid == pair[0] else pair[0]
                k.solo = ev

    # c. bipartite alert-candidate graph -> algo.WCC components
    explains: dict[str, list[str]] = {}  # page -> candidates explaining it
    for k in qualifying:
        for svc in k.group:
            for aid in by_service[svc]:
                explains.setdefault(aid, []).append(k.cand.id)
    for stmt in W_INDEX:
        await log.write(stmt)
    await log.write(
        W_CANDIDATES,
        {"candidates": [{"id": k.cand.id, "service_id": k.cand.service_id} for k in qualifying]},
    )
    await log.write(
        W_ALERTS,
        {
            "pages": [
                {"id": aid, "service_id": svc, "candidates": sorted(explains.get(aid, []))}
                for aid, svc, _, _ in pages
            ]
        },
    )
    wcc = await log.write(WCC)
    wcc_q = log.last_index
    comps: dict[object, _Component] = {}
    service_of = {aid: svc for aid, svc, _, _ in pages}
    for kind, nid, cid in wcc:
        comp = comps.setdefault(cid, _Component())
        (comp.pages if kind == "Alert" else comp.candidates).append(nid)
    seen = {nid for kind, nid, _ in wcc if kind == "Alert"}
    for aid, _, _, _ in pages:  # defensive: a page WCC did not yield stands alone
        if aid not in seen:
            comps[("page", aid)] = _Component([aid])
    # Pages nothing explains are singleton components: regroup them by service.
    merged: dict[object, _Component] = {}
    for cid, comp in comps.items():
        key = cid if comp.candidates else ("unexplained", service_of[comp.pages[0]])
        m = merged.setdefault(key, _Component())
        m.pages += comp.pages
        m.candidates += comp.candidates

    # d. per-component greedy set cover; joint units per KTD13
    units: list[_Unit] = []
    for k in qualifying:
        if k.partner is None or k.solo is None:  # no partner, or never shown harmless alone
            reach = frozenset(a for s in k.group for a in by_service[s])
            units.append(_Unit((k.cand.id,), reach))
    for f_id, c_id, flag_id, cfg_id, svc in pairs:
        f, c = checked[f_id], checked[c_id]
        if f.solo is not None and c.solo is not None and f.group == c.group:
            reach = frozenset(a for s in f.group for a in by_service[s])
            joint = {
                "flag_change": f_id,
                "config_change": c_id,
                "flag": flag_id,
                "config": cfg_id,
                "service_id": svc,
                "solo": {f_id: f.solo, c_id: c.solo},
            }
            units.append(_Unit((f_id, c_id), reach, joint))

    ordered = sorted(
        merged.values(),
        key=lambda m: (
            min(onset[service_of[a]] for a in m.pages),
            min(service_of[a] for a in m.pages),
        ),
    )
    incidents = []
    for n, comp in enumerate(ordered, 1):
        mine = set(comp.pages)
        local = [_Unit(u.ids, u.reach & mine, u.joint) for u in units if u.reach & mine]
        chosen, uncovered = _cover(comp, local)
        explaining = sorted(
            {i for u in chosen for i in u.ids}, key=lambda i: (checked[i].cand.event_time, i)
        )
        alternatives = [
            list(u.ids)
            for u in local
            if u not in chosen and any(u.reach == c.reach for c in chosen)
        ]
        services = sorted({service_of[a] for a in comp.pages}, key=lambda s: (onset[s], s))
        paths = []
        for eid in explaining:
            k = checked[eid]
            for svc in k.group:
                if svc not in services:
                    continue
                prow = await log.rows(
                    CANONICAL_PATH.format(label=k.cand.entity),
                    {"entity_id": eid, "service_id": svc},
                )
                path = prow[0][0] if prow else None
                paths.append(
                    {
                        "from": eid,
                        "to": svc,
                        "path": path,
                        "hops": len(path) - 1 if path else None,
                        "query": log.last_index,
                    }
                )
        joint = next((u.joint for u in chosen if u.joint), None)
        incidents.append(
            {
                "component": n,
                "symptomatic": services,
                "onsets": {s: onset[s] for s in services},
                "signatures": {s: sorted(sigs[s]) for s in services},
                "alerts": len(comp.pages),
                "first_page": min(onset[s] for s in services),
                "explaining": explaining,
                "jointly_caused": joint is not None,
                "joint": joint,
                "alternatives": alternatives,
                "unexplained": sorted({service_of[a] for a in uncovered}),
                "candidates": [
                    checked[i].as_dict()
                    for i in sorted(
                        {k.cand.id for k in checked.values() if k.cand.service_id in services},
                        key=lambda i: (checked[i].cand.event_time, i),
                    )
                ],
                "paths": paths,
            }
        )

    # e. chokepoints: seeded betweenness on the storm area's IMPACTS subgraph
    area = await log.rows(
        AREA.format(hops=AREA_HOPS), {"symptomatic": symptomatic, "cap": AREA_MAX}
    )
    area_ids = [r[0] for r in area]
    edges = await log.rows(AREA_EDGES, {"ids": area_ids})
    await log.write(W_AREA_NODES, {"ids": area_ids})
    await log.write(W_AREA_EDGES, {"edges": [{"source": a, "target": b} for a, b in edges]})
    params = {"samples": BETWEENNESS_SAMPLES, "seed": BETWEENNESS_SEED, "top_k": TOP_CHOKEPOINTS}
    ranked = await log.write(BETWEENNESS, params)
    chokepoints = [{"service_id": r[0], "score": r[1]} for r in ranked if r[1] and r[1] > 0]

    result = {
        "storm_id": storm_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "snapshot_t": snap_t,
        "window_minutes": P.DEFAULT_WINDOW_MINUTES,
        "reach_depth": STORM_REACH_DEPTH,
        "pages_in": len(pages),
        "symptomatic_services": len(symptomatic),
        "candidates_checked": len(checked),
        "outside_window": outside,
        "incidents": incidents,
        "collapsed_to": len(incidents),
        "chokepoints": chokepoints,
        "betweenness": {"seed": BETWEENNESS_SEED, "samples": BETWEENNESS_SAMPLES},
        "area": {
            "nodes": [
                {"id": i, "name": nm, "x": x, "y": y, "symptomatic": i in onset}
                for i, nm, x, y in area
            ],
            "edges": [{"source": a, "target": b} for a, b in edges],
        },
        "graphs": {"snapshot": graph, "storm": scratch, "storm_queries": log.scratch_queries},
        "wcc_query": wcc_q,
    }
    touched = [storm_id, *symptomatic, *(i for inc in incidents for i in inc["explaining"])]
    touched += [c["service_id"] for c in chokepoints]
    return log.result("collapse_storm", result, touched)
