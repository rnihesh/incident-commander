"""Learning curve (U13, R21, F3; KTD15: a separate sequential run, memory NOT reset).

One session world (a copy of ``seed:world``) and N incidents, each a full run (reflex,
snapshots, triage, rehearsal, mocked PR and page, write-back) before the next is injected:

  1. an undeclared-coupling fault is injected and investigated; its write-back creates the
     ``LEARNED_DEPENDS_ON`` edge (caller -> callee) the run discovered on the causal path;
  2. every later incident is another undeclared-coupling fault rooted at the same callee,
     so its symptoms travel through the same learned coupling to callers. Different seeds
     give different symptom sets and alert services (the alert fires on the last service
     to fail).

``inject_fault`` places an undeclared-coupling root on the coupling's callee and has no
parameter forcing the caller, so each later seed is first probed on a scratch copy of the
session world (``s:{sid}:storm:probe``, its own answer store): only a seed whose symptoms
include both ends of the learned coupling is injected for real (deterministic: same world,
same seed, same plan). Each run's write-back then reports whether it confirmed the edge.

Forget phase (R8, KTD8; ``forget`` > 0): after the learned edge has confirmed, the world
changes so the coupling no longer holds: a deploy of the caller stops calling the callee
directly and reads it through a declared dependent of the callee instead, and the
``OBSERVED_CALL`` edge is deleted, so no later snapshot derives the direct hop. Each later
incident is a training fault whose symptoms include BOTH coupling ends (target ranked by the
odds of that, seeds probed on the scratch copy the same way), so both are symptomatic
through a different path while the cleared explanation uses no hop through the coupling:
the write-back contradicts the edge and halves its confidence. The phase stops once the
confidence is under ``MIN_CONFIDENCE`` (recall's seeding threshold) and one more such
incident has run, which shows recall no longer seeding the old suspect.

Per incident: registry tool calls, LLM steps, wall ms of the whole run, whether recall
offered a seed and whether the verdict's suspect was that seed, the learned edge's
confidence before and after the incident (also when the incident left it untouched),
whether the incident contradicted it and whether the edge seeded recall. Results go to
``learning_curve.json`` and into the ``learning_curve`` section of ``report.json`` /
``report.md`` (the 50-case accuracy runs are untouched).
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

import networkx as nx

from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.agent.llm import LLMClient
from ic.api.events import EventType
from ic.bench import report as rpt
from ic.bench.runner import DEFAULT_OUT, git_state
from ic.config import Settings
from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.runs.orchestrator import default_deps, execute_run
from ic.runs.registry import RunRegistry
from ic.sessions.manager import SessionManager
from ic.store import Store
from ic.tools.recall import MIN_CONFIDENCE
from ic.world.faults import TRAINING_CLASSES, InjectionError, inject_fault
from ic.world.faults.common import _Ctx, _impact_services, _max_id
from ic.world.faults.heldout import _declared_services

FAULT_CLASS = "undeclared_coupling"
FIRST_ATTEMPTS = 8  # seeds tried for an incident 1 that teaches a coupling
PROBE_ATTEMPTS = 40  # seeds probed per later incident
DIVERSITY_PROBES = 24  # probes estimating how many alert services sit behind a coupling
DIVERSITY_WANTED = 3  # distinct later alert services wanted before accepting a coupling
FORGET_CLASSES = sorted(TRAINING_CLASSES)  # fault classes of the contradicting incidents
FORGET_REACH = 3  # hops a training fault's symptoms spread (``_plan`` cutoff)
FORGET_SAMPLED = 3.5  # mean services a training fault samples from its reach (2-5)
FORGET_TARGETS = 3  # likeliest services probed per forget-phase incident
FORGET_PROBES = 120  # (target, class, seed) probes per forget-phase incident

EDGE_Q = """
MATCH (:Service {id: $caller})-[l:LEARNED_DEPENDS_ON]->(:Service {id: $callee})
RETURN l.confidence
"""

# The world change of the forget phase: a deploy of the caller stops calling the callee
# directly and goes through ``via`` (a declared dependent of the callee) instead.
DECOUPLE_Q = """
MATCH (a:Service {id: $caller})-[o:OBSERVED_CALL]->(b:Service {id: $callee}),
      (v:Service {id: $via})
CREATE (c:Change {id: $id, kind: 'deploy', service_id: a.id, summary: $summary,
                  valid_from: $at, valid_to: $open_end, recorded_at: $at,
                  value: $value, prev_value: $prev})
CREATE (c)-[:CHANGES]->(a)
CREATE (a)-[:DEPENDS_ON {valid_from: $at, valid_to: $open_end, recorded_at: $at}]->(v)
DELETE o
WITH c
OPTIONAL MATCH (p:Person {id: $author})
FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (p)-[:AUTHORED]->(c))
WITH c
OPTIONAL MATCH (q:Change {id: $prev_id})
FOREACH (_ IN CASE WHEN q IS NULL THEN [] ELSE [1] END |
         CREATE (c)-[:PREVIOUS_VERSION]->(q) SET q.valid_to = $at)
RETURN c.id
"""


def _settings(store_path: Path) -> Settings:
    """Mocked actions only: the learning curve never posts a PR or a page."""
    return Settings(
        store_path=str(store_path),
        github_token=None,
        github_repo=None,
        discord_webhook_url=None,
        llm_api_key=None,
        actions_per_hour=100_000,
    )


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await client.query(graph, cypher, params, read_only=True)).result_set


async def _symptomatic(world: str, iid: str) -> set[str]:
    rows = await _rows(
        world, "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom) RETURN y.service_id", {"iid": iid}
    )
    return {r[0] for r in rows}


async def _drop_prefix(prefix: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(prefix):
            await db.select_graph(name).delete()


class LearningSession:
    def __init__(self, store: Store, settings: Settings, llm: LLMClient | None):
        self.store = store
        self.sessions = SessionManager(store, max_sessions=1000)
        self.deps = default_deps(
            store,
            settings,
            llm,
            runs=RunRegistry(store),
            sessions=self.sessions,
            github=GitHubClient(None, None, mock=True),
            pager=DiscordPager(None, mock=True),
        )
        self.sid: str | None = None
        self.history: list = []  # every Run, in order (events stay inspectable)
        self.coupling: tuple[str, str] | None = None  # the learned edge followed per incident

    async def open(self) -> str:
        sess = await self.sessions.create("127.0.0.1")
        self.sid = sess.sid
        return sess.sid

    @property
    def world(self) -> str:
        assert self.sid
        return naming.session_world(self.sid)

    async def edge_confidence(self) -> float | None:
        if self.coupling is None:
            return None
        caller, callee = self.coupling
        rs = await _rows(self.world, EDGE_Q, {"caller": caller, "callee": callee})
        return round(float(rs[0][0]), 6) if rs and rs[0][0] is not None else None

    async def annotate(self, row: dict, before: float | None) -> dict:
        """The followed edge per incident: confidence before / after (touched or not), the
        write-back's action on it, and whether recall seeded a suspect because of it."""
        assert self.coupling
        caller, callee = self.coupling
        run = self.history[-1]
        recall = next(
            (t["result"] for t in run.of(EventType.TOOL_CALL) if t.get("tool") == "recall"), {}
        )
        mark = f"learned {caller} -> {callee} "
        actions = [
            e["action"] for e in row["learned"] if (e["caller"], e["callee"]) == self.coupling
        ]
        row["edge_confidence_before"] = before
        row["edge_confidence"] = await self.edge_confidence()
        row["edge_action"] = actions[0] if actions else None
        row["contradicted"] = "contradicted" in actions
        row["edge_seeded"] = any(
            r.startswith(mark) for sd in recall.get("seeds") or [] for r in sd.get("reasons") or []
        )
        return row

    async def run(self, f) -> dict:
        """One full investigation of an injected fault; returns its learning-curve row."""
        assert self.sid
        before = await self.edge_confidence()
        run = self.deps.runs.create(self.sid, f.incident_id)
        t0 = time.perf_counter()
        await execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=self.deps)
        wall = round((time.perf_counter() - t0) * 1000, 1)
        self.history.append(run)
        verdict = (run.of(EventType.VERDICT) or [{}])[0]
        memory = (run.of(EventType.MEMORY) or [None])[0]
        recs = [
            n
            for n in run.of(EventType.NARRATION)
            if n.get("topic") == "recommendation" and n.get("recommendations")
        ]
        states = [d["state"] for d in run.of(EventType.STATE)]
        mem = verdict.get("memory") or {}
        row = {
            "incident_id": f.incident_id,
            "alert_service": f.alert_service_id,
            "alert_time": f.alert_time,
            "root_id": f.answer.root_id,
            "verdict": verdict.get("outcome"),
            "suspect_id": verdict.get("suspect_id"),
            "correct": verdict.get("suspect_id") == f.answer.root_id
            and verdict.get("outcome") == "DIAGNOSED",
            "tool_calls": verdict.get("tool_calls"),
            "steps": verdict.get("steps"),
            "ms": wall,
            "recall_used": bool(mem.get("used")),
            "memory_seeded": bool(mem.get("seeded")),
            "final_state": states[-1] if states else None,
            "states": states,
            "symptomatic": sorted(await _symptomatic(self.world, f.incident_id)),
            "learned": (memory or {}).get("learned") or [],
            "recommendations_on_path": [r["id"] for n in recs for r in n["recommendations"]],
        }
        return await self.annotate(row, before) if self.coupling else row


async def probe(
    world: str,
    sid: str,
    target: str,
    seed: int,
    need: set[str],
    probe_store: Store,
    fault_class: str = FAULT_CLASS,
) -> tuple[bool, str | None]:
    """Inject on a scratch copy; (symptoms include ``need``, alert service)."""
    probe = naming.storm(sid, "probe")
    await client.copy_graph(world, probe)
    try:
        f = await inject_fault(
            probe,
            session_id=sid,
            fault_class=fault_class,
            rng_seed=seed,
            target_service=target,
            store=probe_store,
        )
        return need <= await _symptomatic(probe, f.incident_id), f.alert_service_id
    except InjectionError:
        return False, None
    finally:
        await client.delete_graph(probe)


async def _alert_spread(
    world: str, sid: str, coupling: tuple[str, str], start: int, probe_store: Store
) -> set[str]:
    """Alert services of probe injections that route through ``coupling``."""
    caller, callee = coupling
    alerts: set[str] = set()
    for k in range(DIVERSITY_PROBES):
        ok, alert = await probe(world, sid, callee, start + k, {caller, callee}, probe_store)
        if ok and alert:
            alerts.add(alert)
    return alerts


async def _reroute_via(world: str, caller: str, callee: str, t: int) -> str:
    """A declared dependent of the callee the caller can go through instead: not the
    caller, not downstream of it (no cycle), same region; the smallest id."""
    g = await _impact_services(world, t)
    declared = await _declared_services(world, t)
    region = caller.split("@")[-1]
    below = nx.descendants(g, caller) if caller in g else set()
    vias = sorted(
        s
        for s in (declared.successors(callee) if callee in declared else [])
        if s != caller and s not in below and s.split("@")[-1] == region
    )
    if not vias:
        raise RuntimeError(f"no declared dependent of {callee} to route {caller} through")
    return vias[0]


async def remove_coupling(world: str, caller: str, callee: str) -> dict:
    """KTD8 world change: a deploy of the caller (next minor version on its deploy chain),
    after everything injected so far, stops calling the callee directly and reads it through
    a declared dependent of the callee instead (a new DEPENDS_ON, valid from the deploy).
    The ``OBSERVED_CALL`` edge goes, so no later snapshot derives the callee -> caller hop:
    a callee failure still reaches the caller, but two hops away through that dependent,
    and the learned direct edge is on no causal path. The deploy lands hours before the
    next alert window, so it is never a candidate."""
    c = (await _rows(world, "MATCH (c:Change) RETURN max(c.valid_from)"))[0][0]
    i = (await _rows(world, "MATCH (i:Incident) RETURN max(i.opened_at)"))[0][0]
    at = max(int(c or 0), int(i or 0)) + 60
    via = await _reroute_via(world, caller, callee, at)
    info = await _Ctx(world, at).info(caller)
    prev = info.chains.get("deploy")
    before = prev[1] if prev else "v1.0.0"
    major, minor, _ = (int(x) for x in before.lstrip("v").split("."))
    value = f"v{major}.{minor + 1}.0"
    cid = f"chg-{await _max_id(world, 'Change', 'chg-') + 1:05d}"
    summary = f"Deploy {value}: read {callee} through {via} instead of calling it directly"
    params = {
        "caller": caller,
        "callee": callee,
        "via": via,
        "id": cid,
        "summary": summary,
        "at": at,
        "open_end": OPEN_END,
        "value": value,
        "prev": before,
        "prev_id": prev[0] if prev else None,
        "author": info.members[0] if info.members else None,
    }
    if not (await client.query(world, DECOUPLE_Q, params)).result_set:
        raise RuntimeError(f"{caller} has no observed call to {callee} to remove")
    return {
        "id": cid,
        "service_id": caller,
        "kind": "deploy",
        "at": at,
        "summary": summary,
        "via": via,
    }


def _both_ends(g: nx.DiGraph, caller: str, callee: str) -> list[tuple[float, str]]:
    """(odds, service) for services whose training faults can reach both coupling ends over
    ``g`` without the callee -> caller hop (either end itself included), likeliest first. A
    training fault marks its target plus a few services sampled from those within
    ``FORGET_REACH`` hops, so the odds that both ends are among them fall with that pool."""
    g = g.copy()
    if g.has_edge(callee, caller):
        g.remove_edge(callee, caller)
    out = []
    for s in g.nodes:
        d = nx.single_source_shortest_path_length(g, s, cutoff=FORGET_REACH)
        if caller in d and callee in d:
            pool = sum(1 for k in d.values() if k >= 1)
            need = len({caller, callee} - {s})
            out.append((round(min(1.0, FORGET_SAMPLED / pool) ** need, 4), s))
    return sorted(out, key=lambda x: (-x[0], x[1]))


async def forget_targets(world: str, caller: str, callee: str, t: int) -> list[str]:
    return [s for _, s in _both_ends(await _impact_services(world, t), caller, callee)]


def summarize(rows: list[dict]) -> dict:
    calls = [r["tool_calls"] for r in rows if r["tool_calls"] is not None]
    if not calls:
        return {}
    later = calls[1:]
    out = {
        "first_tool_calls": calls[0],
        "second_tool_calls": calls[1] if len(calls) > 1 else None,
        "later_mean_tool_calls": round(mean(later), 2) if later else None,
        "tool_calls": calls,
        "correct": sum(bool(r["correct"]) for r in rows),
        "n": len(rows),
        "recall_used_after_first": sum(r["recall_used"] for r in rows[1:]),
        "ms": [r["ms"] for r in rows],
    }
    if later:
        out["reduction_pct"] = round(100 * (1 - mean(later) / calls[0]), 1) if calls[0] else None
    return out


async def _forget(
    sess: LearningSession,
    rows: list[dict],
    budget: int,
    seed: int,
    store: Store,
    probe_store: Store,
    log,
) -> dict:
    """Forget phase (R8, KTD8): remove the direct coupling from the world, then run incidents
    whose symptoms include both of its ends (probed first) until the edge's confidence is
    under ``MIN_CONFIDENCE`` and one more incident has run; at most ``budget`` incidents."""
    assert sess.sid and sess.coupling
    caller, callee = sess.coupling
    change = await remove_coupling(sess.world, caller, callee)
    targets = await forget_targets(sess.world, caller, callee, change["at"] + 1)
    combos = [(t, c) for t in targets[:FORGET_TARGETS] for c in FORGET_CLASSES]
    log(
        f"forget: {change['id']} drops {caller} -> {callee} (now via {change['via']}) at "
        f"t={change['at']}; {len(targets)} services reach both ends without it"
    )
    picks: list[dict] = []
    cursor = seed * 1000 + 700
    k = 0
    while combos and len(picks) < budget:
        before = await sess.edge_confidence()
        pick = None
        for _ in range(FORGET_PROBES):
            cursor += 1
            target, cls = combos[k % len(combos)]
            k += 1
            ok, _alert = await probe(
                sess.world, sess.sid, target, cursor, {caller, callee}, probe_store, cls
            )
            if ok:
                pick = {"fault_class": cls, "target": target, "seed": cursor}
                break
        if pick is None:
            log(f"forget: no seed puts both {caller} and {callee} in the symptoms; stopping")
            break
        f = await inject_fault(
            sess.world,
            session_id=sess.sid,
            fault_class=pick["fault_class"],
            rng_seed=pick["seed"],
            target_service=pick["target"],
            store=store,
        )
        row = await sess.run(f)
        check = before is not None and before < MIN_CONFIDENCE
        row.update(phase="forget", check=check, **pick)
        rows.append(row)
        picks.append({"incident_id": f.incident_id, **pick})
        log(
            f"#{len(rows)} {f.incident_id} {pick['fault_class']} on {pick['target']} "
            f"seed={pick['seed']} verdict={row['verdict']} state={row['final_state']} "
            f"tool_calls={row['tool_calls']} edge {before} -> {row['edge_confidence']} "
            f"({row['edge_action'] or 'untouched'}) edge_seeded={row['edge_seeded']}"
        )
        if check or row["edge_confidence"] is None:
            break
    forget_rows = [r for r in rows if r.get("phase") == "forget"]
    return {
        "requested": budget,
        "threshold": MIN_CONFIDENCE,
        "change": change,
        "targets": targets[:FORGET_TARGETS],
        "picks": picks,
        "contradictions": sum(r["contradicted"] for r in forget_rows),
        "below_threshold": any((r["edge_confidence"] or 0) < MIN_CONFIDENCE for r in forget_rows),
        "check_incident": next((r["incident_id"] for r in forget_rows if r["check"]), None),
    }


async def run_learning_curve(
    *,
    runs: int = 6,
    forget: int = 0,
    seed: int = 42,
    out: str | Path | None = DEFAULT_OUT,
    llm: LLMClient | None = None,
    log=print,
) -> dict:
    """Run the sequence and (when ``out`` is set) write learning_curve.json and the
    learning-curve section of report.json / report.md."""
    if runs < 2:
        raise ValueError("a learning curve needs at least two incidents")
    t_start = time.perf_counter()
    tmp = tempfile.TemporaryDirectory(prefix="ic-learn-")
    store = Store(Path(tmp.name) / "learn.sqlite3")
    probe_store = Store(Path(tmp.name) / "probe.sqlite3")
    settings = _settings(Path(tmp.name) / "learn.sqlite3")
    sess = LearningSession(store, settings, llm)
    rows: list[dict] = []
    coupling: tuple[str, str] | None = None
    seeds_used: list[int] = []
    discarded: list[dict] = []  # incident-1 attempts not kept
    sids: list[str] = []
    forgot: dict | None = None
    try:
        # 1. incident 1: an undeclared coupling whose resolution teaches a learned edge, on
        #    a coupling with enough distinct downstream alert services for the sequence.
        want = min(runs - 1, DIVERSITY_WANTED)
        best: tuple[int, int] | None = None  # (distinct alerts, seed)
        for attempt in range(FIRST_ATTEMPTS + 1):
            last = attempt == FIRST_ATTEMPTS
            if last and best is None:
                break
            s0 = best[1] if last else seed * 1000 + attempt
            sid = await sess.open()
            sids.append(sid)
            try:
                f = await inject_fault(
                    sess.world, session_id=sid, fault_class=FAULT_CLASS, rng_seed=s0, store=store
                )
            except InjectionError:
                discarded.append({"seed": s0, "reason": "injection failed"})
                continue
            row = await sess.run(f)
            new = [e for e in row["learned"] if e["action"] == "created"]
            found = None
            if row["correct"] and new:
                root_svc = (
                    await _rows(
                        sess.world,
                        "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) RETURN s.id",
                        {"id": f.answer.root_id},
                    )
                )[0][0]
                pick = next((e for e in new if e["callee"] == root_svc), new[0])
                found = (pick["caller"], pick["callee"])
            alerts: set[str] = set()
            if found:
                alerts = await _alert_spread(sess.world, sid, found, seed * 1000 + 500, probe_store)
                alerts.discard(f.alert_service_id)
            log(
                f"#1 {f.incident_id} seed={s0} alert={f.alert_service_id} verdict={row['verdict']} "
                f"tool_calls={row['tool_calls']} learned={found} downstream_alerts={len(alerts)}"
            )
            if found and (len(alerts) >= want or last):
                coupling = sess.coupling = found
                await sess.annotate(row, None)
                row["phase"] = "learn"
                row["coupling_confirmed"] = None
                row["selected"] = True
                rows.append(row)
                seeds_used.append(s0)
                break
            if found and (best is None or len(alerts) > best[0]):
                best = (len(alerts), s0)
            discarded.append(
                {
                    "seed": s0,
                    "verdict": row["verdict"],
                    "correct": row["correct"],
                    "tool_calls": row["tool_calls"],
                    "taught_coupling": found is not None,
                    "downstream_alerts": len(alerts),
                    "reason": (
                        "no coupling learned"
                        if not found
                        else "too few distinct downstream alert services"
                    ),
                }
            )
            await _drop_prefix(f"s:{sid}:")
            await _drop_prefix(f"pr:{sid}:")
        if coupling is None:
            raise RuntimeError("no first incident taught a coupling")
        caller, callee = coupling
        assert sess.sid

        # 2. later incidents downstream of the same coupling, different alert services
        seen_alerts = {rows[0]["alert_service"]}
        cursor = seed * 1000 + 100
        while len(rows) < runs:
            chosen = fallback = None
            for _ in range(PROBE_ATTEMPTS):
                cursor += 1
                ok, alert = await probe(
                    sess.world, sess.sid, callee, cursor, {caller, callee}, probe_store
                )
                if not ok:
                    continue
                if alert not in seen_alerts:
                    chosen = cursor
                    break
                fallback = fallback or cursor
            pick_seed = chosen or fallback
            if pick_seed is None:
                log(f"no further seed routes through {caller} -> {callee}; stopping")
                break
            f = await inject_fault(
                sess.world,
                session_id=sess.sid,
                fault_class=FAULT_CLASS,
                rng_seed=pick_seed,
                target_service=callee,
                store=store,
            )
            row = await sess.run(f)
            row["phase"] = "learn"
            row["coupling_confirmed"] = row["edge_action"] == "confirmed"
            rows.append(row)
            seeds_used.append(pick_seed)
            seen_alerts.add(f.alert_service_id)
            log(
                f"#{len(rows)} {f.incident_id} seed={pick_seed} alert={f.alert_service_id} "
                f"verdict={row['verdict']} tool_calls={row['tool_calls']} "
                f"recall={row['recall_used']} confirmed={row['coupling_confirmed']}"
            )

        # 3. forget phase (KTD8): remove the coupling, then contradict it until recall drops it
        if forget > 0:
            forgot = await _forget(sess, rows, forget, seed, store, probe_store, log)
    finally:
        await sess.deps.runs.wait_all(limit_s=60)
        for sid in sids:
            await _drop_prefix(f"s:{sid}:")
            await _drop_prefix(f"pr:{sid}:")  # the PR copies of the mocked rollback PRs
        tmp.cleanup()

    commit, dirty = git_state()
    learn = [r for r in rows if r.get("phase") != "forget"]
    method = (
        "One session world, incidents investigated in sequence with write-back after "
        "each RESOLVED run. Incident 1 is selected by retry-until-correct: attempts "
        "that were wrong, taught no coupling, or had too few downstream alert "
        "services are discarded and listed in discarded_incident1_attempts (the "
        "first incident shown is the first acceptable attempt, not the first "
        "attempt). Incident 1 teaches LEARNED_DEPENDS_ON "
        f"{caller} -> {callee}; later faults are rooted on {callee} and their "
        "symptoms include both ends of the coupling (seeds probed on a scratch copy "
        "first). Tool calls count every registry call of the triage run (recall, "
        "screening, proofs and the verdict's causal path)."
    )
    if forgot is not None:
        method += (
            f" Forget phase: {forgot['change']['id']} (a deploy of {caller}) stops calling "
            f"{callee} directly and goes through {forgot['change']['via']} (declared), and the "
            "observed-call edge is removed; each later incident is a training fault whose "
            "symptoms include both ends (seeds probed first), so both are symptomatic through "
            "another path, the cleared explanation uses no hop through the coupling and the "
            "write-back halves the learned edge's confidence. It runs until the "
            f"confidence is under the recall threshold {MIN_CONFIDENCE} plus one more "
            "incident. edge_confidence is read from the world after every incident."
        )
    result = {
        "meta": {
            "seed": seed,
            "runs": len(learn),
            "requested_runs": runs,
            "fault_class": FAULT_CLASS,
            "coupling": {"caller": caller, "callee": callee},
            "seeds": seeds_used,
            "forget": forgot,
            "discarded_incident1_attempts": discarded,
            "llm": "none (deterministic triage)" if llm is None else getattr(llm, "model", "?"),
            "commit": commit,
            "dirty": dirty,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wall_s": round(time.perf_counter() - t_start, 1),
            "method": method,
        },
        "incidents": rows,
        "summary": summarize(learn),
    }
    if out is not None:
        write(out, result, log=log)
    return result


def write(out: str | Path, result: dict, log=print) -> None:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    lp = out / "learning_curve.json"
    lp.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    rp = out / "report.json"
    if rp.exists():
        report = json.loads(rp.read_text())
        report["learning_curve"] = result
        rpt.write_report(out, report)
        log(f"wrote {lp} and the learning-curve section of {rp} / report.md")
    else:
        log(f"wrote {lp} (no report.json to update)")


def main(args) -> None:
    import asyncio

    from ic.bench.runner import resolve_llm

    llm = resolve_llm(args.llm)
    try:
        asyncio.run(
            run_learning_curve(
                runs=args.runs,
                forget=getattr(args, "forget", 0) or 0,
                seed=args.seed,
                out=args.out,
                llm=llm,
            )
        )
    finally:
        client.reset_client()
