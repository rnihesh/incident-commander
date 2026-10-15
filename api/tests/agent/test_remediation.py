"""Remediation agent (U12): ledger-only handoff, cloned-world rehearsal, refusal (AE4).

Every test runs the deterministic (no-LLM) triage on a fresh session, then remediation.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import networkx as nx
import pytest
from triage_world import Clone, rows

from ic.actions import pr_store
from ic.actions.errors import ActionError
from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.actions.text import inert
from ic.agent import remediation
from ic.agent.loop import EventLog
from ic.agent.triage import run_triage
from ic.api.events import EventType
from ic.config import Settings
from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.graph.snapshots import build_snapshots, create_session_world
from ic.incidents import state as st
from ic.memory import writeback as wb
from ic.proof import ledger
from ic.proof import verify as verify_mod
from ic.store import Store
from ic.tools import rehearse
from ic.tools.registry import ToolContext
from ic.world import impact_reader as R
from ic.world.causality import necessity, qualifies
from ic.world.faults import inject_fault

E_ID = "chg-99002"  # the second fault's change in the AE4 world


def settings(store: Store) -> Settings:
    return Settings(
        store_path=str(store.path),
        github_token=None,
        github_repo=None,
        discord_webhook_url=None,
        llm_api_key=None,
        actions_per_hour=1000,
    )


async def triage(ctx: ToolContext, store: Store):
    out = await run_triage(ctx, ctx.iid, llm=None, emit=None, store=store)
    assert out.verdict == "DIAGNOSED", out
    return out


async def remediate(sid, iid, t, alert, store, **kw):
    log = EventLog("rem")
    gh, pg = GitHubClient(None, None, mock=True), DiscordPager(None, mock=True)
    res = await remediation.run_remediation(
        sid=sid,
        iid=iid,
        t=t,
        alert_service_id=alert,
        title="Checkout latency",
        emit=log,
        store=store,
        settings=settings(store),
        github=gh,
        pager=pg,
        **kw,
    )
    return res, log, gh, pg


async def rehearsal_nodes(sid: str, iid: str) -> list[list]:
    return await rows(
        naming.ledger(sid),
        "MATCH (r:Rehearsal)-[:FOR]->(:LedgerIncident {id: $iid}) "
        "RETURN r.id, r.result, r.change_id, r.still_explains, r.checked",
        {"iid": iid},
    )


def states(log: EventLog) -> list[str]:
    return [d["state"] for d in log.of(EventType.STATE)]


# --- the correct rollback ---------------------------------------------------------------


async def test_correct_rollback_rehearses_clear_and_resolves(base, cloner, store):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    tracked: list[str] = []
    res, log, gh, pg = await remediate(
        c.sid,
        base.iid,
        base.t,
        base.alert_service,
        store,
        snapshot_ctx=c.ctx,
        track_graph=tracked.append,
    )
    assert res.claimed and res.final_state == "RESOLVED" and res.pr is not None
    assert states(log) == ["REHEARSING", "ACTING", "PR_OPEN", "RESOLVED"]
    assert await st.get_state(naming.ledger(c.sid), base.iid) == st.State.RESOLVED

    types = log.types
    assert types.index("handoff") < types.index("tool_call") < types.index("rehearsal")
    assert types.index("rehearsal") < types.index("action")
    [call] = log.of(EventType.TOOL_CALL)
    assert call["tool"] == "rehearse_rollback" and call["graph"] == c.ctx.snapshot_true
    [reh] = log.of(EventType.REHEARSAL)
    assert reh["result"] == "CLEARED" and reh["still_explains"] == []
    assert reh["change"] == base.root and reh["checked"] > 0 and reh["clone_ms"] > 0
    assert reh["record"] and "surviving" not in reh

    clone = naming.rehearse(c.sid, base.iid)
    verify_graphs = [naming.verify(c.sid, base.iid, v) for v in naming.VIEWS]
    assert tracked == [*verify_graphs, clone] and not await client.graph_exists(clone)
    [node] = await rehearsal_nodes(c.sid, base.iid)
    assert node[1:3] == ["CLEARED", base.root] and json.loads(node[3]) == []
    assert node[0] == reh["record"]

    [pr] = [x for x in gh.calls if x["op"] == "open_rollback_pr"]
    assert "Rollback rehearsal" in pr["body"] and "CLEARED" in pr["body"]
    assert [a["kind"] for a in log.of(EventType.ACTION)] == ["pr", "commit_status", "page"]
    assert len(pg.calls) == 1


async def test_remediation_completes_from_the_ledger_graph_alone(base, cloner, store):
    """Triage's process state is gone: new store object, new client, no locks, no ctx."""
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    # Per-key locks are dropped once released: nothing of this run stays behind.
    assert (naming.ledger(c.sid), base.iid) not in ledger._locks
    assert naming.rehearse(c.sid, base.iid) not in rehearse._locks
    client.reset_client()
    fresh = Store(store.path)
    res, log, _, _ = await remediate(c.sid, base.iid, base.t, base.alert_service, fresh)
    assert res.final_state == "RESOLVED" and res.pr is not None
    [handoff] = log.of(EventType.HANDOFF)
    assert handoff["suspect"] == base.root and handoff["verdict"] == "DIAGNOSED"
    assert handoff["ledger_state"] == "REHEARSING"  # read after the claim
    assert handoff["channel"] == "ledger graph"
    head = await ledger.head_hash(c.sid, base.iid)
    assert handoff["chain_head"] == head and len(handoff["proofs"]) >= 3
    assert handoff["hypotheses"] >= len(handoff["proofs"])
    assert {r["change"] for r in handoff["ruled_out"]} and base.root not in {
        r["change"] for r in handoff["ruled_out"]
    }
    assert log.of(EventType.REHEARSAL)[0]["result"] == "CLEARED"


async def test_a_failing_rehearsal_refuses_and_still_deletes_the_clone(
    base, cloner, store, monkeypatch
):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)

    async def boom(log, label, entity_id):
        raise RuntimeError("clone write failed")

    monkeypatch.setattr(rehearse, "_rollback", boom)
    res, log, gh, pg = await remediate(
        c.sid, base.iid, base.t, base.alert_service, store, snapshot_ctx=c.ctx
    )
    assert res.final_state == "REFUSED" and res.pr is None
    [reh] = log.of(EventType.REHEARSAL)
    assert reh["result"] == "REFUSED" and "clone write failed" in reh["detail"]
    assert not await client.graph_exists(naming.rehearse(c.sid, base.iid))
    assert not [x for x in gh.calls if x["op"] == "open_rollback_pr"]
    assert len(pg.calls) == 1
    assert (await rehearsal_nodes(c.sid, base.iid))[0][1] == "REFUSED"


# --- AE4: a second, partially overlapping fault -------------------------------------------


async def _ae4_world(store: Store):
    """A bad_deploy world plus a second fault E on a direct caller B of the root service:
    an extra symptom on B at the earliest onset (B now fails together with the root's
    service) and a deploy on B before it. The root still qualifies and is necessary; E
    fails coverage on the full set but still explains B's branch after the rollback."""
    for seed in range(3, 15):
        sid = naming.new_session_id()
        world = await create_session_world(sid)
        f = await inject_fault(
            world, session_id=sid, fault_class="bad_deploy", rng_seed=seed, store=store
        )
        h = await build_snapshots(world, sid, f.incident_id, f.alert_time)
        root = f.answer.root_id
        g = await R.read_impact_graph(h.true_graph)
        syms = await R.read_symptoms(h.true_graph, f.incident_id)
        t0 = min(s.onset for s in syms)
        ents = await R.read_window_entities(h.true_graph, t0)
        [r] = [e for e in ents if e.id == root]
        sig = next(s.signature for s in syms if s.service_id == r.service_id)
        for b in sorted({s.service_id for s in syms if g.has_edge(r.service_id, s.service_id)}):
            e = SimpleNamespace(
                id=E_ID, service_id=b, event_time=r.event_time + 15, sig_kind="deploy"
            )
            new = [*syms, R.SymptomRow(b, t0, sig)]
            ge = g.copy()
            ge.add_edge(E_ID, b)
            explained = {s.service_id for s in new}
            g_rb = ge.copy()
            g_rb.remove_node(root)
            if (
                qualifies(r, ge, new)[0]
                and not qualifies(e, ge, new)[0]
                and necessity(r, [*ents, e], ge, new)[0]
                and rehearse.qualifies_subset(e, g_rb, new, explained, t0)[0]
            ):
                await client.query(
                    world,
                    "MATCH (i:Incident {id: $iid}), (s:Service {id: $b}) "
                    "CREATE (c:Change {id: $cid, kind: 'deploy', service_id: $b, "
                    "summary: 'Deploy v7.7.7: cache warmer', valid_from: $vf, valid_to: $vt, "
                    "recorded_at: $rec, value: 'v7.7.7', prev_value: null})-[:CHANGES]->(s) "
                    "CREATE (i)-[:HAS]->(y:Symptom {id: 'sym-99002', service_id: $b, "
                    "onset: $t0, signature: $sig})-[:ON]->(s)",
                    {
                        "iid": f.incident_id,
                        "b": b,
                        "cid": E_ID,
                        "vf": e.event_time,
                        "vt": OPEN_END,
                        "rec": e.event_time + 5,
                        "t0": t0,
                        "sig": sig,
                    },
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
                return sid, f, b, ctx
        await _drop(sid)
    raise AssertionError("no seed gives a partially overlapping second fault")


async def _drop(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


@pytest.fixture
async def ae4(store):
    client.reset_client()
    sid, f, b, ctx = await _ae4_world(store)
    yield sid, f, b, ctx
    await _drop(sid)


async def test_partially_overlapping_second_fault_is_refused_with_no_pr(ae4, store):
    sid, f, b, ctx = ae4
    out = await triage(ctx, store)
    assert out.suspect_id == f.answer.root_id  # triage still diagnoses the first fault

    res, log, gh, pg = await remediate(sid, f.incident_id, f.alert_time, f.alert_service_id, store)
    assert res.claimed and res.final_state == "REFUSED" and res.pr is None
    assert states(log) == ["REHEARSING", "REFUSED"]
    assert await st.get_state(naming.ledger(sid), f.incident_id) == st.State.REFUSED

    [reh] = log.of(EventType.REHEARSAL)
    assert reh["result"] == "REFUSED"
    survivors = {s["entity_id"]: s for s in reh["still_explains"]}
    assert E_ID in survivors
    s = survivors[E_ID]
    assert s["full"] is False and b in s["services"]
    assert s["path"][:2] == [E_ID, b]
    assert reh["surviving"]["change"] in survivors and reh["surviving"]["path"]

    # no PR, no PR copies; the on-call is paged with the surviving explanation and path
    assert not [x for x in gh.calls if x["op"] == "open_rollback_pr"]
    assert not await client.graph_exists(naming.pr_world(sid, f.incident_id))
    assert [a["kind"] for a in log.of(EventType.ACTION)] == ["page"]
    [page] = pg.calls
    fields = {f["name"]: f["value"] for f in page["payload"]["embeds"][0]["fields"]}
    assert fields["Root cause"].startswith("REFUSED: Rollback of " + inert(f.answer.root_id, 99))
    assert inert(" > ".join(s["path"]), 999) in fields["Surviving paths"]
    [action] = log.of(EventType.ACTION)
    assert any(E_ID in x for x in action["surviving"])

    # the refusal is on the record; the clone is gone
    [node] = await rehearsal_nodes(sid, f.incident_id)
    assert node[1] == "REFUSED" and E_ID in {x["entity_id"] for x in json.loads(node[3])}
    assert not await client.graph_exists(naming.rehearse(sid, f.incident_id))


def test_the_ae4_shape_is_one_the_generator_never_produces_for_decoys():
    """The independence clause in miniature: a decoy on a caller that failed after the
    root's service is an echo; the same change with a tied onset is a second fault."""
    g = nx.DiGraph([("root", "a"), ("a", "b"), ("b", "c"), ("d", "b")])
    sym = lambda s, o: R.SymptomRow(s, o, "latency")  # noqa: E731
    d = SimpleNamespace(id="d", service_id="b", event_time=0, sig_kind="deploy")
    g.remove_node("root")
    echo = [sym("a", 100), sym("b", 200), sym("c", 300)]
    assert rehearse.qualifies_subset(d, g, echo, {"a", "b", "c"}, 100)[1] == "independent"
    tied = [sym("a", 100), sym("b", 100), sym("c", 300)]
    assert rehearse.qualifies_subset(d, g, tied, {"a", "b", "c"}, 100) == (True, None, ["b", "c"])


# --- Verify before acting (KTD4/KTD19) ----------------------------------------------------


def _fake_report(status: str):
    async def fake(sid, iid, store, **kw):
        if status == "ERROR":
            raise RuntimeError("snapshot build failed")
        return SimpleNamespace(status=status, proofs=[], tampered_reasons=[])

    return fake


@pytest.mark.parametrize("status", ["TAMPERED", "FALSIFIED", "UNANCHORED", "ERROR"])
async def test_a_chain_that_does_not_reverify_never_opens_a_pr(
    base, cloner, store, monkeypatch, status
):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    monkeypatch.setattr(remediation, "verify_incident", _fake_report(status))
    res, log, gh, pg = await remediate(c.sid, base.iid, base.t, base.alert_service, store)
    assert res.claimed and res.final_state == "REFUSED" and res.pr is None
    assert res.proof_status == status
    assert states(log) == ["REHEARSING", "REFUSED"]
    assert not [x for x in gh.calls if x["op"] == "open_rollback_pr"]
    assert not log.of(EventType.REHEARSAL)  # refused before the rehearsal
    assert [a["kind"] for a in log.of(EventType.ACTION)] == ["page"] and len(pg.calls) == 1
    assert not await client.graph_exists(naming.pr_world(c.sid, base.iid))


async def test_a_drifted_chain_still_remediates(base, cloner, store, monkeypatch):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    monkeypatch.setattr(remediation, "verify_incident", _fake_report("DRIFTED"))
    res, log, gh, _ = await remediate(c.sid, base.iid, base.t, base.alert_service, store)
    assert res.final_state == "RESOLVED" and res.pr is not None
    assert res.proof_status == "DRIFTED" and res.commit_state == "success"


async def test_a_ledger_edited_after_diagnosis_is_refused_end_to_end(base, cloner, store):
    """No monkeypatching: a forged proof in the session ledger is caught by the real Verify."""
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    await client.query(
        naming.ledger(c.sid),
        "MATCH (p:Proof {incident_id: $iid, seq: 1}) SET p.result_hash = 'forged'",
        {"iid": base.iid},
    )
    res, log, gh, pg = await remediate(c.sid, base.iid, base.t, base.alert_service, store)
    assert res.final_state == "REFUSED" and res.proof_status == "TAMPERED" and res.pr is None
    assert not [x for x in gh.calls if x["op"] == "open_rollback_pr"]
    assert len(pg.calls) == 1


async def test_a_concurrent_user_verify_does_not_refuse_remediation(
    base, cloner, store, monkeypatch
):
    """The console's Verify and remediation's Verify-before-acting build and delete the same
    ``s:{sid}:verify:{iid}:*`` graphs: they serialize instead of deleting each other's."""
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    real_check = verify_mod._check

    async def slow_check(*a, **kw):
        await asyncio.sleep(0.2)  # widen the window in which the snapshots are in use
        return await real_check(*a, **kw)

    monkeypatch.setattr(verify_mod, "_check", slow_check)
    tracked: list[str] = []
    user, (res, log, gh, _) = await asyncio.gather(
        verify_mod.verify_incident(c.sid, base.iid, store),
        remediate(
            c.sid,
            base.iid,
            base.t,
            base.alert_service,
            store,
            snapshot_ctx=c.ctx,
            track_graph=tracked.append,
        ),
    )
    assert user.status == "VERIFIED", user.tampered_reasons
    assert res.proof_status == "VERIFIED", res.errors
    assert res.final_state == "RESOLVED" and res.pr is not None
    # remediation's Verify snapshots are tracked for the session cleanup, then deleted
    verify_graphs = {naming.verify(c.sid, base.iid, v) for v in naming.VIEWS}
    assert verify_graphs <= set(tracked)
    for g in verify_graphs:
        assert not await client.graph_exists(g)


# --- one PR per incident ------------------------------------------------------------------


async def test_a_second_remediation_never_overwrites_an_open_prs_copies(base, cloner, store):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    res, _, _, _ = await remediate(c.sid, base.iid, base.t, base.alert_service, store)
    assert res.final_state == "RESOLVED" and res.pr is not None
    frozen = pr_store.pr_anchor(store, c.sid, base.iid)
    assert (
        frozen is not None and frozen["head_hash"] == store.get_anchor(c.sid, base.iid)["head_hash"]
    )
    assert frozen["rehearsal_hash"]
    pr_ledger = naming.pr_ledger(c.sid, base.iid)
    before = await rows(pr_ledger, "MATCH (n) RETURN count(n)")

    # The incident is DIAGNOSED again (as after a re-derive): remediation refuses up front.
    await client.query(
        naming.ledger(c.sid),
        "MATCH (li:LedgerIncident {id: $iid}) SET li.state = 'DIAGNOSED'",
        {"iid": base.iid},
    )
    res2, log2, gh2, pg2 = await remediate(c.sid, base.iid, base.t, base.alert_service, store)
    assert res2.final_state == "REFUSED" and res2.pr is None
    assert not [x for x in gh2.calls if x["op"] == "open_rollback_pr"]
    [page] = pg2.calls
    assert "a PR is already open for this incident" in json.dumps(page["payload"])
    assert await rows(pr_ledger, "MATCH (n) RETURN count(n)") == before
    assert pr_store.pr_anchor(store, c.sid, base.iid) == frozen
    for name in pr_store.tracked_pr_graphs(store):
        if name.startswith(naming.pr_prefix(c.sid, base.iid)):
            await client.delete_graph(name)


# --- outbound failures ----------------------------------------------------------------------


class FailingGitHub(GitHubClient):
    """Opens no PR: GitHub answers 502 (retryable) to the PR request."""

    def __init__(self):
        super().__init__(None, None, mock=True)

    async def open_rollback_pr(self, *a, **kw):
        raise ActionError("GitHub returned 502 Bad Gateway", retryable=True)


async def test_a_github_failure_still_pages_the_on_call(base, cloner, store):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    log = EventLog("rem")
    pg = DiscordPager(None, mock=True)
    res = await remediation.run_remediation(
        sid=c.sid,
        iid=base.iid,
        t=base.t,
        alert_service_id=base.alert_service,
        title="Checkout latency",
        emit=log,
        store=store,
        settings=settings(store),
        github=FailingGitHub(),
        pager=pg,
    )
    assert res.pr is None and res.final_state == "ACTING"
    assert await st.get_state(naming.ledger(c.sid), base.iid) == st.State.ACTING
    [err] = log.of(EventType.ERROR)
    assert err["code"] == "action_failed" and err["action"] == "pr" and err["retryable"] is True
    # The page still goes out, and says the PR failed.
    [page] = pg.calls
    assert "502" in json.dumps(page["payload"])
    kinds = [(a["kind"], a["status"]) for a in log.of(EventType.ACTION)]
    assert kinds == [("pr", "failed"), ("page", "ok")]


# --- memory write-back on a judge reveal (KTD8) ---------------------------------------------


async def test_a_reveal_mid_remediation_teaches_nothing_if_the_rehearsal_refuses(
    base, cloner, store
):
    c: Clone = await cloner(base)
    await triage(c.ctx, store)
    lg = naming.ledger(c.sid)
    assert await st.claim(lg, base.iid, st.State.DIAGNOSED, st.State.REHEARSING)
    res = await wb.write_back_after_reveal(
        sid=c.sid, iid=base.iid, match=True, resolved_at=base.t + 60, run_finished=False
    )
    assert res is None  # deferred to the run's own RESOLVED write-back
    await st.transition(lg, base.iid, st.State.REFUSED)  # the rehearsal refused
    world = naming.session_world(c.sid)
    left = await rows(
        world,
        "MATCH (i:Incident {id: $iid}) RETURN i.memory_written, i.status, "
        "size([(i)-[:CAUSED_BY]->() | 1]), size([(:Postmortem)-[:ABOUT]->(i) | 1])",
        {"iid": base.iid},
    )
    assert left == [[None, "open", 0, 0]]  # as injected: not resolved, nothing learned
    # ... and nothing later teaches it either: REFUSED is a blocked state.
    assert (
        await wb.write_back_after_reveal(
            sid=c.sid, iid=base.iid, match=True, resolved_at=base.t + 60, run_finished=True
        )
        is None
    )
