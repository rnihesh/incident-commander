"""Storm collapse (U17, KTD13, AE6): storm injection and ``collapse_storm`` on real snapshots."""

from __future__ import annotations

import re
import time

import pytest

from ic.graph import client, naming
from ic.graph.snapshots import build_snapshots, create_session_world
from ic.store import Store
from ic.tools.registry import ScopeError, ToolContext, call_tool
from ic.tools.result import ToolError
from ic.world.faults import JOINT_CLASS, InjectedStorm, inject_storm


async def rows(g: str, q: str, params: dict | None = None) -> list[list]:
    return (await client.query(g, q, params, read_only=True)).result_set


async def drop(sid: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"s:{sid}:"):
            await db.select_graph(name).delete()


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> Store:
    s = Store(tmp_path_factory.mktemp("storm") / "ic.sqlite3")
    s.init()
    return s


class Storm:
    def __init__(self, sid: str, world: str, st: InjectedStorm, ctx: ToolContext, tracked: list):
        self.sid, self.world, self.st, self.ctx, self.tracked = sid, world, st, ctx, tracked

    async def collapse(self):
        return await call_tool("collapse_storm", {"storm_id": self.st.storm_id}, self.ctx)


async def make_storm(sid: str, store: Store, *, seed: int, faults: list[str], **kw) -> Storm:
    world = naming.session_world(sid)
    if world not in await client.get_db().list_graphs():
        await create_session_world(sid)
    st = await inject_storm(world, session_id=sid, rng_seed=seed, store=store, faults=faults, **kw)
    return await snapshot(sid, st)


async def snapshot(sid: str, st: InjectedStorm) -> Storm:
    world = naming.session_world(sid)
    h = await build_snapshots(world, sid, st.storm_id, st.ended_at)
    tracked: list[str] = []
    ctx = ToolContext(
        sid=sid,
        iid=st.storm_id,
        world_graph=world,
        snapshot_true=h.true_graph,
        snapshot_known=h.known_graph,
        track_graph=tracked.append,
    )
    return Storm(sid, world, st, ctx, tracked)


@pytest.fixture(scope="module")
async def ae6(module_sid, store) -> Storm:
    return await make_storm(module_sid, store, seed=7, faults=["bad_deploy", JOINT_CLASS])


def by_answer(result: dict, storm: Storm) -> dict[str, dict]:
    """fault class -> the incident whose symptomatic services are that fault's."""
    out = {}
    for a in storm.st.answers:
        [inc] = [i for i in result["incidents"] if sorted(i["symptomatic"]) == list(a.services)]
        out[a.fault_class] = inc
    return out


async def test_ae6_two_incidents_second_jointly_caused(ae6):
    """AE6: a bad deploy and a flag+config AND fault -> two incidents, the second marked
    jointly caused by both changes."""
    r = await ae6.collapse()
    res = r.result
    assert 200 <= res["pages_in"] <= 400 and res["pages_in"] == ae6.st.pages
    assert res["collapsed_to"] == 2
    assert sum(i["alerts"] for i in res["incidents"]) == res["pages_in"]
    answers = {a.fault_class: a for a in ae6.st.answers}
    inc = by_answer(res, ae6)

    deploy = inc["bad_deploy"]
    assert deploy["explaining"] == list(answers["bad_deploy"].root_ids)
    assert deploy["jointly_caused"] is False and deploy["joint"] is None
    assert deploy["unexplained"] == []

    joint = inc[JOINT_CLASS]
    assert sorted(joint["explaining"]) == sorted(answers[JOINT_CLASS].root_ids)
    assert joint["jointly_caused"] is True and joint["unexplained"] == []
    j = joint["joint"]
    assert {j["flag_change"], j["config_change"]} == set(answers[JOINT_CLASS].root_ids)
    gated = await rows(
        ae6.ctx.snapshot_known,
        "MATCH (:Flag {id: $f})-[:GATED_BY]->(c:Config {id: $c}) RETURN count(c)",
        {"f": j["flag"], "c": j["config"]},
    )
    assert gated == [[1]]
    for eid, solo in j["solo"].items():  # each alone ran symptom-free longer than the lead
        assert solo["seconds"] > solo["lead_s"] > 0, eid
        assert solo["to"] <= joint["first_page"]
    # Every incident carries a causal path from its explaining change(s) to its services.
    for i in res["incidents"]:
        assert {p["to"] for p in i["paths"]} == set(i["symptomatic"])
        assert all(p["path"][0] in i["explaining"] for p in i["paths"])


async def test_joint_components_alone_do_not_explain(ae6):
    """Each change of the AND fault passes the KTD23 predicate by itself, yet neither is
    sufficient alone: both were active alone, symptom-free, longer than the lead."""
    res = (await ae6.collapse()).result
    joint = by_answer(res, ae6)[JOINT_CLASS]
    roots = {a.fault_class: a for a in ae6.st.answers}[JOINT_CLASS].root_ids
    cands = {c["id"]: c for c in joint["candidates"]}
    for rid in roots:
        c = cands[rid]
        assert c["qualifies"] is True, c
        assert c["alone"] == {"sufficient": False}
        assert c["partner"] in roots and c["partner"] != rid
    # No single entity is offered as an alternative explanation of the joint incident.
    assert joint["alternatives"] == []
    # The flag's solo interval comes from its own history (flipped and flipped back
    # outside the alert window, while the config still had its old value).
    flag = next(c for c in cands.values() if c["id"] in roots and c["kind"] == "flag")
    assert flag["solo"]["to"] < joint["first_page"] - 120 * 60


async def test_flag_alone_prevents_false_joint(sid, store):
    """A flag flip that ran alone and symptom-free, followed by its gating config change
    (never active alone) shortly before the onset: one cause (the config change), not a
    joint one."""
    s = await make_storm(sid, store, seed=11, faults=[JOINT_CLASS])
    roots = s.st.answers[0].root_ids
    kinds = dict(
        await rows(
            s.world, "MATCH (c:Change) WHERE c.id IN $ids RETURN c.id, c.kind", {"ids": roots}
        )
    )
    flag_id = next(i for i, k in kinds.items() if k == "flag")
    cfg_id = next(i for i, k in kinds.items() if k == "config")
    onset = (
        await rows(
            s.world,
            "MATCH (:Storm {id: $st})<-[:PART_OF]-(a:Alert) WHERE a.service_id IN $svcs "
            "RETURN min(a.fired_at)",
            {"st": s.st.storm_id, "svcs": list(s.st.answers[0].services)},
        )
    )[0][0]
    new_vf = onset - 120  # after the flip, never live without it, fresh value
    await client.query(
        s.world,
        "MATCH (c:Change {id: $id}) OPTIONAL MATCH (c)-[:PREVIOUS_VERSION]->(q:Change) "
        "SET c.valid_from = $vf, c.recorded_at = $vf + 30, c.value = '999999', "
        "q.valid_to = $vf",
        {"id": cfg_id, "vf": new_vf},
    )
    s = await snapshot(sid, s.st)
    res = (await s.collapse()).result
    assert res["collapsed_to"] == 1
    [inc] = res["incidents"]
    assert inc["explaining"] == [cfg_id]
    assert inc["jointly_caused"] is False and inc["joint"] is None
    cands = {c["id"]: c for c in inc["candidates"]}
    assert cands[flag_id]["alone"] == {"sufficient": False}  # shown harmless alone
    assert cands[cfg_id]["alone"] == {"sufficient": True}  # never ran alone
    assert cands[flag_id]["qualifies"] and cands[cfg_id]["qualifies"]


async def test_single_fault_300_alerts_is_one_incident(sid, store):
    s = await make_storm(sid, store, seed=5, faults=["config_change"], pages=320)
    res = (await s.collapse()).result
    assert res["pages_in"] >= 300
    assert res["collapsed_to"] == 1
    [inc] = res["incidents"]
    assert inc["alerts"] == res["pages_in"]
    assert inc["explaining"] == list(s.st.answers[0].root_ids)
    assert inc["jointly_caused"] is False


async def test_chokepoints_and_result_identical_across_runs(ae6):
    a, b = (await ae6.collapse()).result, (await ae6.collapse()).result
    assert a["chokepoints"] and a["chokepoints"] == b["chokepoints"]
    assert a["incidents"] == b["incidents"]
    assert a["betweenness"] == {"seed": 42, "samples": 512}
    area = {n["id"] for n in a["area"]["nodes"]}
    assert {c["service_id"] for c in a["chokepoints"]} <= area


async def test_storm_graph_tracked_and_deleted(ae6):
    scratch = naming.storm(ae6.sid, ae6.st.storm_id)
    r = await ae6.collapse()
    assert scratch in ae6.tracked
    assert scratch not in await client.get_db().list_graphs()
    # The WCC and betweenness queries ran on the scratch graph and are recorded with the rest.
    idx = r.result["graphs"]["storm_queries"]
    assert r.result["graphs"]["storm"] == scratch and r.result["wcc_query"] in idx
    assert any("algo.WCC" in r.cypher[i] for i in idx)
    assert any("algo.betweenness" in r.cypher[i] for i in idx)
    assert len(r.cypher) == len(r.params) and all(c.strip() for c in r.cypher)
    assert r.nodes_touched and not [n for n in r.nodes_touched if re.fullmatch(r"\d+", str(n))]
    assert r.ms > 0 and r.graph == ae6.ctx.snapshot_known
    # Also on failure.
    with pytest.raises(ToolError):
        await call_tool("collapse_storm", {"storm_id": "storm-999"}, ae6.ctx)
    assert naming.storm(ae6.sid, "storm-999") not in await client.get_db().list_graphs()


async def test_scope_needs_a_session(ae6):
    ctx = ToolContext(snapshot_known=ae6.ctx.snapshot_known, run="r1")
    with pytest.raises(ScopeError):
        await call_tool("collapse_storm", {"storm_id": ae6.st.storm_id}, ctx)


async def test_answer_key_never_reaches_the_graph_or_the_tool(ae6, store):
    w = ae6.world
    blob = repr(await rows(w, "MATCH (n) RETURN properties(n)")) + repr(
        await rows(w, "MATCH ()-[e]->() RETURN properties(e)")
    )
    res = await ae6.collapse()
    out = res.model_dump_json()
    for a in ae6.st.answers:
        for secret in (a.nonce, a.fault_id, a.key):
            assert secret not in blob and secret not in out
        row = store.get_answer(a.key, ae6.sid)
        assert row["root_id"] == "+".join(a.root_ids) and row["fault_class"] == a.fault_class
    assert "joint" not in blob and "bad_deploy" not in blob
    # Pages carry only service, time and signature; the storm only its time bounds.
    keys = await rows(w, "MATCH (a:Alert) RETURN DISTINCT keys(a)")
    assert {tuple(sorted(k[0])) for k in keys} == {("fired_at", "id", "service_id", "signature")}
    skeys = await rows(w, "MATCH (s:Storm) RETURN DISTINCT keys(s)")
    assert {tuple(sorted(k[0])) for k in skeys} == {("ended_at", "id", "started_at")}
    rels = await rows(w, "MATCH (:Alert)-[e]->() RETURN DISTINCT type(e) ORDER BY type(e)")
    assert rels == [["FOR"], ["PART_OF"]]
    roots = [r for a in ae6.st.answers for r in a.root_ids]
    caused = await rows(
        w, "MATCH ()-[e:CAUSED_BY]->(c) WHERE c.id IN $ids RETURN count(e)", {"ids": roots}
    )
    assert caused == [[0]]
    # Page ids follow fired_at order, so their numbering cannot point at a root either.
    pages = await rows(
        w,
        "MATCH (:Storm {id: $id})<-[:PART_OF]-(a:Alert) RETURN a.id, a.fired_at ORDER BY a.id",
        {"id": ae6.st.storm_id},
    )
    assert [p[1] for p in pages] == sorted(p[1] for p in pages)


async def test_same_seed_same_storm(store):
    outs = []
    for _ in range(2):
        s = naming.new_session_id()
        try:
            w = await create_session_world(s)
            st = await inject_storm(w, session_id=s, rng_seed=3, store=store)
            pages = await rows(
                w, "MATCH (a:Alert) RETURN a.id, a.service_id, a.fired_at ORDER BY a.id"
            )
            outs.append((st.storm_id, st.started_at, st.ended_at, pages))
        finally:
            await drop(s)
    assert outs[0] == outs[1]


@pytest.mark.slow
async def test_storm_collapse_under_ten_seconds(sid, store):
    s = await make_storm(
        sid, store, seed=21, faults=["bad_deploy", JOINT_CLASS, "flag_flip"], pages=380
    )
    t0 = time.perf_counter()
    r = await s.collapse()
    elapsed = time.perf_counter() - t0
    assert r.result["pages_in"] >= 300
    assert r.result["collapsed_to"] == 3
    assert elapsed < 10, f"collapse took {elapsed:.2f}s"
