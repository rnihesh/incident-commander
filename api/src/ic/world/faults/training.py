"""The training fault classes (bad deploy, config change, flag flip) and ``inject_fault``,
which also dispatches the held-out classes.
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass

import networkx as nx

from ic.graph.schema import (
    COMPATIBLE_SIGNATURES,
    ChangeKind,
)
from ic.store import Store
from ic.world.causality import necessity, qualifies
from ic.world.faults.common import (
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
    _services,
    _window_entities,
    _write_change,
    _write_incident,
)
from ic.world.faults.heldout import HELDOUT_CLASSES, _inject_heldout
from ic.world.model import Change, Symptom

# ------------------------------------------------------------------ planning


@dataclass
class _Plan:
    target: str
    symptoms: list[Symptom]
    root: Change
    decoys: list[Change]


async def _plan(
    ctx: _Ctx,
    rng: random.Random,
    g: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    target: str,
    root_kind: ChangeKind,
    alert: int,
    background: list[_Ent],
) -> _Plan | None:
    dist = nx.single_source_shortest_path_length(g, target, cutoff=3)
    pool = sorted(s for s, d in dist.items() if d >= 1 and s in services)
    if len(pool) < 2:
        return None
    chosen = rng.sample(pool, rng.randint(2, min(5, len(pool))))

    sigs = sorted(str(s) for s in COMPATIBLE_SIGNATURES[root_kind])
    first_sig = rng.choice(sigs)
    step = rng.randint(60, 150)
    onset = {target: 0} | {s: dist[s] * step + rng.randint(0, step // 3) for s in chosen}
    spread = max(onset.values())
    t0 = alert - spread - rng.randint(30, 90)  # earliest onset
    symptoms = [
        Symptom(
            "",
            "",
            s,
            t0 + off,
            first_sig if rng.random() < 0.7 else rng.choice(sigs),
        )
        for s, off in onset.items()
    ]
    sym_ids = set(onset)
    sym_sigs = {s.signature for s in symptoms}

    used: set[tuple[str, str]] = set()
    pre = lambda: t0 - rng.randint(240, 1800)  # noqa: E731  same lead distribution as the root
    root = await _make_change(ctx, rng, target, root_kind, pre(), used)
    if root is None:
        return None

    kinds = list(ChangeKind)
    incompatible = [k for k in kinds if not sym_sigs <= {str(x) for x in COMPATIBLE_SIGNATURES[k]}]
    healthy = sorted(s for s in g.predecessors(target) if s in services and s not in sym_ids)
    name = services[target][0]
    siblings = sorted(
        s for s, (n, _) in services.items() if n == name and s != target and s not in sym_ids
    )

    def kind_like_root() -> ChangeKind:
        return root_kind if rng.random() < 0.9 else rng.choice(kinds)

    # (decoy type, service, kind, time) wishes; each is designed to fail >= 1 KTD23 clause
    wishes: list[tuple[str, str, ChangeKind, int]] = []
    if incompatible:  # fails signature
        wishes.append(("signature", target, rng.choice(incompatible), pre()))
    wishes.append(
        ("origin_caller", rng.choice(sorted(sym_ids - {target})), kind_like_root(), pre())
    )
    if healthy:  # fails origin: the service itself shows no symptom
        wishes.append(("origin_healthy", rng.choice(healthy), kind_like_root(), pre()))
    late = rng.random() < 0.35
    wanted = rng.randint(3, 6)
    extra = ["origin_caller", "origin_healthy", "origin_sibling"] + (
        ["signature"] if incompatible else []
    )
    while len(wishes) + late < wanted:
        typ = rng.choice(extra)
        if typ == "origin_caller":
            svc, kind = rng.choice(sorted(sym_ids - {target})), kind_like_root()
        elif typ == "origin_healthy" and healthy:
            svc, kind = rng.choice(healthy), kind_like_root()
        elif typ == "origin_sibling" and siblings:
            svc, kind = rng.choice(siblings), kind_like_root()
        elif typ == "signature":
            svc, kind = target, rng.choice(incompatible)
        else:
            continue
        wishes.append((typ, svc, kind, pre()))
    if late:  # fails after_onset (still before the alert, so it is in the snapshot)
        wishes.append(
            ("after_onset", target, kind_like_root(), t0 + rng.randint(1, alert - t0 - 1))
        )

    decoys: list[Change] = []
    for _, svc, kind, vf in wishes:
        d = await _make_change(ctx, rng, svc, kind, vf, used)
        if d is not None:
            decoys.append(d)

    # verify with the oracle: root qualifies, every decoy and background entity fails
    og = g.copy()
    cand = {f"new-{i}": c for i, c in enumerate([root, *decoys])}
    for key, c in cand.items():
        c.id = key
        og.add_edge(key, c.service_id)
    for b in background:
        og.add_edge(b.id, b.service_id)
    if not qualifies(root, og, symptoms)[0]:
        return None
    if any(qualifies(b, og, symptoms)[0] for b in background):
        return None
    decoys = [d for d in decoys if not qualifies(d, og, symptoms)[0]]
    if len(decoys) < 3:
        return None
    ents = [root, *decoys, *background]
    if not necessity(root, ents, og, symptoms)[0]:
        return None
    return _Plan(target, symptoms, root, decoys)


# ------------------------------------------------------------------ writing


async def _write(
    graph: str,
    rng: random.Random,
    plan: _Plan,
    alert: int,
    root_kind: ChangeKind,
    ids: tuple[int, int, int],
) -> tuple[str, str]:
    """Create the rows. Returns (incident_id, root change id)."""
    chg_n, inc_n, sym_n = ids
    changes = [plan.root, *plan.decoys]
    for n, c in enumerate(sorted(changes, key=lambda c: (c.valid_from, rng.random())), 1):
        c.id = f"chg-{chg_n + n:05d}"
    order = changes[:]
    rng.shuffle(order)  # write order must not reveal the root
    for c in order:
        await _write_change(graph, c)
    inc_id = await _write_incident(graph, rng, plan, alert, root_kind, inc_n, sym_n)
    return inc_id, plan.root.id


async def inject_fault(
    world_graph: str,
    *,
    session_id: str,
    fault_class: str,
    rng_seed: int,
    target_service: str | None = None,
    alert_time: int | None = None,
    store: Store,
) -> InjectedFault:
    if fault_class in HELDOUT_CLASSES:
        return await _inject_heldout(
            world_graph,
            session_id=session_id,
            fault_class=fault_class,
            rng_seed=rng_seed,
            target_service=target_service,
            alert_time=alert_time,
            store=store,
        )
    if fault_class not in TRAINING_CLASSES:
        raise ValueError(f"unsupported fault class: {fault_class!r}")
    root_kind = TRAINING_CLASSES[fault_class]
    rng = random.Random(f"{fault_class}:{rng_seed}")
    alert = alert_time if alert_time is not None else await _default_alert_time(world_graph, rng)

    services = await _services(world_graph)
    g = await _impact_services(world_graph, alert)
    ctx = _Ctx(world_graph, alert)
    if target_service is not None:
        if target_service not in services:
            raise InjectionError(f"unknown service {target_service!r}")
        targets = [target_service]
    else:
        targets = sorted(
            s
            for s, (_, domain) in services.items()
            if domain != "edge" and s in g and sum(1 for _ in g.successors(s)) >= 2
        )
        rng.shuffle(targets)
    background = await _window_entities(world_graph, alert - WINDOW_S - 3600, alert)

    plan: _Plan | None = None
    for target in targets[:80]:
        plan = await _plan(ctx, rng, g, services, target, root_kind, alert, background)
        if plan is not None:
            break
    if plan is None:
        raise InjectionError(f"no valid {fault_class} injection found near t={alert}")

    incident_id, root_id = await _write(
        world_graph,
        rng,
        plan,
        alert,
        root_kind,
        (
            await _max_id(world_graph, "Change", "chg-"),
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
