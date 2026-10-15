"""Alert storms (U17, KTD13): ``inject_storm`` and the ``joint`` fault class."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, field

import networkx as nx

from ic.graph.client import query
from ic.graph.schema import (
    COMPATIBLE_SIGNATURES,
    OPEN_END,
    WORLD_NOW,
    ChangeKind,
)
from ic.store import Store
from ic.world.causality import qualifies
from ic.world.faults.commit_reveal import commitment
from ic.world.faults.common import (
    TRAINING_CLASSES,
    WINDOW_S,
    InjectionError,
    _Ctx,
    _delay,
    _Ent,
    _impact_services,
    _Info,
    _make_change,
    _max_id,
    _rows,
    _services,
    _window_entities,
    _write_change,
)
from ic.world.faults.training import _plan
from ic.world.generator import CONFIG_KEYS
from ic.world.model import Change, Symptom

# ================================================================== alert storm (U17, KTD13)
#
# ``inject_storm`` plants 2-3 independent faults whose alerts land within minutes of each
# other, then fires hundreds of pages (repeats per symptomatic service plus flapping
# duplicates), each an ``(:Alert)-[:FOR]->(:Service)`` tied to one ``(:Storm)``. No Incident
# or Symptom nodes are written: the pages are the only evidence, and a page carries only its
# service, time and signature. Faults are independent in the graph sense: no symptomatic
# service of one fault is within ``STORM_REACH_DEPTH`` IMPACTS hops of another fault's, and
# no change of one fault sits on another fault's symptomatic services.
#
# The ``joint`` class (KTD13) is an AND fault on one service: a config change and a flip of
# the flag GATED_BY that config. Each alone ran without symptoms for longer than the delay
# between "both active" and the first symptom: the config change went live tens of minutes
# before the flip, and the flag already sat in its new state hours earlier (flipped and
# flipped back, outside the alert window) while the config still had its old value.
# Symptoms carry only signatures both kinds can produce, so each change also satisfies the
# KTD23 predicate on its own; only the solo intervals separate "jointly caused" from a
# single cause.

JOINT_CLASS = "joint"
STORM_FAULT_CLASSES: tuple[str, ...] = (*TRAINING_CLASSES, JOINT_CLASS)
STORM_REACH_DEPTH = 6  # collapse_storm groups symptoms by IMPACTS reach up to this depth
STORM_PAGES = (220, 380)  # target page count (the result is always within 200-400)
STORM_SPREAD_S = 15 * 60  # the faults' last-symptom times all fall within this of the start
STORM_TAIL_S = (20 * 60, 30 * 60)  # pages keep firing this long after the last fault
JOINT_SIGNATURES = sorted(
    str(s)
    for s in COMPATIBLE_SIGNATURES[ChangeKind.CONFIG] & COMPATIBLE_SIGNATURES[ChangeKind.FLAG]
)


@dataclass(frozen=True)
class StormAnswer:
    """Ground truth for one fault of a storm; only in the SQLite store (R26)."""

    fault_id: str
    session_id: str
    storm_id: str
    key: str  # answer-key incident id: "{storm_id}-f{n}"
    fault_class: str
    root_ids: tuple[str, ...]  # a joint fault has two: the flag flip and the config change
    services: tuple[str, ...]  # its symptomatic services
    nonce: str

    @property
    def root_id(self) -> str:
        return "+".join(self.root_ids)

    @property
    def commitment(self) -> str:
        return commitment(self.fault_id, self.key, self.fault_class, self.root_id, self.nonce)


@dataclass
class InjectedStorm:
    storm_id: str
    started_at: int  # first page
    ended_at: int  # last page time bound; the storm's snapshot time
    pages: int
    answers: list[StormAnswer] = field(repr=False)


@dataclass
class _StormFault:
    fault_class: str
    target: str
    symptoms: list[Symptom]
    roots: list[Change]
    changes: list[Change]  # every new change, roots included
    links: list[tuple[Change, Change]] = field(default_factory=list)  # (child, new parent)


def _pinned_change(
    info: _Info,
    rng: random.Random,
    service_id: str,
    kind: ChangeKind,
    target_id: str,
    valid_from: int,
    prev: tuple[str, str, int, int] | None,
) -> Change:
    """One flag flip or config change on a chosen chain (same text, author and delay
    distributions as ``_make_change``). ``prev`` is (id, value, valid_from, valid_to)."""
    if kind == ChangeKind.FLAG:
        _, name, enabled = next(f for f in info.flags if f[0] == target_id)
        before = prev[1] if prev else ("on" if enabled else "off")
        value = "off" if before == "on" else "on"
        summary = f"Flip flag {name} {before}->{value}"
    else:
        _, key, cur = next(c for c in info.configs if c[0] == target_id)
        before = prev[1] if prev else cur
        value = before
        while value == before:
            value = str(rng.randint(*CONFIG_KEYS[key]))
        summary = f"Set {key} to {value}"
    return Change(
        "",
        kind,
        service_id,
        summary,
        valid_from,
        OPEN_END,
        valid_from + _delay(rng),
        rng.choice(info.members),
        target_id,
        target_id,
        value,
        before,
        prev[0] if prev else None,
    )


def _reflip(info: _Info, c: Change, before: str) -> None:
    """Re-point a flag flip at the state it follows (its chain parent is new too)."""
    name = next(f[1] for f in info.flags if f[0] == c.target_key)
    c.prev_value, c.value = before, ("off" if before == "on" else "on")
    c.summary = f"Flip flag {name} {before}->{c.value}"


async def _gated_pairs(graph: str, service_id: str) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in await _rows(
            graph,
            "MATCH (s:Service {id: $sid})-[:HAS_FLAG]->(f:Flag)-[:GATED_BY]->(c:Config) "
            "MATCH (s)-[:HAS_CONFIG]->(c) RETURN f.id, c.id ORDER BY f.id, c.id",
            {"sid": service_id},
        )
    ]


async def _plan_joint(
    ctx: _Ctx,
    rng: random.Random,
    g: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    target: str,
    alert: int,
    background: list[_Ent],
) -> _StormFault | None:
    pairs = await _gated_pairs(ctx.graph, target)
    if not pairs:
        return None
    flag_id, cfg_id = rng.choice(pairs)
    dist = nx.single_source_shortest_path_length(g, target, cutoff=3)
    pool = sorted(s for s, d in dist.items() if d >= 1 and s in services)
    if len(pool) < 2:
        return None
    chosen = rng.sample(pool, rng.randint(2, min(5, len(pool))))
    step = rng.randint(60, 150)
    onset = {target: 0} | {s: dist[s] * step + rng.randint(0, step // 3) for s in chosen}
    t0 = alert - max(onset.values()) - rng.randint(30, 90)
    symptoms = [
        Symptom("", "", s, t0 + off, rng.choice(JOINT_SIGNATURES)) for s, off in onset.items()
    ]
    sym_ids = set(onset)

    # Timeline: flag alone (outside the window), config alone, then both -> symptoms.
    lead = rng.randint(240, 600)
    f_vf = t0 - lead
    c_vf = f_vf - rng.randint(35, 80) * 60
    x_vf = t0 - rng.randint(300, 420) * 60
    x_back = x_vf + rng.randint(40, 110) * 60
    info = await ctx.info(target)
    fprev, cprev = info.chains.get(flag_id), info.chains.get(cfg_id)
    if (fprev and fprev[2] >= x_vf) or (cprev and cprev[2] >= c_vf):
        return None
    x = _pinned_change(info, rng, target, ChangeKind.FLAG, flag_id, x_vf, fprev)
    back = _pinned_change(info, rng, target, ChangeKind.FLAG, flag_id, x_back, None)
    flip = _pinned_change(info, rng, target, ChangeKind.FLAG, flag_id, f_vf, None)
    cfg = _pinned_change(info, rng, target, ChangeKind.CONFIG, cfg_id, c_vf, cprev)
    _reflip(info, back, x.value)  # x -> back (restores the old state) -> flip (x's state again)
    _reflip(info, flip, back.value)
    x.valid_to, back.valid_to = x_back, f_vf

    used = {(target, flag_id), (target, cfg_id)}
    kinds = list(ChangeKind)
    healthy = sorted(s for s in g.predecessors(target) if s in services and s not in sym_ids)
    name = services[target][0]
    siblings = sorted(
        s for s, (n, _) in services.items() if n == name and s != target and s not in sym_ids
    )
    callers = sorted(sym_ids - {target})
    pre = lambda: t0 - rng.randint(240, 1800)  # noqa: E731  same lead as the roots
    wishes: list[tuple[str, ChangeKind, int]] = [(rng.choice(callers), rng.choice(kinds), pre())]
    if healthy:
        wishes.append((rng.choice(healthy), rng.choice(kinds), pre()))
    if siblings:
        wishes.append((rng.choice(siblings), rng.choice(kinds), pre()))
    wanted = rng.randint(3, 5)
    while len(wishes) < wanted:
        wishes.append((rng.choice(callers), rng.choice(kinds), pre()))
    if rng.random() < 0.5:  # fails after_onset (still before the storm ends)
        wishes.append((target, ChangeKind.DEPLOY, t0 + rng.randint(1, alert - t0 - 1)))
    decoys: list[Change] = []
    for svc, k, t in wishes:
        d = await _make_change(ctx, rng, svc, k, t, used)
        if d is not None:
            decoys.append(d)

    og = g.copy()
    for i, c in enumerate([cfg, flip, *decoys]):
        c.id = f"new-{i}"
        og.add_edge(c.id, c.service_id)
    for b in background:
        og.add_edge(b.id, b.service_id)
    if not (qualifies(cfg, og, symptoms)[0] and qualifies(flip, og, symptoms)[0]):
        return None
    if any(qualifies(b, og, symptoms)[0] for b in background):
        return None
    decoys = [d for d in decoys if not qualifies(d, og, symptoms)[0]]
    if len(decoys) < 2:
        return None
    return _StormFault(
        JOINT_CLASS,
        target,
        symptoms,
        [flip, cfg],
        [x, back, flip, cfg, *decoys],
        [(back, x), (flip, back)],
    )


async def _plan_storm_fault(
    ctx: _Ctx,
    rng: random.Random,
    g: nx.DiGraph,
    services: dict[str, tuple[str, str]],
    fault_class: str,
    target: str,
    alert: int,
    background: list[_Ent],
) -> _StormFault | None:
    if fault_class == JOINT_CLASS:
        return await _plan_joint(ctx, rng, g, services, target, alert, background)
    kind = TRAINING_CLASSES[fault_class]
    plan = await _plan(ctx, rng, g, services, target, kind, alert, background)
    if plan is None:
        return None
    return _StormFault(fault_class, target, plan.symptoms, [plan.root], [plan.root, *plan.decoys])


def _reach(g: nx.DiGraph, sources: set[str]) -> set[str]:
    out = set(sources)
    for s in sources:
        if s in g:
            out |= set(nx.single_source_shortest_path_length(g, s, cutoff=STORM_REACH_DEPTH))
    return out


def _independent(new: _StormFault, old: list[_StormFault], g: nx.DiGraph) -> bool:
    """Disjoint blast areas, disjoint version chains, no change on another fault's
    symptomatic services."""
    ns = {s.service_id for s in new.symptoms}
    nreach = _reach(g, ns)
    nchains = {(c.service_id, c.target_key) for c in new.changes}
    for o in old:
        os_ = {s.service_id for s in o.symptoms}
        if ns & _reach(g, os_) or os_ & nreach:
            return False
        if nchains & {(c.service_id, c.target_key) for c in o.changes}:
            return False
        if {c.service_id for c in new.changes} & os_ or {c.service_id for c in o.changes} & ns:
            return False
    return True


def _storm_pages(
    rng: random.Random, faults: list[_StormFault], end: int, total: int | None = None
) -> list[tuple[str, int, str]]:
    """(service, fired_at, signature) pages: the first at each service's onset, repeats until
    ``end``, and flapping duplicates seconds apart. 200-400 in total (``total`` aims)."""
    syms = sorted((s for f in faults for s in f.symptoms), key=lambda s: (s.onset, s.service_id))
    total = rng.randint(*STORM_PAGES) if total is None else min(max(total, 200), 400)
    weights = [(end - s.onset) * rng.uniform(0.6, 1.4) for s in syms]
    scale = total / sum(weights)
    pages: list[tuple[str, int, str]] = []
    for s, w in zip(syms, weights, strict=True):
        n = max(3, round(w * scale * 0.87))
        span = max(end - s.onset, 1)
        times = [s.onset] + sorted(s.onset + rng.randint(1, span) for _ in range(n - 1))
        pages += [(s.service_id, t, s.signature) for t in times]
    flaps = [p for p in pages if rng.random() < 0.15]
    pages += [(svc, min(t + rng.randint(5, 40), end), sig) for svc, t, sig in flaps]
    while len(pages) < 200:
        s = rng.choice(syms)
        pages.append((s.service_id, rng.randint(s.onset + 1, end), s.signature))
    if len(pages) > 400:  # keep every first page; drop random repeats
        firsts = [(s.service_id, s.onset, s.signature) for s in syms]
        rest = [p for p in pages if p not in firsts]
        rng.shuffle(rest)
        pages = firsts + rest[: 400 - len(firsts)]
    return pages


async def _storm_base(graph: str) -> int:
    vals = [
        (await _rows(graph, q))[0][0]
        for q in (
            "MATCH (c:Change) RETURN max(c.valid_from)",
            "MATCH (i:Incident) RETURN max(i.opened_at)",
            "MATCH (a:Alert) RETURN max(a.fired_at)",
        )
    ]
    return max(WORLD_NOW, *(v or 0 for v in vals))


async def _write_storm(
    graph: str,
    rng: random.Random,
    storm_id: str,
    faults: list[_StormFault],
    pages: list[tuple[str, int, str]],
    end: int,
) -> None:
    chg_n = await _max_id(graph, "Change", "chg-")
    changes = [c for f in faults for c in f.changes]
    for n, c in enumerate(sorted(changes, key=lambda c: (c.valid_from, rng.random())), 1):
        c.id = f"chg-{chg_n + n:05d}"
    order = changes[:]
    rng.shuffle(order)  # write order must not reveal the roots
    for c in order:
        await _write_change(graph, c)
    # Links between two new changes (the joint fault's flag flips) are made once every row
    # exists; their valid_to is already set on the objects.
    for child, parent in (link for f in faults for link in f.links):
        child.previous_id = parent.id
        await query(
            graph,
            "MATCH (c:Change {id: $id}), (q:Change {id: $prev}) "
            "CREATE (c)-[:PREVIOUS_VERSION]->(q)",
            {"id": child.id, "prev": parent.id},
        )

    alr_n = await _max_id(graph, "Alert", "alr-")
    keyed = sorted(pages, key=lambda p: (p[1], rng.random()))
    rows = [
        {"id": f"alr-{alr_n + n:05d}", "sid": svc, "at": t, "sig": sig}
        for n, (svc, t, sig) in enumerate(keyed, 1)
    ]
    rng.shuffle(rows)  # write order must not reveal which service failed first
    await query(
        graph,
        "CREATE (:Storm {id: $id, started_at: $start, ended_at: $end})",
        {"id": storm_id, "start": min(p[1] for p in pages), "end": end},
    )
    for i in range(0, len(rows), 200):
        await query(
            graph,
            "MATCH (st:Storm {id: $storm}) UNWIND $rows AS r MATCH (s:Service {id: r.sid}) "
            "CREATE (a:Alert {id: r.id, service_id: r.sid, fired_at: r.at, signature: r.sig}) "
            "CREATE (a)-[:FOR]->(s) CREATE (a)-[:PART_OF]->(st)",
            {"storm": storm_id, "rows": rows[i : i + 200]},
        )


async def inject_storm(
    world_graph: str,
    *,
    session_id: str,
    rng_seed: int,
    store: Store,
    faults: list[str] | None = None,
    pages: int | None = None,
) -> InjectedStorm:
    """Plant 1-3 independent faults (``faults``: training classes and/or ``joint``; default a
    bad deploy, a joint flag+config fault and sometimes a third) and fire their pages
    (``pages``: roughly how many, clamped to 200-400; default 220-380 at random)."""
    rng = random.Random(f"storm:{rng_seed}")
    if faults is None:
        faults = ["bad_deploy", JOINT_CLASS]
        if rng.random() < 0.5:
            faults.append(rng.choice(sorted(TRAINING_CLASSES)))
    if not 1 <= len(faults) <= 3 or any(f not in STORM_FAULT_CLASSES for f in faults):
        raise ValueError(f"storm faults must be 1-3 of {STORM_FAULT_CLASSES}: {faults!r}")

    base = await _storm_base(world_graph)
    start = base + rng.randint(9 * 60, 11 * 60) * 60
    alerts = [start + rng.randint(0, STORM_SPREAD_S) for _ in faults]
    end = max(alerts) + rng.randint(*STORM_TAIL_S)
    services = await _services(world_graph)
    g = await _impact_services(world_graph, end)
    ctx = _Ctx(world_graph, end)
    background = await _window_entities(world_graph, start - WINDOW_S - 3 * 3600, end)
    targets = sorted(
        s
        for s, (_, domain) in services.items()
        if domain != "edge" and s in g and sum(1 for _ in g.successors(s)) >= 2
    )
    rng.shuffle(targets)

    planned: list[_StormFault] = []
    for fault_class, alert in zip(faults, alerts, strict=True):
        prior = [
            _Ent(f"prior-{i}-{j}", c.service_id, c.valid_from, str(c.kind))
            for i, f in enumerate(planned)
            for j, c in enumerate(f.changes)
        ]
        taken = {s.service_id for f in planned for s in f.symptoms}
        found: _StormFault | None = None
        for target in targets[:150]:
            if target in taken:
                continue
            cand = await _plan_storm_fault(
                ctx, rng, g, services, fault_class, target, alert, background + prior
            )
            if cand is not None and _independent(cand, planned, g):
                found = cand
                break
        if found is None:
            raise InjectionError(f"no independent {fault_class} fault found for the storm")
        planned.append(found)

    fired = _storm_pages(rng, planned, end, pages)
    storm_id = f"storm-{await _max_id(world_graph, 'Storm', 'storm-') + 1:03d}"
    await _write_storm(world_graph, rng, storm_id, planned, fired, end)

    answers = []
    for n, f in enumerate(planned, 1):
        a = StormAnswer(
            fault_id=f"flt-{secrets.token_hex(8)}",
            session_id=session_id,
            storm_id=storm_id,
            key=f"{storm_id}-f{n}",
            fault_class=f.fault_class,
            root_ids=tuple(sorted(c.id for c in f.roots)),
            services=tuple(sorted({s.service_id for s in f.symptoms})),
            nonce=secrets.token_hex(16),
        )
        store.put_answer(
            fault_id=a.fault_id,
            session_id=session_id,
            incident_id=a.key,
            fault_class=a.fault_class,
            root_id=a.root_id,
            nonce=a.nonce,
            commitment=a.commitment,
        )
        answers.append(a)
    return InjectedStorm(storm_id, min(p[1] for p in fired), end, len(fired), answers)
