"""Shared pieces of fault injection: the answer types, the session-world context, the
change path every root and decoy goes through, topology reads and the row writers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import networkx as nx

from ic.graph.client import query
from ic.graph.schema import (
    OBSERVED_CALL_THRESHOLD,
    OPEN_END,
    WORLD_NOW,
    ChangeKind,
)
from ic.world import text
from ic.world.faults.commit_reveal import commitment
from ic.world.generator import CONFIG_KEYS, DEPLOY_NOTES
from ic.world.model import Change

TRAINING_CLASSES: dict[str, ChangeKind] = {
    "bad_deploy": ChangeKind.DEPLOY,
    "config_change": ChangeKind.CONFIG,
    "flag_flip": ChangeKind.FLAG,
}
WINDOW_S = 120 * 60
_NEG, _POS = -(2**62), OPEN_END * 10


class InjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FaultAnswer:
    """Ground truth. Persisted only through ``Store.put_answer``; never written to a graph."""

    fault_id: str
    session_id: str
    incident_id: str
    fault_class: str
    root_id: str
    nonce: str

    @property
    def commitment(self) -> str:
        """The public commit-reveal hash (KTD11); safe to publish, reveals nothing."""
        fields = (self.fault_id, self.incident_id, self.fault_class, self.root_id)
        return commitment(*fields, self.nonce)


@dataclass
class InjectedFault:
    """What the caller (API layer) gets back. ``answer`` must stay server-side."""

    incident_id: str
    alert_service_id: str
    alert_time: int
    answer: FaultAnswer = field(repr=False)

    @property
    def commitment(self) -> str:
        return self.answer.commitment


@dataclass(frozen=True)
class _Ent:  # an existing window entity, enough for the oracle
    id: str
    service_id: str
    event_time: int
    sig_kind: str


@dataclass
class _Info:
    members: list[str]
    configs: list[tuple[str, str, str]]  # id, key, value
    flags: list[tuple[str, str, bool]]  # id, name, enabled
    chains: dict[str, tuple[str, str, int, int]]  # target_key -> (id, value, valid_from, valid_to)


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


class _Ctx:
    """Lazy, cached reads of the topology around the services an injection touches."""

    def __init__(self, graph: str, t: int):
        self.graph, self.t = graph, t
        self._info: dict[str, _Info] = {}

    async def info(self, sid: str) -> _Info:
        if sid in self._info:
            return self._info[sid]
        g, p = self.graph, {"sid": sid, "t": self.t, "ninf": _NEG, "pinf": _POS}
        members = [
            r[0]
            for r in await _rows(
                g,
                "MATCH (p:Person)-[:MEMBER_OF]->(:Team)-[o:OWNS]->(:Service {id: $sid}) "
                "WHERE coalesce(o.valid_from, $ninf) <= $t AND $t < coalesce(o.valid_to, $pinf) "
                "RETURN DISTINCT p.id ORDER BY p.id",
                p,
            )
        ]
        if not members:
            members = [
                r[0]
                for r in await _rows(
                    g,
                    "MATCH (p:Person)-[:MEMBER_OF]->(:Team)-[:OWNS]->(:Service {id: $sid}) "
                    "RETURN DISTINCT p.id ORDER BY p.id",
                    p,
                )
            ]
        configs = [
            tuple(r)
            for r in await _rows(
                g,
                "MATCH (:Service {id: $sid})-[:HAS_CONFIG]->(c:Config) "
                "RETURN c.id, c.key, c.value ORDER BY c.id",
                p,
            )
        ]
        flags = [
            tuple(r)
            for r in await _rows(
                g,
                "MATCH (:Service {id: $sid})-[:HAS_FLAG]->(f:Flag) "
                "RETURN f.id, f.name, f.enabled ORDER BY f.id",
                p,
            )
        ]
        chains: dict[str, tuple[str, str, int, int]] = {}
        for cid, value, vf, vt, tid in await _rows(
            g,
            "MATCH (c:Change)-[:CHANGES]->(:Service {id: $sid}) "
            "OPTIONAL MATCH (c)-[:TOUCHES]->(x) "
            "RETURN c.id, c.value, c.valid_from, c.valid_to, x.id ORDER BY c.valid_from, c.id",
            p,
        ):
            chains[tid or "deploy"] = (cid, value, vf, vt)
        info = _Info(members, configs, flags, chains)  # type: ignore[arg-type]
        self._info[sid] = info
        return info


# ------------------------------------------------------------------ the shared change path


def _delay(rng: random.Random) -> int:
    """recorded_at - valid_from. One distribution for root and decoys alike."""
    return rng.randint(5, 240)


async def _make_change(
    ctx: _Ctx,
    rng: random.Random,
    service_id: str,
    kind: str,
    valid_from: int,
    used: set[tuple[str, str]],
) -> Change | None:
    """Build one Change by the single code path used for the root and every decoy.

    Returns None when the service has no unused version chain of ``kind``. The returned
    change links to the latest existing change on its chain (previous_id / prev_value).
    """
    info = await ctx.info(service_id)
    touches: str | None = None
    if kind == ChangeKind.DEPLOY:
        target = "deploy"
        prev = info.chains.get("deploy")
        before = prev[1] if prev else "v1.0.0"
        major, minor, patch = (int(x) for x in before.lstrip("v").split("."))
        value = f"v{major}.{minor}.{patch + rng.randint(1, 2)}"
        summary = f"Deploy {value}: {rng.choice(DEPLOY_NOTES)}"
    elif kind == ChangeKind.CONFIG:
        pool = [c for c in info.configs if (service_id, c[0]) not in used]
        if not pool:
            return None
        cid, key, cur = rng.choice(pool)
        target = touches = cid
        prev = info.chains.get(cid)
        before = prev[1] if prev else cur
        value = before
        while value == before:
            value = str(rng.randint(*CONFIG_KEYS[key]))
        summary = f"Set {key} to {value}"
    else:
        pool = [f for f in info.flags if (service_id, f[0]) not in used]
        if not pool:
            return None
        fid, name, enabled = rng.choice(pool)
        target = touches = fid
        prev = info.chains.get(fid)
        before = prev[1] if prev else ("on" if enabled else "off")
        value = "off" if before == "on" else "on"
        summary = f"Flip flag {name} {before}->{value}"
    if (service_id, target) in used:
        return None
    if prev and prev[2] >= valid_from:
        return None  # would need re-linking a later change; pick another chain/time
    used.add((service_id, target))
    return Change(
        "",
        kind,
        service_id,
        summary,
        valid_from,
        OPEN_END,
        valid_from + _delay(rng),
        rng.choice(info.members),
        target,
        touches,
        value,
        before,
        prev[0] if prev else None,
    )


# ------------------------------------------------------------------ topology reads


async def _impact_services(graph: str, t: int) -> nx.DiGraph:
    """callee -> caller edges between services at ``t`` (same rule as the snapshot builder)."""
    g = nx.DiGraph()
    p = {"t": t, "ninf": _NEG, "pinf": _POS, "thr": OBSERVED_CALL_THRESHOLD}
    for a, b in await _rows(
        graph,
        "MATCH (a:Service)-[e:DEPENDS_ON]->(b:Service) "
        "WHERE coalesce(e.valid_from, $ninf) <= $t AND $t < coalesce(e.valid_to, $pinf) "
        "RETURN a.id, b.id",
        p,
    ):
        g.add_edge(b, a)
    for a, b in await _rows(
        graph,
        "MATCH (a:Service)-[o:OBSERVED_CALL]->(b:Service) "
        "WHERE o.count >= $thr AND o.first_seen <= $t RETURN a.id, b.id",
        p,
    ):
        g.add_edge(b, a)
    return g


async def _services(graph: str) -> dict[str, tuple[str, str]]:
    return {
        i: (n, d)
        for i, n, d in await _rows(graph, "MATCH (s:Service) RETURN s.id, s.name, s.domain")
    }


async def _window_entities(graph: str, lo: int, hi: int) -> list[_Ent]:
    p = {"lo": lo, "hi": hi}
    out = [
        _Ent(*r)
        for r in await _rows(
            graph,
            "MATCH (c:Change)-[:CHANGES]->(s:Service) WHERE c.valid_from >= $lo AND "
            "c.valid_from <= $hi RETURN c.id, s.id, c.valid_from, c.kind",
            p,
        )
    ]
    out += [
        _Ent(*r)
        for r in await _rows(
            graph,
            "MATCH (e:Expiry)-[:EXPIRES_ON]->(s:Service) WHERE e.valid_to >= $lo AND "
            "e.valid_to <= $hi RETURN e.id, s.id, e.valid_to, e.kind",
            p,
        )
    ]
    return out


async def _max_id(graph: str, label: str, prefix: str) -> int:
    """Highest numeric suffix among ``label`` ids shaped ``<prefix><digits>`` (0 if none).

    One aggregate row instead of shipping every id to the client."""
    rows = await _rows(
        graph,
        f"MATCH (n:{label}) WHERE n.id STARTS WITH $p RETURN max(toInteger(substring(n.id, $k)))",
        {"p": prefix, "k": len(prefix)},
    )
    return int(rows[0][0] or 0)


async def _default_alert_time(graph: str, rng: random.Random) -> int:
    """WORLD_NOW plus a seeded offset; pushed past anything already injected so windows
    from successive injections into one world never overlap."""
    c = (await _rows(graph, "MATCH (c:Change) RETURN max(c.valid_from)"))[0][0]
    i = (await _rows(graph, "MATCH (i:Incident) RETURN max(i.opened_at)"))[0][0]
    base = max(WORLD_NOW, c or 0, i or 0)
    return base + rng.randint(180, 360) * 60


# ------------------------------------------------------------------ writing (shared)


async def _write_change(graph: str, c: Change) -> None:
    await query(
        graph,
        "MATCH (s:Service {id: $sid}), (p:Person {id: $author}) "
        "CREATE (c:Change {id: $id, kind: $kind, service_id: $sid, summary: $summary, "
        "valid_from: $vf, valid_to: $vt, recorded_at: $rec, value: $value, "
        "prev_value: $prev}) "
        "CREATE (c)-[:CHANGES]->(s) CREATE (p)-[:AUTHORED]->(c)",
        {
            "sid": c.service_id,
            "author": c.author_id,
            "id": c.id,
            "kind": str(c.kind),
            "summary": c.summary,
            "vf": c.valid_from,
            "vt": c.valid_to,
            "rec": c.recorded_at,
            "value": c.value,
            "prev": c.prev_value,
        },
    )
    if c.touches_id:
        label = "Config" if c.kind == ChangeKind.CONFIG else "Flag"
        await query(
            graph,
            f"MATCH (c:Change {{id: $id}}), (x:{label} {{id: $tid}}) CREATE (c)-[:TOUCHES]->(x)",
            {"id": c.id, "tid": c.touches_id},
        )
    if c.previous_id:
        await query(
            graph,
            "MATCH (c:Change {id: $id}), (q:Change {id: $prev}) "
            "CREATE (c)-[:PREVIOUS_VERSION]->(q) SET q.valid_to = $vf",
            {"id": c.id, "prev": c.previous_id, "vf": c.valid_from},
        )


async def _write_incident(
    graph: str, rng: random.Random, plan, alert: int, root_kind: str, inc_n: int, sym_n: int
) -> str:
    names = {r[0]: r[1] for r in await _rows(graph, "MATCH (s:Service) RETURN s.id, s.name")}
    inc_id = f"inc-{inc_n + 1:03d}"
    # The pager title describes what fired (the alerting service and its symptom), never
    # the root: naming the root would hand the answer to anyone reading the alert.
    alert_svc = _alert_service(plan)
    fired = max(plan.symptoms, key=lambda s: (s.onset, s.service_id))
    facts = text.PostmortemFacts(
        root=names[alert_svc],
        others=[],
        signatures=[fired.signature],
        cause_kind=str(root_kind),
        cause_service=names[plan.target],
        detail="",
        remediation="",
        minutes=0,
        coupling=None,
    )
    await query(
        graph,
        "CREATE (:Incident {id: $id, title: $title, opened_at: $at, status: 'open', "
        "alert_service_id: $svc})",
        {
            "id": inc_id,
            "title": text.incident_title(rng, facts),
            "at": alert,
            "svc": alert_svc,
        },
    )
    for n, s in enumerate(sorted(plan.symptoms, key=lambda s: (s.onset, s.service_id)), 1):
        await query(
            graph,
            "MATCH (i:Incident {id: $iid}), (v:Service {id: $sid}) "
            "CREATE (y:Symptom {id: $id, service_id: $sid, onset: $onset, signature: $sig}) "
            "CREATE (i)-[:HAS]->(y) CREATE (y)-[:ON]->(v)",
            {
                "iid": inc_id,
                "sid": s.service_id,
                "id": f"sym-{sym_n + n:04d}",
                "onset": s.onset,
                "sig": str(s.signature),
            },
        )
    return inc_id


def _alert_service(plan) -> str:
    """The alert fires on the last service to fail: the customer-facing edge of the blast,
    several hops from the root, so finding the cause requires walking the graph."""
    return max(plan.symptoms, key=lambda s: (s.onset, s.service_id)).service_id
