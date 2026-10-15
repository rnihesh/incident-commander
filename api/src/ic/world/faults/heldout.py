"""The held-out fault classes (U15, KTD15): expiry and undeclared coupling."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass

import networkx as nx

from ic.graph.client import query
from ic.graph.schema import (
    COMPATIBLE_SIGNATURES,
    OBSERVED_CALL_THRESHOLD,
    ChangeKind,
    ExpiryKind,
)
from ic.store import Store
from ic.world.causality import necessity, qualifies
from ic.world.faults.common import (
    _NEG,
    _POS,
    TRAINING_CLASSES,
    WINDOW_S,
    FaultAnswer,
    InjectedFault,
    InjectionError,
    _alert_service,
    _Ctx,
    _default_alert_time,
    _Ent,
    _impact_services,
    _make_change,
    _max_id,
    _rows,
    _services,
    _window_entities,
    _write_change,
    _write_incident,
)
from ic.world.model import Change, Expiry, Symptom

# ================================================================== held-out classes (U15)
#
# Written after the freeze (KTD15); the triage prompts and tool descriptions never name them.
# Same discipline as the training classes: the root is built by the same code path as the
# decoys of its type, timing-matched (same lead distribution), ids follow valid_from /
# valid_to order, rows are written in seeded shuffled order, and the oracle verifies that the
# root qualifies, is necessary, and that every decoy and background entity fails a clause.
#
#   expiry               a cert/token/quota Expiry on the first service to fail, whose valid_to
#                        falls shortly before the earliest onset; symptom signatures are ones
#                        that expiry kind can produce. Decoys mix changes and expiries.
#   undeclared_coupling  a change on a callee whose failure reaches its symptomatic callers only
#                        over an OBSERVED_CALL edge (count above threshold, no DEPENDS_ON);
#                        the edge is created when the world has no usable one. Every other
#                        symptomatic service is reachable from the callee only through that
#                        edge, so the declared topology alone cannot explain the incident.

HELDOUT_CLASSES: dict[str, str] = {"expiry": "expiry", "undeclared_coupling": "coupling"}
FAULT_CLASSES: tuple[str, ...] = (*TRAINING_CLASSES, *HELDOUT_CLASSES)
_DAY = 86400
_ALL_KINDS: list[str] = [*ChangeKind, *ExpiryKind]
_EXPIRY_KINDS = frozenset(str(k) for k in ExpiryKind)


@dataclass
class _HPlan:
    target: str
    symptoms: list[Symptom]
    root: Change | Expiry
    decoys: list[Change | Expiry]
    root_kind: str
    coupling: tuple[str, str] | None = None  # (caller, callee) of the undeclared edge
    new_edge: dict | None = None  # OBSERVED_CALL to create, when the world had none usable


def _make_expiry(
    rng: random.Random,
    services: dict[str, tuple[str, str]],
    service_id: str,
    kind: str,
    valid_to: int,
    used: set[tuple[str, str]],
) -> Expiry | None:
    """The single code path for the root expiry and every decoy expiry (generator shape:
    issued 30-365 days before it expires, recorded when issued)."""
    if (service_id, f"expiry:{kind}") in used:
        return None
    used.add((service_id, f"expiry:{kind}"))
    vf = valid_to - rng.randint(30, 365) * _DAY
    name = services[service_id][0]
    return Expiry("", kind, service_id, f"{kind} for {name} expires", vf, valid_to, vf)


async def _make_entity(
    ctx: _Ctx,
    rng: random.Random,
    services: dict[str, tuple[str, str]],
    service_id: str,
    kind: str,
    t: int,
    used: set[tuple[str, str]],
) -> Change | Expiry | None:
    if str(kind) in _EXPIRY_KINDS:
        return _make_expiry(rng, services, service_id, str(kind), t, used)
    return await _make_change(ctx, rng, service_id, kind, t, used)


async def _declared_services(graph: str, t: int) -> nx.DiGraph:
    """callee -> caller edges for DEPENDS_ON valid at ``t``: the declared topology only."""
    g = nx.DiGraph()
    p = {"t": t, "ninf": _NEG, "pinf": _POS}
    for a, b in await _rows(
        graph,
        "MATCH (a:Service)-[e:DEPENDS_ON]->(b:Service) "
        "WHERE coalesce(e.valid_from, $ninf) <= $t AND $t < coalesce(e.valid_to, $pinf) "
        "RETURN a.id, b.id",
        p,
    ):
        g.add_edge(b, a)
    return g


def _reaches(g: nx.DiGraph, src: str) -> set[str]:
    return nx.descendants(g, src) if src in g else set()


async def _coupling_pairs(graph: str, t: int, declared: nx.DiGraph) -> list[tuple[str, str]]:
    """(caller, callee) OBSERVED_CALL edges above threshold with no declared path from the
    callee's failure to the caller (so the coupling is the only way it travels)."""
    rows = await _rows(
        graph,
        "MATCH (a:Service)-[o:OBSERVED_CALL]->(b:Service) "
        "WHERE o.count >= $thr AND o.first_seen <= $t AND a <> b RETURN a.id, b.id",
        {"t": t, "thr": OBSERVED_CALL_THRESHOLD},
    )
    return sorted(
        (a, b) for a, b in rows if not declared.has_edge(b, a) and a not in _reaches(declared, b)
    )


async def _finish_plan(
    ctx: _Ctx,
    rng: random.Random,
    g: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    target: str,
    root_kind: str,
    chosen: list[str],
    dist: dict[str, int],
    alert: int,
    background: list[_Ent],
    *,
    kinds: list[str],
    same_kind_p: float,
) -> tuple[list[Symptom], Change | Expiry, list[Change | Expiry], nx.DiGraph] | None:
    """Symptoms, root and decoys for a held-out plan, verified with the oracle on ``g``.
    Returns (symptoms, root, decoys, oracle graph including the new entities) or None."""
    sigs = sorted(str(s) for s in COMPATIBLE_SIGNATURES[root_kind])
    first_sig = rng.choice(sigs)
    step = rng.randint(60, 150)
    onset = {target: 0} | {s: dist[s] * step + rng.randint(0, step // 3) for s in chosen}
    spread = max(onset.values())
    t0 = alert - spread - rng.randint(30, 90)  # earliest onset
    symptoms = [
        Symptom("", "", s, t0 + off, first_sig if rng.random() < 0.7 else rng.choice(sigs))
        for s, off in onset.items()
    ]
    sym_ids = set(onset)
    sym_sigs = {s.signature for s in symptoms}

    used: set[tuple[str, str]] = set()
    pre = lambda: t0 - rng.randint(240, 1800)  # noqa: E731  same lead for root and decoys
    root = await _make_entity(ctx, rng, services, target, root_kind, pre(), used)
    if root is None:
        return None

    incompatible = [k for k in kinds if not sym_sigs <= {str(x) for x in COMPATIBLE_SIGNATURES[k]}]
    healthy = sorted(s for s in g.predecessors(target) if s in services and s not in sym_ids)
    name = services[target][0]
    siblings = sorted(
        s for s, (n, _) in services.items() if n == name and s != target and s not in sym_ids
    )

    def kind_like_root() -> str:
        return root_kind if rng.random() < same_kind_p else rng.choice(kinds)

    callers = sorted(sym_ids - {target})
    wishes: list[tuple[str, str, int]] = []
    if incompatible:  # fails signature
        wishes.append((target, rng.choice(incompatible), pre()))
    wishes.append((rng.choice(callers), kind_like_root(), pre()))  # fails origin
    if healthy:  # fails origin: the service itself shows no symptom
        wishes.append((rng.choice(healthy), kind_like_root(), pre()))
    late = rng.random() < 0.35
    wanted = rng.randint(3, 6)
    extra = ["origin_caller", "origin_healthy", "origin_sibling"] + (
        ["signature"] if incompatible else []
    )
    guard = 0
    while len(wishes) + late < wanted and guard < 50:
        guard += 1
        typ = rng.choice(extra)
        if typ == "origin_caller":
            wishes.append((rng.choice(callers), kind_like_root(), pre()))
        elif typ == "origin_healthy" and healthy:
            wishes.append((rng.choice(healthy), kind_like_root(), pre()))
        elif typ == "origin_sibling" and siblings:
            wishes.append((rng.choice(siblings), kind_like_root(), pre()))
        elif typ == "signature":
            wishes.append((target, rng.choice(incompatible), pre()))
    if late:  # fails after_onset (still before the alert, so it is in the snapshot)
        wishes.append((target, kind_like_root(), t0 + rng.randint(1, alert - t0 - 1)))

    decoys: list[Change | Expiry] = []
    for svc, kind, t in wishes:
        d = await _make_entity(ctx, rng, services, svc, kind, t, used)
        if d is not None:
            decoys.append(d)

    og = g.copy()
    for i, c in enumerate([root, *decoys]):
        c.id = f"new-{i}"
        og.add_edge(c.id, c.service_id)
    for b in background:
        og.add_edge(b.id, b.service_id)
    if not qualifies(root, og, symptoms)[0]:
        return None
    if any(qualifies(b, og, symptoms)[0] for b in background):
        return None
    decoys = [d for d in decoys if not qualifies(d, og, symptoms)[0]]
    if len(decoys) < 3:
        return None
    if not necessity(root, [root, *decoys, *background], og, symptoms)[0]:
        return None
    return symptoms, root, decoys, og


async def _plan_expiry(
    ctx: _Ctx,
    rng: random.Random,
    g: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    target: str,
    alert: int,
    background: list[_Ent],
) -> _HPlan | None:
    root_kind = rng.choice(sorted(_EXPIRY_KINDS))
    dist = nx.single_source_shortest_path_length(g, target, cutoff=3)
    pool = sorted(s for s, d in dist.items() if d >= 1 and s in services)
    if len(pool) < 2:
        return None
    chosen = rng.sample(pool, rng.randint(2, min(5, len(pool))))
    out = await _finish_plan(
        ctx,
        rng,
        g,
        services,
        target,
        root_kind,
        chosen,
        dist,
        alert,
        background,
        kinds=_ALL_KINDS,
        same_kind_p=0.6,
    )
    if out is None:
        return None
    symptoms, root, decoys, _ = out
    return _HPlan(target, symptoms, root, decoys, root_kind)


async def _plan_coupling(
    ctx: _Ctx,
    rng: random.Random,
    g: nx.DiGraph,
    declared: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    pair: tuple[str, str],
    new_edge: dict | None,
    alert: int,
    background: list[_Ent],
) -> _HPlan | None:
    caller, target = pair
    full = g.copy()
    full.add_edge(target, caller)  # present already unless the edge is being created
    dist = nx.single_source_shortest_path_length(full, target, cutoff=3)
    visible = _reaches(declared, target)
    hidden = sorted(s for s, d in dist.items() if d >= 1 and s in services and s not in visible)
    others = [s for s in hidden if s != caller]
    if caller not in hidden or not others:
        return None
    chosen = [caller, *rng.sample(others, rng.randint(1, min(4, len(others))))]
    root_kind = rng.choice(sorted(str(k) for k in ChangeKind))
    out = await _finish_plan(
        ctx,
        rng,
        full,
        services,
        target,
        root_kind,
        chosen,
        dist,
        alert,
        background,
        kinds=[str(k) for k in ChangeKind],
        same_kind_p=0.9,
    )
    if out is None:
        return None
    symptoms, root, decoys, og = out
    # Without the observed-only edges the root must fail coverage: the coupling is load-bearing.
    observed_only = [(a, b) for a, b in og.edges if a in services and not declared.has_edge(a, b)]
    dg = og.copy()
    dg.remove_edges_from(observed_only)
    if qualifies(root, dg, symptoms)[1] != "coverage":
        return None
    return _HPlan(target, symptoms, root, decoys, root_kind, (caller, target), new_edge)


def _new_coupling_pairs(
    rng: random.Random,
    g: nx.DiGraph,
    declared: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    alert: int,
) -> list[tuple[tuple[str, str], dict | None]]:
    """Fresh undeclared couplings (same region, no declared path from callee to caller, the
    caller has callers of its own), each with the OBSERVED_CALL row to create."""
    ids = sorted(s for s, (_, dom) in services.items() if dom != "edge")
    out: list[tuple[tuple[str, str], dict | None]] = []
    for _ in range(4000):
        if len(out) >= 40:
            break
        a, b = rng.choice(ids), rng.choice(ids)
        if a == b or a.split("@")[1] != b.split("@")[1] or g.has_edge(b, a):
            continue
        if a in _reaches(declared, b) or a not in g or not any(True for _ in g.successors(a)):
            continue
        edge = {
            "first_seen": alert - rng.randint(7, 60) * _DAY,
            "last_seen": alert,
            "count": rng.randint(80, 3000),
        }
        out.append(((a, b), edge))
    return out


async def _write_expiry(graph: str, e: Expiry) -> None:
    await query(
        graph,
        "MATCH (s:Service {id: $sid}) "
        "CREATE (e:Expiry {id: $id, kind: $kind, service_id: $sid, summary: $summary, "
        "valid_from: $vf, valid_to: $vt, recorded_at: $rec}) CREATE (e)-[:EXPIRES_ON]->(s)",
        {
            "sid": e.service_id,
            "id": e.id,
            "kind": str(e.kind),
            "summary": e.summary,
            "vf": e.valid_from,
            "vt": e.valid_to,
            "rec": e.recorded_at,
        },
    )


async def _write_heldout(
    graph: str, rng: random.Random, plan: _HPlan, alert: int, ids: tuple[int, int, int, int]
) -> tuple[str, str]:
    chg_n, exp_n, inc_n, sym_n = ids
    ents = [plan.root, *plan.decoys]
    changes = [e for e in ents if isinstance(e, Change)]
    expiries = [e for e in ents if isinstance(e, Expiry)]
    for n, c in enumerate(sorted(changes, key=lambda c: (c.valid_from, rng.random())), 1):
        c.id = f"chg-{chg_n + n:05d}"
    for n, e in enumerate(sorted(expiries, key=lambda e: (e.valid_to, rng.random())), 1):
        e.id = f"exp-{exp_n + n:04d}"
    order = ents[:]
    rng.shuffle(order)  # write order must not reveal the root
    for e in order:
        if isinstance(e, Expiry):
            await _write_expiry(graph, e)
        else:
            await _write_change(graph, e)
    if plan.new_edge is not None and plan.coupling is not None:
        caller, callee = plan.coupling
        await query(
            graph,
            "MATCH (a:Service {id: $a}), (b:Service {id: $b}) "
            "CREATE (a)-[:OBSERVED_CALL {first_seen: $fs, last_seen: $ls, count: $n}]->(b)",
            {
                "a": caller,
                "b": callee,
                "fs": plan.new_edge["first_seen"],
                "ls": plan.new_edge["last_seen"],
                "n": plan.new_edge["count"],
            },
        )
    inc_id = await _write_incident(graph, rng, plan, alert, plan.root_kind, inc_n, sym_n)
    return inc_id, plan.root.id


async def _inject_heldout(
    world_graph: str,
    *,
    session_id: str,
    fault_class: str,
    rng_seed: int,
    target_service: str | None,
    alert_time: int | None,
    store: Store,
) -> InjectedFault:
    rng = random.Random(f"{fault_class}:{rng_seed}")
    alert = alert_time if alert_time is not None else await _default_alert_time(world_graph, rng)
    services = await _services(world_graph)
    if target_service is not None and target_service not in services:
        raise InjectionError(f"unknown service {target_service!r}")
    g = await _impact_services(world_graph, alert)
    ctx = _Ctx(world_graph, alert)
    background = await _window_entities(world_graph, alert - WINDOW_S - 3600, alert)

    plan: _HPlan | None = None
    if fault_class == "expiry":
        if target_service is not None:
            targets = [target_service]
        else:
            targets = sorted(
                s
                for s, (_, domain) in services.items()
                if domain != "edge" and s in g and sum(1 for _ in g.successors(s)) >= 2
            )
            rng.shuffle(targets)
        for target in targets[:80]:
            plan = await _plan_expiry(ctx, rng, g, services, target, alert, background)
            if plan is not None:
                break
    else:
        declared = await _declared_services(world_graph, alert)
        pairs: list[tuple[tuple[str, str], dict | None]] = [
            (p, None) for p in await _coupling_pairs(world_graph, alert, declared)
        ]
        rng.shuffle(pairs)
        pairs += _new_coupling_pairs(rng, g, declared, services, alert)
        if target_service is not None:
            pairs = [(p, e) for p, e in pairs if p[1] == target_service]
        for pair, edge in pairs[:120]:
            plan = await _plan_coupling(
                ctx, rng, g, declared, services, pair, edge, alert, background
            )
            if plan is not None:
                break
    if plan is None:
        raise InjectionError(f"no valid {fault_class} injection found near t={alert}")

    incident_id, root_id = await _write_heldout(
        world_graph,
        rng,
        plan,
        alert,
        (
            await _max_id(world_graph, "Change", "chg-"),
            await _max_id(world_graph, "Expiry", "exp-"),
            await _max_id(world_graph, "Incident", "inc-"),
            await _max_id(world_graph, "Symptom", "sym-"),
        ),
    )
    answer = FaultAnswer(
        fault_id=f"flt-{secrets.token_hex(8)}",
        session_id=session_id,
        incident_id=incident_id,
        fault_class=fault_class,
        root_id=root_id,
        nonce=secrets.token_hex(16),
    )
    store.put_answer(
        fault_id=answer.fault_id,
        session_id=session_id,
        incident_id=incident_id,
        fault_class=fault_class,
        root_id=root_id,
        nonce=answer.nonce,
        commitment=answer.commitment,
    )
    return InjectedFault(incident_id, _alert_service(plan), alert, answer)
