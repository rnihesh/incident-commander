"""find_precedents on a generated world (n_services=40) snapshotted after several incidents."""

import math

import pytest
from fixture_world import write_impacts_only

from ic.config import get_settings
from ic.graph import naming
from ic.graph.client import query
from ic.graph.snapshots import build_snapshots
from ic.tools import precedent as PR
from ic.tools.fingerprint import MATCH_THRESHOLD
from ic.tools.registry import ToolContext, all_tools, call_tool
from ic.tools.result import QueryLog, ToolError, ToolResult
from ic.world.embed import HashingEmbedder
from ic.world.generator import generate_world
from ic.world.loader import load_world

N_PAST = 9


@pytest.fixture(autouse=True)
def hashing_embedder(monkeypatch):
    """Never reach for the network: the loader and the tool share one local embedder."""
    emb = HashingEmbedder(get_settings().embedding_dims)
    monkeypatch.setattr(PR, "get_embedder", lambda: emb)
    return emb


@pytest.fixture(scope="module")
async def fx(module_sid):
    world = generate_world(11, 40)
    wg = naming.session_world(module_sid)
    await load_world(world, wg, HashingEmbedder(get_settings().embedding_dims))
    t = world.incidents[N_PAST].opened_at - 1  # incidents 0..N_PAST-1 are in the past
    h = await build_snapshots(wg, module_sid, "fx", t)
    log = QueryLog(h.true_graph)
    past = [i.id for i in world.incidents[:N_PAST]]
    # P: a past incident with at least two symptomatic services and some spread
    chosen = None
    for iid in past:
        syms = await _symptoms(log, iid)
        if len({s["service_id"] for s in syms}) < 2:
            continue
        pairs = PR._root_pairs(syms, None)
        paths = await PR._paths(log, pairs)
        fp, _ = PR.shape_fingerprints(syms, None, paths)
        if fp.max_hop >= 1:
            chosen = iid, syms, fp
            break
    assert chosen, "generated world has no multi-hop incident among the first few"
    return {"sid": module_sid, "graph": h.true_graph, "h": h, "past": past, "p": chosen}


async def _symptoms(log, iid):
    cols = ("service_id", "name", "tier", "signature", "onset")
    return [
        dict(zip(cols, r, strict=True)) for r in await log.rows(PR.SYMPTOMS, {"incident_id": iid})
    ]


def ctx_for(fx, graph=None) -> ToolContext:
    g = graph or fx["graph"]
    return ToolContext(
        sid=fx["sid"], iid="fx", world_graph=g, snapshot_true=g, snapshot_known=g, view="true"
    )


async def precedents(fx, graph=None, **args) -> ToolResult:
    return await call_tool("find_precedents", {"k": 50, **args}, ctx_for(fx, graph))


async def add_lookalike(fx, name, symptoms, *, signatures=None, service_names=None, cause=None):
    """Write a NEW incident on fresh services whose symptoms have the shape of ``symptoms``.

    A chain of new services with the tiers of P's deepest chain, root first; the remaining
    symptomatic services hang off the root. No name or id is shared with P.
    """
    g = fx["graph"]
    _, p_syms, p_fp = fx["p"]
    chain_tiers = list(p_fp.tier_seq)
    n = len({s["service_id"] for s in p_syms})
    chain = [f"{name}-c{i}" for i in range(len(chain_tiers))]
    leaves = [f"{name}-l{i}" for i in range(max(n - len(chain), 0))]
    for i, sid in enumerate([*chain, *leaves]):
        tier = chain_tiers[i] if i < len(chain) else chain_tiers[0]
        label = (service_names or {}).get(sid, f"{name}svc{i}")
        await query(
            g, "CREATE (:Service {id: $id, name: $n, tier: $t})", {"id": sid, "n": label, "t": tier}
        )
    edges = [*zip(chain, chain[1:], strict=False), *((chain[0], lf) for lf in leaves)]
    for a, b in edges:
        await query(
            g,
            "MATCH (a:Service {id: $a}), (b:Service {id: $b}) CREATE (a)-[:IMPACTS]->(b)",
            {"a": a, "b": b},
        )
    nodes = [chain[0], chain[-1], *chain[1:-1], *leaves][:n]
    sigs = signatures or [s["signature"] for s in sorted(p_syms, key=lambda s: s["onset"])]
    base = fx["h"].t - 10_000
    await query(
        g, "CREATE (:Incident {id: $id, title: 'new', opened_at: $t})", {"id": name, "t": fx["h"].t}
    )
    for i, (svc, sig) in enumerate(zip(nodes, sigs, strict=True)):
        await query(
            g,
            "MATCH (i:Incident {id: $i}), (s:Service {id: $s}) "
            "CREATE (i)-[:HAS]->(:Symptom {id: $sym, service_id: $s, onset: $o, "
            "signature: $sig})-[:ON]->(s)",
            {"i": name, "s": svc, "sym": f"{name}-sym{i}", "o": base + i * 60, "sig": sig},
        )
    if cause:
        label, cid, kind = cause
        await query(
            g,
            f"MATCH (s:Service {{id: $s}}) CREATE (:{label} {{id: $id, kind: $k}})-[:IMPACTS]->(s)",
            {"s": chain[0], "id": cid, "k": kind},
        )
    return chain


async def test_shape_found_on_different_services_with_different_text(fx):
    p_id, p_syms, _ = fx["p"]
    await add_lookalike(fx, "inc-new", p_syms)
    r = await precedents(fx, incident_id="inc-new")
    res = r.result
    assert res["advisory"] is True
    assert "inc-new" not in {p["incident_id"] for p in res["precedents"]}
    assert p_id in res["by_method"]["fingerprint"]
    entry = next(p for p in res["precedents"] if p["incident_id"] == p_id)
    assert entry["methods"]["fingerprint"] == 1.0
    assert entry["postmortem_id"].startswith("pm-") and entry["title"]
    assert entry["remediation"] and entry["resolved_minutes"] > 0
    assert set(entry["methods"]) == {"vector", "fulltext", "fingerprint"}
    # the text shares nothing with P: no service name in common
    p_names = {s["name"] for s in p_syms}
    assert not p_names & set(res["query"]["text"].replace(";", " ").replace(",", " ").split())


async def test_different_signatures_or_cause_kind_do_not_match(fx):
    p_id, p_syms, _ = fx["p"]
    p_sigs = {s["signature"] for s in p_syms}
    other = next(s for s in ("saturation", "tls_error", "timeout", "latency") if s not in p_sigs)
    await add_lookalike(
        fx, "inc-sig", p_syms, signatures=[other] * len({s["service_id"] for s in p_syms})
    )
    r = await precedents(fx, incident_id="inc-sig")
    assert p_id not in r.result["by_method"]["fingerprint"]

    # same signatures, same path length, but a different kind of cause
    _, p_cause_kind = await _cause(fx, p_id)
    wrong = "cert" if p_cause_kind != "cert" else "deploy"
    wrong_label = "Expiry" if wrong == "cert" else "Change"
    await add_lookalike(fx, "inc-kind", p_syms, cause=(wrong_label, "cause-wrong", wrong))
    r = await precedents(fx, incident_id="inc-kind", suspect_id="cause-wrong")
    assert p_id not in r.result["by_method"]["fingerprint"]

    right_label = "Expiry" if p_cause_kind in ("cert", "token", "quota") else "Change"
    await add_lookalike(fx, "inc-right", p_syms, cause=(right_label, "cause-right", p_cause_kind))
    r = await precedents(fx, incident_id="inc-right", suspect_id="cause-right")
    entry = next(p for p in r.result["precedents"] if p["incident_id"] == p_id)
    assert entry["methods"]["fingerprint"] >= MATCH_THRESHOLD
    assert entry["fingerprint_detail"]["cause_similarity"] is not None


async def _cause(fx, iid):
    rows = await QueryLog(fx["graph"]).rows(
        "MATCH (:Incident {id: $i})-[:CAUSED_BY]->(c) RETURN c.id, c.kind", {"i": iid}
    )
    return rows[0]


async def test_typo_in_service_word_still_matches_via_fuzzy_fulltext(fx):
    p_id, p_syms, _ = fx["p"]
    log = QueryLog(fx["graph"])
    text = " ".join(await _pm_text(log, p_id))
    in_text = set(PR._words(text))
    word = next(w for s in p_syms for w in PR._words(s["name"]) if len(w) >= 6 and w in in_text)
    typo = word[:2] + word[3:]  # one deleted letter: edit distance 1
    assert typo not in in_text
    # the fuzzy term alone finds P's postmortem...
    rows = await log.rows(PR.FULLTEXT, {"query": f"%{typo}%", "incident_id": "none", "keep": 50})
    assert p_id in {r[0] for r in rows}
    # ...and so does the tool, when the typo sits in the new incident's service name
    names = {"inc-typo-c0": typo}
    await add_lookalike(fx, "inc-typo", p_syms, service_names=names)
    r = await precedents(fx, incident_id="inc-typo")
    assert f"%{typo}%" in r.result["query"]["fulltext"]
    assert p_id in r.result["by_method"]["fulltext"]
    entry = next(p for p in r.result["precedents"] if p["incident_id"] == p_id)
    assert entry["methods"]["fulltext"] > 0


async def _pm_text(log, iid):
    rows = await log.rows(
        "MATCH (pm:Postmortem)-[:ABOUT]->(:Incident {id: $i}) "
        "RETURN pm.title, pm.body, pm.symptoms_text",
        {"i": iid},
    )
    return rows[0]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


async def test_vector_order_follows_distance_closest_first(fx, hashing_embedder):
    p_id, p_syms, _ = fx["p"]
    await add_lookalike(fx, "inc-vec", p_syms)
    r = await precedents(fx, incident_id="inc-vec", k=6)
    ids = r.result["by_method"]["vector"]
    assert len(ids) == 6
    rows = await QueryLog(fx["graph"]).rows(
        "MATCH (pm:Postmortem)-[:ABOUT]->(i:Incident) RETURN i.id, pm.embedding"
    )
    q = hashing_embedder.embed([r.result["query"]["text"]])[0]
    expected = sorted(((cosine(q, list(e)), i) for i, e in rows), key=lambda x: (-x[0], x[1]))
    assert ids == [i for _, i in expected[:6]]
    by_id = {p["incident_id"]: p for p in r.result["precedents"]}
    sims = [by_id[i]["methods"]["vector"] for i in ids if i in by_id]
    assert sims == sorted(sims, reverse=True)  # larger similarity = smaller distance
    for sim, (cos, _) in zip(sims, expected, strict=False):
        assert sim == pytest.approx(cos, abs=1e-3)  # similarity = 1 - cosine distance


async def test_past_incident_is_not_its_own_precedent(fx):
    p_id = fx["p"][0]
    r = await precedents(fx, incident_id=p_id)
    res = r.result
    assert p_id not in {p["incident_id"] for p in res["precedents"]}
    for ids in res["by_method"].values():
        assert p_id not in ids
    assert len(res["precedents"]) > 0


async def test_k_limits_merged_list_and_missing_incident_errors(fx):
    r = await precedents(fx, incident_id=fx["p"][0], k=2)
    assert len(r.result["precedents"]) <= 2
    with pytest.raises(ToolError):
        await precedents(fx, incident_id="inc-nope")
    with pytest.raises(ToolError):
        await precedents(fx, incident_id=fx["p"][0], suspect_id="nope")


async def test_result_envelope_has_queries_and_domain_ids(fx):
    p_id = fx["p"][0]
    r = await precedents(fx, incident_id=p_id, k=3)
    assert r.tool == "find_precedents" and r.graph == fx["graph"]
    assert len(r.cypher) == len(r.params) >= 5
    assert any("db.idx.vector.queryNodes" in c for c in r.cypher)
    assert any("db.idx.fulltext.queryNodes" in c for c in r.cypher)
    assert any("shortestPath" in c for c in r.cypher)
    assert p_id in r.nodes_touched
    assert all(n.startswith(("inc-", "pm-", "svc-", "chg-")) or n for n in r.nodes_touched)
    assert any(n.startswith("pm-") for n in r.nodes_touched)
    assert "find_precedents" in {t.name for t in all_tools()}


async def test_empty_memory_returns_empty_list(fx):
    g = naming.asof(fx["sid"], "empty", "true")
    await write_impacts_only(g)  # incidents and symptoms but no postmortems, no indexes
    r = await precedents(fx, graph=g, incident_id="inc-main")
    assert r.result["precedents"] == []
    assert r.result["advisory"] is True
    assert r.cypher  # it still asked
