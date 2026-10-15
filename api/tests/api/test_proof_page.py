"""Public proof page (KTD19, R18) and the PR auto-close pass (U21).

One resolved run on its own app: the proof page reads the ``pr:`` copies only, never names
the session, re-verifies against the copies (rate-limited and debounced) and refreshes the
commit status; the cleanup loop closes stale PRs and drops their copies in the same pass.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from api_helpers import cleanup_store_graphs, http, inject, sid_of, stream, test_settings

from ic.actions import pr_store as prs
from ic.actions.github import GitHubClient
from ic.api import routes
from ic.api.app import create_app
from ic.api.auth import COOKIE
from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.proof.verify import verify_incident
from ic.store import Store

GOOD_REF = "0123456789abcdef01234567"


@pytest.fixture(scope="module")
async def proof_app(tmp_path_factory):
    client.reset_client()
    tmp = tmp_path_factory.mktemp("proof")
    store = Store(tmp / "ic.sqlite3")
    settings = test_settings(tmp, proof_rerun_interval_s=0.0, proof_verifies_per_ip_hour=1000)
    app = create_app(settings, llm=None, store=store)
    state = routes.app_state(app)
    yield app, state
    await state.runs.wait_all(limit_s=60)
    await cleanup_store_graphs(store)
    client.reset_client()


@pytest.fixture(scope="module")
async def resolved(proof_app):
    app, state = proof_app
    async with http(app, "10.9.0.1") as c:
        r = await inject(c, "bad_deploy", 3)
        assert r.status_code == 200, r.text
        body = r.json()
        _, events = await stream(c, body["run_id"])
        assert events[-1]["type"] == "done"
        sid = sid_of(c)
    iid = body["incident_id"]
    ref = prs.proof_ref(state.store, sid, iid)
    assert ref is not None and prs.valid_proof_ref(ref), "the run did not open a PR"
    answer = state.store.get_answer(iid, sid)
    return {"sid": sid, "iid": iid, "ref": ref, "answer": answer, "events": events}


async def _add_qualifying_deploy(world: str, root_id: str, new_id: str) -> None:
    """AE5: a qualifying deploy upstream, recorded before t (flips a REFUTED proof)."""
    vf, svc = (
        await client.query(
            world,
            "MATCH (c:Change {id: $id})-[:CHANGES]->(s) RETURN c.valid_from, s.id",
            {"id": root_id},
        )
    ).result_set[0]
    await client.query(
        world,
        "MATCH (s:Service {id: $svc}) "
        "CREATE (c:Change {id: $new, kind: 'deploy', service_id: $svc, "
        "summary: 'Deploy v9.9.9: judge', valid_from: $vf, valid_to: $vt, recorded_at: $rec}) "
        "CREATE (c)-[:CHANGES]->(s)",
        {"svc": svc, "new": new_id, "vf": vf + 1, "vt": OPEN_END, "rec": vf + 10},
    )


def _no_secrets(text: str, ctx: dict) -> None:
    assert ctx["sid"] not in text
    assert ctx["answer"]["nonce"] not in text
    assert ctx["answer"]["fault_id"] not in text
    assert '"fault_class"' not in text and '"nonce"' not in text and '"sid"' not in text


# --- the page -----------------------------------------------------------------------------


async def test_proof_page_reads_the_pr_copy_and_names_no_session(proof_app, resolved):
    app, state = proof_app
    async with http(app, "10.9.1.1") as anon:  # no cookie: the page is public
        r = await anon.get(f"/api/proof/{resolved['ref']}")
        assert r.status_code == 200, r.text
        assert "set-cookie" not in r.headers
    _no_secrets(r.text, resolved)
    page = r.json()
    assert page["incident_id"] == resolved["iid"] and page["ref"] == resolved["ref"]
    assert page["verdict"] == "DIAGNOSED" and page["suspect"] == resolved["answer"]["root_id"]
    assert page["anchored"] is True and page["anchor_matches"] is True
    assert page["proofs"] and page["chain_head"] == page["proofs"][-1]["hash"]
    assert page["alert_service"] and page["title"]
    assert page["alert_time"].endswith("Z")  # an ISO string, not an epoch int
    for p in page["proofs"]:
        assert {"kind", "entity_id", "claim", "conclusion", "hash", "prev_hash", "cypher"} <= set(p)
        assert p["state"] in ("HOLDS", "REFUTED") and "graph" not in p
    assert page["rehearsal"]["result"] == "CLEARED"
    assert page["pr"]["url"].startswith("https://github.com/") and page["pr"]["simulated"]

    # It is the PR copy that is read: dropping the live session's graphs changes nothing.
    ledger_copy = naming.pr_ledger(resolved["sid"], resolved["iid"])
    assert await client.graph_exists(ledger_copy)


async def test_mcp_get_proof_returns_the_public_page(proof_app, resolved):
    from mcp import Client

    from ic.mcp_server import McpScope, build_server

    app, state = proof_app
    scope = McpScope(sid=None, store=state.store)  # the ref alone is the credential
    async with Client(build_server(lambda: scope)) as c:
        res = await c.call_tool("get_proof", {"ref": resolved["ref"]})
    assert not res.is_error, res.content[0].text
    got = json.loads(res.content[0].text)
    async with http(app, "10.9.1.7") as anon:
        want = (await anon.get(f"/api/proof/{resolved['ref']}")).json()
    assert got == want
    assert got["verdict"] == "DIAGNOSED" and got["proofs"]
    assert resolved["sid"] not in res.content[0].text
    assert resolved["answer"]["nonce"] not in res.content[0].text


@pytest.mark.parametrize(
    "ref",
    [GOOD_REF, "xyz", "0123456789ABCDEF01234567", "0123456789abcdef0123456", "..%2F..%2Fetc"],
)
async def test_unknown_or_malformed_ref_is_404(proof_app, ref):
    app, _ = proof_app
    async with http(app, "10.9.1.2") as c:
        assert (await c.get(f"/api/proof/{ref}")).status_code == 404
        assert (await c.post(f"/api/proof/{ref}/verify", json={})).status_code == 404


def test_proof_ref_format_check(tmp_path):
    assert prs.valid_proof_ref(GOOD_REF)
    assert not prs.valid_proof_ref(GOOD_REF.upper())
    assert not prs.valid_proof_ref(GOOD_REF + "0")
    store = Store(tmp_path / "ic.sqlite3")
    store.init()
    assert prs.resolve_proof_ref(store, "' OR 1=1 --") is None
    assert prs.resolve_proof_ref(store, GOOD_REF) is None
    for bad in (GOOD_REF, "' OR 1=1 --"):
        with pytest.raises(prs.ProofRefNotFound):  # a not-found, never a PR graph conflict
            prs.require_proof_ref(store, bad)


async def test_reverify_with_a_conflicting_snapshot_name_is_409(proof_app, resolved):
    """A verify snapshot name tracked for another session is a real conflict: 409, not 404."""
    app, state = proof_app
    sid, iid, ref = resolved["sid"], resolved["iid"], resolved["ref"]
    name = naming.pr_verify(sid, iid, naming.VIEWS[0])
    with state.store.conn() as conn:
        before = conn.execute("SELECT * FROM pr_graphs WHERE graph_name = ?", (name,)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO pr_graphs (graph_name, incident_id, sid, created_at, "
            "proof_ref) VALUES (?, ?, 'someone-else', ?, ?)",
            (name, iid, time.time(), GOOD_REF),
        )
    try:
        async with http(app, "10.9.1.3") as c:
            r = await c.post(f"/api/proof/{ref}/verify", json={})
        assert r.status_code == 409, r.text
        assert resolved["sid"] not in r.text
    finally:
        with state.store.conn() as conn:
            conn.execute("DELETE FROM pr_graphs WHERE graph_name = ?", (name,))
            if before is not None:
                marks = ", ".join("?" * len(before))
                conn.execute(f"INSERT INTO pr_graphs VALUES ({marks})", tuple(before))


# --- re-verify ----------------------------------------------------------------------------


async def test_reverify_runs_on_the_pr_copies_not_the_live_session(proof_app, resolved):
    app, state = proof_app
    sid, iid, ref = resolved["sid"], resolved["iid"], resolved["ref"]
    async with http(app, "10.9.2.1") as c:
        r = await c.post(f"/api/proof/{ref}/verify", json={})
        assert r.status_code == 200, r.text
        rep = r.json()
        _no_secrets(r.text, resolved)
        assert rep["status"] == "VERIFIED" and rep["chain_ok"] and rep["anchor_ok"] is True
        assert rep["cached"] is False and rep["proofs"]
        assert rep["commit_status"]["posted"] and rep["commit_status"]["simulated"]
        assert rep["commit_status"]["state"] == "success"
        for v in naming.VIEWS:  # the pr-scoped snapshots are gone afterwards
            assert not await client.graph_exists(naming.pr_verify(sid, iid, v))
            assert not await client.graph_exists(naming.verify(sid, iid, v))

        # Editing the live session world after the PR opened does not move the proof page.
        await _add_qualifying_deploy(
            naming.session_world(sid), resolved["answer"]["root_id"], "chg-live-1"
        )
        rep = (await c.post(f"/api/proof/{ref}/verify", json={})).json()
        assert rep["status"] == "VERIFIED", rep

        # Editing the PR's own world copy does.
        world = naming.pr_world(sid, iid)
        await _add_qualifying_deploy(world, resolved["answer"]["root_id"], "chg-pr-1")
        r = await c.post(f"/api/proof/{ref}/verify", json={})
        rep = r.json()
        _no_secrets(r.text, resolved)
        assert rep["status"] == "FALSIFIED" and rep["chain_ok"], rep
        assert any("chg-pr-1" in p["responsible"] for p in rep["proofs"])
        assert not any("chg-live-1" in p["responsible"] for p in rep["proofs"])
        assert rep["commit_status"]["state"] == "failure"
        await client.query(world, "MATCH (c:Change {id: 'chg-pr-1'}) DETACH DELETE c")

        page = (await c.get(f"/api/proof/{ref}")).json()
        assert page["last_verify"]["status"] == "FALSIFIED"


async def test_reverify_is_rate_limited_per_ip_and_debounced_per_ref(
    proof_app, resolved, monkeypatch
):
    app, state = proof_app
    ref = resolved["ref"]
    monkeypatch.setattr(state.limiter, "proof_verifies_per_ip_hour", 2)
    async with http(app, "10.9.3.1") as c:
        assert (await c.post(f"/api/proof/{ref}/verify", json={})).status_code == 200
        assert (await c.post(f"/api/proof/{ref}/verify", json={})).status_code == 200
        r = await c.post(f"/api/proof/{ref}/verify", json={})
        assert r.status_code == 429 and "address" in r.json()["error"]
        assert (await c.post(f"/api/proof/{ref}/verify")).status_code == 415  # JSON only

    monkeypatch.setattr(state.limiter, "proof_verifies_per_ip_hour", 1000)
    monkeypatch.setattr(state.settings, "proof_rerun_interval_s", 3600.0)
    async with http(app, "10.9.3.2") as c:
        # The previous re-run was moments ago: the cached report, no new Verify.
        r = await c.post(f"/api/proof/{ref}/verify", json={})
        assert r.status_code == 200 and r.json()["cached"] is True
        _no_secrets(r.text, resolved)
        state.proof_reports.pop(ref)
        r = await c.post(f"/api/proof/{ref}/verify", json={})
        assert r.status_code == 429 and r.json()["retry_after"] == 3600.0


async def test_reverify_posts_a_real_commit_status_on_a_real_pr(proof_app, resolved, monkeypatch):
    app, state = proof_app
    sid, iid, ref = resolved["sid"], resolved["iid"], resolved["ref"]
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"id": 1, "state": json.loads(req.content)["state"]})

    gh = GitHubClient("tok", "o/r", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(state, "github", gh)
    rec = prs.pr_record(state.store, sid, iid)
    prs.set_pr_url(state.store, sid, iid, "https://github.com/o/r/pull/7", "a" * 40, False)
    try:
        async with http(app, "10.9.4.1") as c:
            rep = (await c.post(f"/api/proof/{ref}/verify", json={})).json()
        assert rep["commit_status"] == {
            "posted": True,
            "state": "success",
            "context": "incident-commander/proofs",
            "simulated": False,
        }
        assert len(seen) == 1 and seen[0].url.path == f"/repos/o/r/statuses/{'a' * 40}"
        sent = json.loads(seen[0].content)
        assert sent["target_url"].endswith(f"/proof/{ref}") and sid not in seen[0].content.decode()
    finally:
        prs.set_pr_url(state.store, sid, iid, rec["pr_url"], rec["head_sha"], rec["simulated"])


# --- the PR's frozen anchor and sealed rehearsal ---------------------------------------------


async def test_rederive_is_refused_while_a_pr_is_open_and_its_proof_is_unchanged(
    proof_app, resolved
):
    """A re-derive would reset the chain and anchor and re-copy ``pr:{sid}:{iid}:*``: the
    open PR's public proof page would then vouch for a different chain."""
    app, state = proof_app
    sid, iid, ref = resolved["sid"], resolved["iid"], resolved["ref"]
    async with http(app, "10.9.6.1") as anon:
        before = (await anon.get(f"/api/proof/{ref}")).json()
    anchor = state.store.get_anchor(sid, iid)
    async with http(app, "10.9.6.2") as c:
        c.cookies.set(COOKIE, sid)
        r = await c.post(f"/api/judge/{iid}/rederive", json={})
        assert r.status_code == 409, r.text
        assert "PR" in r.json()["error"]
        assert state.store.get_anchor(sid, iid) == anchor  # nothing was reset
        assert not state.runs.active_for(sid, iid)
        after = (await c.get(f"/api/proof/{ref}")).json()
        for k in ("chain_head", "proofs", "verdict", "suspect", "rehearsal", "anchor_matches"):
            assert after[k] == before[k], k
        rep = (await c.post(f"/api/proof/{ref}/verify", json={})).json()
        assert rep["status"] == "VERIFIED" and rep["anchor_ok"] is True


async def test_pr_verify_uses_the_anchor_frozen_with_the_pr_copies(proof_app, resolved):
    app, state = proof_app
    sid, iid = resolved["sid"], resolved["iid"]
    live = state.store.get_anchor(sid, iid)
    frozen = prs.pr_anchor(state.store, sid, iid)
    assert frozen is not None and frozen["rehearsal_hash"]
    assert (frozen["head_hash"], frozen["t"], frozen["world"]) == (
        live["head_hash"],
        live["t"],
        live["world"],
    )
    # The session re-anchors (what a re-derived run does): the PR's proof does not move.
    state.store.put_anchor(
        session_id=sid, incident_id=iid, head_hash="0" * 64, t=live["t"], world=live["world"]
    )
    try:
        rep = await verify_incident(sid, iid, state.store, pr=True)
        assert rep.status == "VERIFIED" and rep.anchored_head == frozen["head_hash"]
        assert (await verify_incident(sid, iid, state.store)).status == "TAMPERED"
        async with http(app, "10.9.6.3") as anon:
            page = (await anon.get(f"/api/proof/{ref_of(state, sid, iid)}")).json()
        assert page["anchored"] is True and page["anchor_matches"] is True
    finally:
        state.store.put_anchor(
            session_id=sid,
            incident_id=iid,
            head_hash=live["head_hash"],
            t=live["t"],
            world=live["world"],
        )
    # ... and a forged frozen anchor is caught.
    with state.store.conn() as conn:
        conn.execute(
            "UPDATE pr_anchor SET head_hash = ? WHERE sid = ? AND incident_id = ?",
            ("1" * 64, sid, iid),
        )
    try:
        rep = await verify_incident(sid, iid, state.store, pr=True)
        assert rep.status == "TAMPERED" and rep.anchor_ok is False
    finally:
        with state.store.conn() as conn:
            conn.execute(
                "UPDATE pr_anchor SET head_hash = ? WHERE sid = ? AND incident_id = ?",
                (frozen["head_hash"], sid, iid),
            )


def ref_of(state, sid: str, iid: str) -> str:
    ref = prs.proof_ref(state.store, sid, iid)
    assert ref is not None
    return ref


REHEARSAL_DETAIL_Q = "MATCH (r:Rehearsal)-[:FOR]->(:LedgerIncident {id: $iid}) RETURN r.detail"
SET_REHEARSAL_Q = (
    "MATCH (r:Rehearsal)-[:FOR]->(:LedgerIncident {id: $iid}) SET r.result = $res, r.detail = $d"
)


async def test_an_edited_rehearsal_record_is_tampered(proof_app, resolved):
    """The rehearsal shown on the proof page is sealed (content hash in the store) when it is
    written: a Browser edit of the PR ledger copy, or the session ledger, is TAMPERED."""
    app, state = proof_app
    sid, iid, ref = resolved["sid"], resolved["iid"], resolved["ref"]
    for lg, pr in ((naming.pr_ledger(sid, iid), True), (naming.ledger(sid), False)):
        [[detail]] = (await client.query(lg, REHEARSAL_DETAIL_Q, {"iid": iid})).result_set
        await client.query(lg, SET_REHEARSAL_Q, {"iid": iid, "res": "CLEARED", "d": "forged"})
        try:
            rep = await verify_incident(sid, iid, state.store, pr=pr)
            assert rep.status == "TAMPERED", rep.tampered_reasons
            assert any("rehearsal" in why for why in rep.tampered_reasons)
            if pr:
                async with http(app, "10.9.7.1") as anon:
                    page = (await anon.get(f"/api/proof/{ref}")).json()
                    assert page["rehearsal"]["status"] == "TAMPERED"
                    r = await anon.post(f"/api/proof/{ref}/verify", json={})
                    assert r.json()["status"] == "TAMPERED"
        finally:
            await client.query(lg, SET_REHEARSAL_Q, {"iid": iid, "res": "CLEARED", "d": detail})
        after = await verify_incident(sid, iid, state.store, pr=pr)
        # (the live session world carries an earlier test's edit: FALSIFIED, not TAMPERED)
        assert after.status == ("VERIFIED" if pr else "FALSIFIED"), after.tampered_reasons
    async with http(app, "10.9.7.2") as anon:
        page = (await anon.get(f"/api/proof/{ref}")).json()
    assert page["rehearsal"]["status"] == "SEALED" and page["rehearsal"]["result"] == "CLEARED"


# --- auto-close (run last: it deletes the PR copies) ---------------------------------------


class FakeGitHub:
    def __init__(self, repo: str = "o/r", numbers: list[int] | None = None, fail: bool = False):
        self.repo, self.mock, self.numbers, self.fail = repo, False, numbers or [], fail
        self.calls: list[float] = []

    async def close_stale_prs(self, older_than_s: float = 3600, *, now: float | None = None):
        self.calls.append(older_than_s)
        if self.fail:
            raise RuntimeError("GitHub is down")
        return list(self.numbers)


async def test_auto_close_pass_is_throttled_and_never_raises(
    proof_app, resolved, monkeypatch, caplog
):
    app, state = proof_app
    failing = FakeGitHub(fail=True)
    monkeypatch.setattr(state, "github", failing)
    monkeypatch.setattr(state, "last_pr_close", 0.0)
    with caplog.at_level(logging.ERROR):
        out = await routes.maintenance_once(state)
    assert failing.calls == [3600] and out["prs_closed"] == []
    assert "closing stale rollback PRs failed" in caplog.text
    assert out["pr_graphs"] == []  # fresh copies of an open PR stay
    assert await client.graph_exists(naming.pr_world(resolved["sid"], resolved["iid"]))

    # Not due again for 10 minutes.
    again = await routes.maintenance_once(state, now=state.last_pr_close + 60)
    assert failing.calls == [3600] and again["pr_graphs"] == []

    # Mock mode (no token): a no-op, no network.
    monkeypatch.setattr(state, "github", None)
    assert await routes.close_stale_prs_once(state, now=state.last_pr_close + 601) == set()


async def test_auto_close_deletes_the_closed_prs_copies(proof_app, resolved, monkeypatch):
    app, state = proof_app
    sid, iid, ref = resolved["sid"], resolved["iid"], resolved["ref"]
    prs.set_pr_url(state.store, sid, iid, "https://github.com/o/r/pull/4242")
    other = "f" * 32  # an unrelated, fresh PR group stays
    prs.track_pr_graph(state.store, naming.pr_world(other, "inc-x"), "inc-x", other)
    prs.set_pr_url(state.store, other, "inc-x", "https://github.com/o/r/pull/9")

    gh = FakeGitHub(numbers=[4242])
    monkeypatch.setattr(state, "github", gh)
    monkeypatch.setattr(state, "last_pr_close", 0.0)
    out = await routes.maintenance_once(state)
    assert out["prs_closed"] == ["https://github.com/o/r/pull/4242"]
    names = set(out["pr_graphs"])
    assert {naming.pr_world(sid, iid), naming.pr_ledger(sid, iid)} <= names
    assert {naming.pr_verify(sid, iid, v) for v in naming.VIEWS} <= names
    assert naming.pr_world(other, "inc-x") not in names
    assert not await client.graph_exists(naming.pr_world(sid, iid))
    assert not await client.graph_exists(naming.pr_ledger(sid, iid))
    assert prs.resolve_proof_ref(state.store, ref) is None and ref not in state.proof_reports
    async with http(app, "10.9.5.1") as c:
        assert (await c.get(f"/api/proof/{ref}")).status_code == 404

    # Past the TTL, a group goes even when GitHub closed nothing (mock mode, or a real PR
    # closed by hand): copies and PRs share the one-hour window.
    with state.store.conn() as conn:
        conn.execute(
            "UPDATE pr_graphs SET created_at = ? WHERE sid = ?", (time.time() - 3601, other)
        )
    monkeypatch.setattr(state, "github", FakeGitHub())
    monkeypatch.setattr(state, "last_pr_close", 0.0)
    out = await routes.maintenance_once(state)
    assert out["pr_graphs"] == [naming.pr_world(other, "inc-x")]
    assert prs.tracked_pr_graphs(state.store) == []
