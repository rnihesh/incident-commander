"""Run orchestration (U9) and remediation (U11 actions, U12 rehearsal) on real FalkorDB."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from ic.actions import pr_store
from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.agent import remediation
from ic.agent.loop import EventLog
from ic.agent.triage import run_triage
from ic.api.events import EventType
from ic.config import Settings
from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.graph.snapshots import build_snapshots
from ic.incidents import state as st
from ic.proof import ledger
from ic.runs import orchestrator
from ic.runs.orchestrator import default_deps, execute_run, start_run
from ic.runs.registry import RunRegistry
from ic.sessions.manager import SessionManager
from ic.store import Store
from ic.tools.registry import ToolContext
from ic.world.faults import inject_fault

SEED = 3


def settings(tmp) -> Settings:
    return Settings(
        store_path=str(tmp / "ic.sqlite3"),
        github_token=None,
        github_repo=None,
        discord_webhook_url=None,
        llm_api_key=None,
        actions_per_hour=1000,
    )


async def drop(prefixes: list[str]) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if any(name.startswith(p) for p in prefixes):
            await db.select_graph(name).delete()


@pytest.fixture
async def env(tmp_path):
    """A store, deps with mocked actions, and a session manager; graphs dropped after."""
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    s = settings(tmp_path)
    sessions = SessionManager(store, max_sessions=50)
    deps = default_deps(
        store,
        s,
        None,
        runs=RunRegistry(store),
        sessions=sessions,
        github=GitHubClient(None, None, mock=True),
        pager=DiscordPager(None, mock=True),
    )
    made: list[str] = []
    yield store, s, sessions, deps, made
    await deps.runs.wait_all(limit_s=60)
    await drop([f"s:{sid}:" for sid in made])
    for name in pr_store.tracked_pr_graphs(store):
        await client.delete_graph(name)
    client.reset_client()


async def new_session(sessions: SessionManager, made: list[str]):
    sess = await sessions.create("127.0.0.1")
    made.append(sess.sid)
    return sess


async def add_second_qualifier(world: str, root: str) -> str:
    """AE1: a second deploy on the root's service, also before onset (as in the agent tests)."""
    vf, svc = (
        await client.query(
            world,
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) RETURN c.valid_from, s.id",
            {"id": root},
        )
    ).result_set[0]
    await client.query(
        world,
        "MATCH (s:Service {id: $svc}) "
        "CREATE (c:Change {id: 'chg-99001', kind: 'deploy', service_id: $svc, "
        "summary: 'Deploy hotfix: connection pool tuning', valid_from: $vf, "
        "valid_to: $vt, recorded_at: $rec, value: 'v9.9.9', prev_value: null}) "
        "CREATE (c)-[:CHANGES]->(s)",
        {"svc": svc, "vf": vf + 30, "vt": OPEN_END, "rec": vf + 40},
    )
    return "chg-99001"


# --- full runs --------------------------------------------------------------------------


async def test_start_run_diagnoses_remediates_and_releases_the_session(env):
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    run_id = await start_run(
        sess,
        f.incident_id,
        f.alert_time,
        f.alert_service_id,
        llm=None,
        store=store,
        settings=s,
        deps=deps,
    )
    assert sessions.get(sess.sid).active_runs == 1  # counted before the task runs
    run = deps.runs.get(run_id)
    await run.task
    assert run.finished and sessions.get(sess.sid).active_runs == 0
    verdict = run.of(EventType.VERDICT)[0]
    assert verdict["outcome"] == "DIAGNOSED"
    assert verdict["root_cause"]["change"] == f.answer.root_id
    states = [d["state"] for d in run.of(EventType.STATE)]
    assert states == [
        "TRIAGING",
        "DIAGNOSED",
        "REHEARSING",
        "ACTING",
        "PR_OPEN",
        "RESOLVED",
        "LEARNED",  # U13: write-back after the CLEARED rehearsal
    ]
    lg = naming.ledger(sess.sid)
    assert await st.get_state(lg, f.incident_id) == st.State.LEARNED
    [memory] = run.of(EventType.MEMORY)
    assert memory["incident_id"] == f.incident_id and memory["cause_id"] == f.answer.root_id

    # Trusted frame: the ledger and the anchor carry the run's own t and world.
    anchor = store.get_anchor(sess.sid, f.incident_id)
    assert anchor["t"] == f.alert_time and anchor["world"] == world

    # PR copies exist and are tracked outside the session (they outlive its TTL).
    pr_names = (naming.pr_world(sess.sid, f.incident_id), naming.pr_ledger(sess.sid, f.incident_id))
    for name in pr_names:
        assert await client.graph_exists(name)
        assert name in pr_store.tracked_pr_graphs(store)
        assert name not in sessions.tracked_graphs(sess.sid)
    # Snapshots and the ledger are tracked for the session cleanup.
    tracked = set(sessions.tracked_graphs(sess.sid))
    assert {naming.asof(sess.sid, f.incident_id, v) for v in naming.VIEWS} <= tracked
    assert naming.ledger(sess.sid) in tracked

    # Rehearsal (U12): CLEARED on a tracked clone that no longer exists.
    [rehearsal] = run.of(EventType.REHEARSAL)
    assert rehearsal["result"] == "CLEARED" and rehearsal["change"] == f.answer.root_id
    assert rehearsal["still_explains"] == [] and rehearsal["checked"] > 0
    clone = naming.rehearse(sess.sid, f.incident_id)
    assert clone in tracked and not await client.graph_exists(clone)
    types = run.types
    assert types.index("handoff") < types.index("rehearsal") < types.index("action")

    # The mocked actions saw the proof-carrying PR and the page.
    [pr_call] = [c for c in deps.github.calls if c["op"] == "open_rollback_pr"]
    assert verdict["chain_head"] in pr_call["body"]
    [status_call] = [c for c in deps.github.calls if c["op"] == "post_proof_status"]
    assert status_call["state"] == "success"
    assert len(deps.pager.calls) == 1


async def test_inconclusive_pages_and_opens_no_pr(env):
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    second = await add_second_qualifier(world, f.answer.root_id)
    run = deps.runs.create(sess.sid, f.incident_id)
    await execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=deps)

    verdict = run.of(EventType.VERDICT)[0]
    assert verdict["outcome"] == "INCONCLUSIVE"
    actions = run.of(EventType.ACTION)
    assert [a["kind"] for a in actions] == ["page"]
    assert sorted(actions[0]["surviving"]) == sorted([f.answer.root_id, second])
    assert run.of(EventType.HANDOFF) == [] and run.of(EventType.REHEARSAL) == []
    assert not [c for c in deps.github.calls if c["op"] == "open_rollback_pr"]
    assert len(deps.pager.calls) == 1
    embed = deps.pager.calls[0]["payload"]["embeds"][0]
    assert "INCONCLUSIVE" in embed["fields"][0]["value"]
    assert run.types[-1] == "done"
    assert await st.get_state(naming.ledger(sess.sid), f.incident_id) == st.State.INCONCLUSIVE
    assert pr_store.tracked_pr_graphs(store) == []  # no PR copies either
    # U13: nothing is written back after INCONCLUSIVE
    assert run.of(EventType.MEMORY) == []
    left = await client.query(
        world,
        "MATCH (i:Incident {id: $iid}) RETURN i.memory_written, i.status, "
        "size([(i)-[:CAUSED_BY]->() | 1]), size([(i)<-[:ABOUT]-() | 1])",
        {"iid": f.incident_id},
        read_only=True,
    )
    assert left.result_set == [[None, "open", 0, 0]]


async def test_unexpected_exception_emits_error_then_done(env, monkeypatch):
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="flag_flip", rng_seed=SEED, store=store
    )

    async def boom(*a, **kw):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(orchestrator, "run_triage", boom)
    run_id = await start_run(
        sess,
        f.incident_id,
        f.alert_time,
        f.alert_service_id,
        llm=None,
        store=store,
        settings=s,
        deps=deps,
    )
    run = deps.runs.get(run_id)
    await run.task
    assert run.types[-2:] == ["error", "done"]
    assert "triage exploded" in run.of(EventType.ERROR)[0]["message"]
    assert run.of(EventType.ERROR)[0]["code"] == "failed"
    assert sessions.get(sess.sid).active_runs == 0


@pytest.mark.parametrize("stage", ["opened", "triaging"])
async def test_cancelled_run_fails_the_incident_and_releases_the_session(env, monkeypatch, stage):
    """Cancelled mid-triage: state(FAILED), then error, then done; the session slot is freed."""
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="flag_flip", rng_seed=SEED, store=store
    )
    inside = asyncio.Event()

    async def stuck(ctx, iid, **kw):
        await ledger.open_incident(ctx.sid, iid, ctx.trusted_t, ctx.world_graph)
        if stage == "triaging":
            await st.transition(ctx.ledger_graph, iid, st.State.TRIAGING)
        inside.set()
        await asyncio.Event().wait()  # an LLM call that never returns

    monkeypatch.setattr(orchestrator, "run_triage", stuck)
    run_id = await start_run(
        sess,
        f.incident_id,
        f.alert_time,
        f.alert_service_id,
        llm=None,
        store=store,
        settings=s,
        deps=deps,
    )
    run = deps.runs.get(run_id)
    await asyncio.wait_for(inside.wait(), 60)
    assert sessions.get(sess.sid).active_runs == 1
    run.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run.task
    assert run.finished
    assert run.types[-3:] == ["state", "error", "done"]
    assert run.of(EventType.STATE)[-1] == {"state": "FAILED"}
    assert run.of(EventType.ERROR)[-1]["message"] == "the run was cancelled"
    assert sessions.get(sess.sid).active_runs == 0
    assert await st.get_state(naming.ledger(sess.sid), f.incident_id) == st.State.FAILED


async def test_run_cancelled_before_triage_still_closes(env, monkeypatch):
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="flag_flip", rng_seed=SEED, store=store
    )
    inside = asyncio.Event()

    async def stuck_snapshots(*a, **kw):
        inside.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator, "build_snapshots", stuck_snapshots)
    run_id = await start_run(
        sess, f.incident_id, f.alert_time, f.alert_service_id, llm=None, store=store,
        settings=s, deps=deps,
    )  # fmt: skip
    run = deps.runs.get(run_id)
    await asyncio.wait_for(inside.wait(), 60)
    run.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run.task
    assert run.types[-2:] == ["error", "done"] and run.of(EventType.STATE) == []
    assert sessions.get(sess.sid).active_runs == 0
    lg = naming.ledger(sess.sid)  # nothing was opened, so nothing is left mid-triage
    assert not await client.graph_exists(lg) or await st.get_state(lg, f.incident_id) is None


async def test_same_incident_id_in_two_sessions_keeps_separate_pr_copies(env):
    """Incident ids repeat across sessions; each session's PR copies stay its own."""
    store, s, sessions, deps, made = env
    runs = []
    for tag in ("A", "B"):
        sess = await new_session(sessions, made)
        world = naming.session_world(sess.sid)
        f = await inject_fault(
            world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
        )
        await client.query(world, "CREATE (:Marker {owner: $tag})", {"tag": tag})
        runs.append((tag, sess, f, deps.runs.create(sess.sid, f.incident_id)))
    (_, sa, fa, _), (_, sb, fb, _) = runs
    assert fa.incident_id == fb.incident_id  # the collision this naming exists for
    iid = fa.incident_id
    await asyncio.gather(
        *(
            execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=deps)
            for _, _, f, run in runs
        )
    )
    refs = set()
    for tag, sess, _, run in runs:
        assert "PR_OPEN" in [d["state"] for d in run.of(EventType.STATE)]
        assert await st.get_state(naming.ledger(sess.sid), iid) == st.State.LEARNED
        pw, pl = naming.pr_world(sess.sid, iid), naming.pr_ledger(sess.sid, iid)
        marker = await client.query(pw, "MATCH (m:Marker) RETURN m.owner", read_only=True)
        assert [r[0] for r in marker.result_set] == [tag]  # intact: not the other's copy
        li = await client.query(
            pl, "MATCH (i:LedgerIncident {id: $iid}) RETURN i.sid, i.state", {"iid": iid}
        )
        assert li.result_set == [[sess.sid, "ACTING"]]  # copied while ACTING
        other = sb if sess is sa else sa
        mine = ToolContext(sid=sess.sid, iid=iid)
        assert mine.allows(pw) and mine.allows(pl)
        assert not mine.allows(naming.pr_world(other.sid, iid))
        assert not mine.allows(naming.pr_ledger(other.sid, iid))
        ref = pr_store.proof_ref(store, sess.sid, iid)
        assert ref and pr_store.resolve_proof_ref(store, ref) == (sess.sid, iid)
        url = run.of(EventType.ACTION)[1]["url"]  # the commit status target
        assert url.endswith(f"/proof/{ref}") and sess.sid not in url
        refs.add(ref)
    assert len(refs) == 2

    # A tracking row owned by another session is never taken over.
    with pytest.raises(pr_store.PrGraphConflict):
        pr_store.track_pr_graph(store, naming.pr_world(sa.sid, iid), iid, sb.sid)


# --- remediation claim ------------------------------------------------------------------


async def test_two_concurrent_claims_exactly_one_wins(env):
    _, _, sessions, _, made = env
    sess = await new_session(sessions, made)
    lg = naming.ledger(sess.sid)
    for to in (st.State.TRIAGING, st.State.DIAGNOSED):
        await st.transition(lg, "inc-777", to)
    results = await asyncio.gather(
        *(st.claim(lg, "inc-777", st.State.DIAGNOSED, st.State.REHEARSING) for _ in range(2))
    )
    assert sorted(results) == [False, True]
    assert await st.get_state(lg, "inc-777") == st.State.REHEARSING


async def test_two_concurrent_remediations_exactly_one_proceeds(env):
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    sid, world = sess.sid, naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    h = await build_snapshots(world, sid, f.incident_id, f.alert_time)
    ctx = ToolContext(
        sid=sid,
        iid=f.incident_id,
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
        ledger_graph=naming.ledger(sid),
        trusted_t=f.alert_time,
        trusted_world=world,
    )
    out = await run_triage(ctx, f.incident_id, llm=None, emit=None, store=store)
    assert out.verdict == "DIAGNOSED"
    logs = [EventLog("a"), EventLog("b")]

    def go(log):
        return remediation.run_remediation(
            sid=sid,
            iid=f.incident_id,
            t=f.alert_time,
            alert_service_id=f.alert_service_id,
            title="t",
            emit=log,
            store=store,
            settings=s,
            snapshot_ctx=ctx,
            github=GitHubClient(None, None, mock=True),
            pager=DiscordPager(None, mock=True),
        )

    results = await asyncio.gather(go(logs[0]), go(logs[1]))
    assert sorted(r.claimed for r in results) == [False, True]
    winner = next(r for r in results if r.claimed)
    assert winner.final_state == "RESOLVED" and winner.pr is not None
    assert winner.proof_status == "VERIFIED" and winner.commit_state == "success"
    assert sum(len(log.of(EventType.ACTION)) for log in logs) == 3  # one PR, status, page
    assert await st.get_state(naming.ledger(sid), f.incident_id) == st.State.RESOLVED


async def test_remediation_refuses_an_incident_that_is_not_diagnosed(env):
    store, s, sessions, _, made = env
    sess = await new_session(sessions, made)
    lg = naming.ledger(sess.sid)
    await st.transition(lg, "inc-778", st.State.TRIAGING)
    log = EventLog("x")
    res = await remediation.run_remediation(
        sid=sess.sid,
        iid="inc-778",
        t=0,
        alert_service_id="svc",
        title="t",
        emit=log,
        store=store,
        settings=s,
        snapshot_ctx=ToolContext(sid=sess.sid, iid="inc-778"),
    )
    assert res.claimed is False and log.of(EventType.ACTION) == []
    assert await st.get_state(lg, "inc-778") == st.State.TRIAGING


async def test_github_failure_leaves_the_incident_acting_with_a_retryable_error(env):
    import httpx

    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    failing = GitHubClient(
        "tok",
        "acme/sandbox",
        transport=httpx.MockTransport(lambda req: httpx.Response(502, json={"message": "bad"})),
    )
    deps = dataclasses.replace(deps, github=failing)
    run = deps.runs.create(sess.sid, f.incident_id)
    await execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=deps)
    errors = run.of(EventType.ERROR)
    assert errors and errors[0]["code"] == "action_failed" and errors[0]["retryable"] is True
    actions = run.of(EventType.ACTION)
    assert {
        "kind": "pr",
        "status": "failed",
        "label": "pr failed",
        "detail": errors[0]["message"],
    } in actions
    # The on-call is paged regardless, with the failure noted.
    assert actions[-1]["kind"] == "page" and actions[-1]["status"] == "ok"
    assert "rollback PR failed" in actions[-1]["detail"]
    assert await st.get_state(naming.ledger(sess.sid), f.incident_id) == st.State.ACTING
    assert run.types[-1] == "done"


# --- registry ---------------------------------------------------------------------------


async def test_registry_persists_and_closes_runs_interrupted_by_a_restart(tmp_path):
    store = Store(tmp_path / "r.sqlite3")
    reg = RunRegistry(store)
    run = reg.create("a" * 32, "inc-1")
    await reg.emit(run, EventType.RUN_STARTED, {"incident_id": "inc-1"})
    await reg.emit(run, EventType.NARRATION, {"text": "hi"})
    assert [e.id for e in run.events] == [0, 1] and not run.finished

    reborn = RunRegistry(store).get(run.id)  # a new process
    assert reborn.types == ["run_started", "narration", "error", "done"]
    assert reborn.finished and [e.id for e in reborn.events] == [0, 1, 2, 3]
    again = RunRegistry(store).get(run.id)
    assert again.types == reborn.types  # closed once, on the record

    seen = [ev.id async for ev in RunRegistry(store).stream(again, after=1)]
    assert seen == [2, 3]


async def test_live_stream_delivers_events_as_they_land(tmp_path):
    reg = RunRegistry(Store(tmp_path / "r.sqlite3"))
    run = reg.create("b" * 32, "inc-2")

    async def consume():
        return [ev.type async for ev in reg.stream(run)]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await reg.emit(run, EventType.RUN_STARTED, {})
    await asyncio.sleep(0.01)
    await reg.emit(run, EventType.DONE, {"ms": 1})
    assert await asyncio.wait_for(task, 2) == ["run_started", "done"]
    with pytest.raises(RuntimeError):
        await reg.emit(run, EventType.NARRATION, {})


async def test_two_sessions_with_the_same_incident_get_distinct_rollback_branches(env):
    store, s, sessions, deps, made = env
    suffixes = []
    for _ in range(2):
        sess = await new_session(sessions, made)
        world = naming.session_world(sess.sid)
        f = await inject_fault(
            world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
        )
        run = deps.runs.create(sess.sid, f.incident_id)
        await execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=deps)
    opened = [c for c in deps.github.calls if c["op"] == "open_rollback_pr"]
    suffixes = [c.get("branch") for c in opened]
    assert len(opened) == 2 and len(set(suffixes)) == 2, suffixes


async def test_remediation_refuses_when_proofs_do_not_verify(env, monkeypatch):
    """Verify runs before any action: a falsified chain never opens a PR."""
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )

    async def falsified(sid, iid, store, **kw):
        from types import SimpleNamespace

        return SimpleNamespace(status="FALSIFIED", proofs=[], tampered_reasons=[])

    monkeypatch.setattr(remediation, "verify_incident", falsified)
    run = deps.runs.create(sess.sid, f.incident_id)
    await execute_run(run, f.incident_id, f.alert_time, f.alert_service_id, deps=deps)
    kinds = [d.get("kind") for d in run.of(EventType.ACTION)]
    assert "pr" not in kinds and "page" in kinds
    assert await st.get_state(naming.ledger(sess.sid), f.incident_id) == st.State.REFUSED


# --- late-arriving facts (snapshot payload) ---------------------------------------------


async def test_late_arriving_lists_only_incident_relevant_facts(env):
    """Seed-world changes recorded late but long before the incident are not listed; a
    late change in the window on the impact area is, a late one off the area is not, and a
    late DEPENDS_ON edge on the area is reported as an edge."""
    store, s, sessions, deps, made = env
    sess = await new_session(sessions, made)
    world = naming.session_world(sess.sid)
    f = await inject_fault(
        world, session_id=sess.sid, fault_class="bad_deploy", rng_seed=SEED, store=store
    )
    h = await build_snapshots(world, sess.sid, f.incident_id, f.alert_time)
    g, t = h.true_graph, f.alert_time

    async def q(cypher, params=None):
        return (await client.query(g, cypher, params)).result_set

    # The true snapshot does hold old changes recorded after t (the seed world's), yet
    # none of them is listed.
    old_late = await q(
        "MATCH (c:Change) WHERE c.recorded_at > $t AND c.valid_from < $t - 86400 RETURN count(c)",
        {"t": t},
    )
    assert old_late[0][0] > 0
    late, edges = await orchestrator.late_arriving(g, f.incident_id, t)
    assert late == [] and edges == []

    syms = await q(
        "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom)-[:ON]->(s:Service) "
        "RETURN s.id, y.onset ORDER BY y.onset",
        {"iid": f.incident_id},
    )
    sym_ids = sorted({r[0] for r in syms})
    area = {
        r[0]
        for r in await q(
            orchestrator.REVERSE_REACH.format(depth=orchestrator.MAX_DEPTH),
            {"symptomatic": sym_ids},
        )
    }
    off = (
        await q(
            "MATCH (s:Service) WHERE NOT s.id IN $area RETURN s.id ORDER BY s.id LIMIT 1",
            {"area": sorted(area)},
        )
    )[0][0]
    on = syms[0][0]
    vf = syms[0][1] - 600  # inside [earliest onset - 120 min, t]
    for cid, svc in (("chg-late-on", on), ("chg-late-off", off)):
        await q(
            "MATCH (s:Service {id: $svc}) CREATE (c:Change {id: $id, kind: 'config', "
            "summary: 'late', valid_from: $vf, recorded_at: $rec})-[:CHANGES]->(s)",
            {"svc": svc, "id": cid, "vf": vf, "rec": t + 300},
        )
    # an old change on the impact area, recorded late: outside the window, not listed
    await q(
        "MATCH (s:Service {id: $svc}) CREATE (c:Change {id: 'chg-late-old', kind: 'config', "
        "summary: 'old', valid_from: $vf, recorded_at: $rec})-[:CHANGES]->(s)",
        {"svc": sym_ids[-1], "vf": t - 3 * 86400, "rec": t + 300},
    )
    a, b = (
        await q(
            "MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) WHERE a.id IN $area AND b.id IN $area "
            "RETURN a.id, b.id ORDER BY a.id, b.id LIMIT 1",
            {"area": sorted(area)},
        )
    )[0]
    await q(
        "MATCH (a:Service {id: $a})-[d:DEPENDS_ON]->(b:Service {id: $b}) SET d.recorded_at = $rec",
        {"a": a, "b": b, "rec": t + 60},
    )
    late, edges = await orchestrator.late_arriving(g, f.incident_id, t)
    assert late == [on]
    assert edges == [{"caller": a, "callee": b}]
    payload = await orchestrator._snapshot_payload(h, f.incident_id, t)
    assert payload["late_arriving"] == [on] and payload["late_edges"] == edges
