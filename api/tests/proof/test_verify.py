"""Verify (U6, R14, AE5): replay every proof on fresh :verify: snapshots of the current world."""

from __future__ import annotations

import asyncio
import json

import pytest
from proof_fixture import rows, setup_incident

from ic.graph import client, naming
from ic.graph.schema import OPEN_END
from ic.graph.snapshots import build_snapshots
from ic.proof import ledger
from ic.proof.canonical import canonical_json, sha256_hex
from ic.proof.verify import verify_incident

QUALIFIES, NECESSARY, ABSENCE, REFUTED_1, REFUTED_2 = 1, 2, 3, 4, 5


def by_seq(report) -> dict:
    return {c.seq: c for c in report.proofs}


async def root_valid_from(inc) -> int:
    return (
        await rows(inc.world, "MATCH (c:Change {id: $id}) RETURN c.valid_from", {"id": inc.root})
    )[0][0]


async def verify_graphs_left(sid: str) -> list[str]:
    return [g for g in await client.get_db().list_graphs() if g.startswith(f"s:{sid}:verify:")]


async def test_unchanged_world_verifies_and_is_stable(incident):
    reports = [await verify_incident(incident.sid, incident.iid, incident.store) for _ in range(2)]
    for r in reports:
        assert r.status == "VERIFIED" and r.chain_ok and r.anchor_ok is True
        assert [c.seq for c in r.proofs] == [1, 2, 3, 4, 5]
        assert all(c.status == "VERIFIED" and c.hash_matches for c in r.proofs)
        assert all(c.responsible == [] and not c.late_arriving for c in r.proofs)
        assert r.recomputed_head == r.stored_head == r.anchored_head
        assert r.abandoned == [] and r.pending == []
    # The root's proof carries a canonical causal path (equal-cost ties resolved the same way).
    assert incident.proofs[0].result["path"]["path"]
    assert reports[0].model_dump(exclude={"ms", "snapshot_ms"}) == reports[1].model_dump(
        exclude={"ms", "snapshot_ms"}
    )
    assert await verify_graphs_left(incident.sid) == []
    print(f"\nverify ms={reports[0].ms} snapshot_ms={reports[0].snapshot_ms}")


async def test_single_edit_is_tampered_at_that_seq(incident):
    await client.query(
        naming.ledger(incident.sid),
        "MATCH (p:Proof {incident_id: $iid, seq: 4}) SET p.result_hash = $h",
        {"iid": incident.iid, "h": sha256_hex("forged")},
    )
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED"
    assert r.chain_ok is False and r.broken_at == 4 and r.anchor_ok is False


async def test_consistent_rewrite_passes_integrity_but_is_tampered_vs_anchor(incident):
    g = naming.ledger(incident.sid)
    chain = await ledger.chain(incident.sid, incident.iid)
    # Forge proof 2's result, then recompute every hash and prev_hash after it.
    chain[1].result_hash = sha256_hex("a different result")
    prev = ledger.genesis_hash(incident.sid, incident.iid, incident.t, incident.world)
    for p in chain:
        p.prev_hash = prev
        p.hash = ledger.link_hash(
            prev,
            p.result_hash,
            p.conclusion_hash,
            p.evidence_hash,
            p.hypothesis_hash,
            p.kind,
            p.entity_id,
            p.seq,
        )
        prev = p.hash
        await client.query(
            g,
            "MATCH (x:Proof {id: $id}) SET x.result_hash = $r, x.prev_hash = $ph, x.hash = $h",
            {"id": p.id, "r": p.result_hash, "ph": p.prev_hash, "h": p.hash},
        )
    stored = await ledger.chain(incident.sid, incident.iid)
    assert ledger.verify_chain_integrity(stored) is None  # internally consistent...
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.chain_ok is True
    assert r.anchor_ok is False and r.status == "TAMPERED"  # ...but not what was anchored
    assert r.recomputed_head == stored[-1].hash != r.anchored_head


async def test_unrelated_service_far_away_only_verifies_or_drifts(incident):
    t = incident.t
    checked = set(incident.proofs[ABSENCE - 1].result["checked_services"])
    far = (
        await rows(
            incident.world,
            "MATCH (s:Service) WHERE NOT s.id IN $checked RETURN s.id ORDER BY s.id LIMIT 1",
            {"checked": sorted(checked)},
        )
    )[0][0]
    await client.query(
        incident.world,
        "MATCH (b:Service {id: $far}) "
        "CREATE (a:Service {id: 'svc-judge-new@us-east', name: 'judge-new', region: 'us-east', "
        "domain: 'internal'})"
        "-[:DEPENDS_ON {valid_from: $vf, valid_to: $vt, recorded_at: $rec}]->(b)",
        {"far": far, "vf": t - 86400, "vt": OPEN_END, "rec": t - 86400},
    )
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status in ("VERIFIED", "DRIFTED")
    assert {c.status for c in r.proofs} <= {"VERIFIED", "DRIFTED"}


async def test_ae5_qualifying_upstream_deploy_falsifies_certificate_and_names_it(incident):
    vf = await root_valid_from(incident)
    await client.query(
        incident.world,
        "MATCH (s:Service {id: $svc}) "
        "CREATE (c:Change {id: 'chg-judge-1', kind: 'deploy', service_id: $svc, "
        "summary: 'Deploy v9.9.9: judge', valid_from: $vf, valid_to: $vt, recorded_at: $rec}) "
        "CREATE (c)-[:CHANGES]->(s)",
        {"svc": incident.root_service, "vf": vf + 1, "vt": OPEN_END, "rec": vf + 10},
    )
    assert vf + 10 < incident.t
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    checks = by_seq(r)
    assert r.status == "FALSIFIED" and r.chain_ok and r.anchor_ok
    for seq in (ABSENCE, NECESSARY):
        c = checks[seq]
        assert c.status == "FALSIFIED", c
        assert c.responsible[0] == "chg-judge-1"
        assert c.late_arriving is False  # recorded before t: an honest miss, not a hand edit
    assert checks[ABSENCE].true_conclusion == {"qualifying": ["chg-judge-1"]}
    assert checks[NECESSARY].true_conclusion == {"necessary": False, "rivals": ["chg-judge-1"]}
    assert checks[QUALIFIES].status == "VERIFIED"  # the root itself still qualifies
    assert checks[REFUTED_1].status == checks[REFUTED_2].status == "VERIFIED"


async def test_refuted_decoy_edited_to_qualify_is_falsified(incident):
    vf = await root_valid_from(incident)
    decoy = incident.decoys[0]
    await client.query(
        incident.world,
        "MATCH (c:Change {id: $id})-[e:CHANGES]->() DELETE e",
        {"id": decoy},
    )
    await client.query(
        incident.world,
        "MATCH (c:Change {id: $id}), (s:Service {id: $svc}) "
        "SET c.kind = 'deploy', c.valid_from = $vf, c.recorded_at = $rec, c.service_id = $svc "
        "CREATE (c)-[:CHANGES]->(s)",
        {"id": decoy, "svc": incident.root_service, "vf": vf + 2, "rec": vf + 5},
    )
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    checks = by_seq(r)
    c = checks[REFUTED_1]
    assert c.entity_id == decoy and c.recorded_state == "HOLDS"
    assert c.status == "FALSIFIED" and c.responsible[0] == decoy
    assert c.true_conclusion == {"ok": True, "clause": None}
    assert checks[REFUTED_2].status == "VERIFIED"
    assert checks[ABSENCE].status == "FALSIFIED" and decoy in checks[ABSENCE].responsible


async def test_refuted_claim_flips_when_the_suspect_now_qualifies(sid, store):
    """A REFUTED proof (decoy claimed as cause) is FALSIFIED once the decoy qualifies."""
    inc = await setup_incident(sid, store, run=False)
    decoy = inc.decoys[1]
    p = await ledger.test_hypothesis(inc.ctx, "suspect_qualifies", decoy, f"{decoy} did it")
    assert p.state == "REFUTED"
    vf = await root_valid_from(inc)
    await client.query(
        inc.world,
        "MATCH (c:Change {id: $id})-[e:CHANGES]->() DELETE e",
        {"id": decoy},
    )
    await client.query(
        inc.world,
        "MATCH (c:Change {id: $id}), (s:Service {id: $svc}) "
        "SET c.kind = 'deploy', c.valid_from = $vf, c.recorded_at = $rec "
        "CREATE (c)-[:CHANGES]->(s)",
        {"id": decoy, "svc": inc.root_service, "vf": vf + 2, "rec": vf + 5},
    )
    [c] = (await verify_incident(sid, inc.iid, store)).proofs
    assert c.recorded_state == "REFUTED" and c.status == "FALSIFIED"
    assert c.responsible[0] == decoy  # then the services its new path touches


async def test_hand_edit_without_recorded_at_falsifies_and_is_late_arriving(incident):
    vf = await root_valid_from(incident)
    # Hand-added in the Browser: an event time, but no recorded_at / valid_to.
    await client.query(
        incident.world,
        "MATCH (s:Service {id: $svc}) "
        "CREATE (c:Change {id: 'chg-hand-1', kind: 'deploy', service_id: $svc, valid_from: $vf}) "
        "CREATE (c)-[:CHANGES]->(s)",
        {"svc": incident.root_service, "vf": vf + 1},
    )
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    c = by_seq(r)[ABSENCE]
    assert c.hash_matches  # unknown at t: the as-known replay is unchanged...
    assert c.status == "FALSIFIED"  # ...but the as-true view decides
    assert c.responsible[0] == "chg-hand-1"
    assert c.late_arriving is True and c.late_ids == ["chg-hand-1"]
    assert by_seq(r)[NECESSARY].late_arriving is True


async def test_abandoned_hypotheses_are_listed_not_verified(sid, store, monkeypatch):
    inc = await setup_incident(sid, store, run=False)
    await ledger.test_hypothesis(inc.ctx, "suspect_qualifies", inc.root, "root qualifies")

    async def stopped(ctx, tool, args):
        raise asyncio.CancelledError

    monkeypatch.setattr(ledger, "_run_falsifier", stopped)
    with pytest.raises(asyncio.CancelledError):
        await ledger.test_hypothesis(inc.ctx, "no_other_qualifying", inc.root, "pending")
    pending = await verify_incident(sid, inc.iid, store)
    assert [h.seq for h in pending.pending] == [2]
    await ledger.abandon_pending(sid, inc.iid)
    await ledger.anchor(sid, inc.iid, store)
    r = await verify_incident(sid, inc.iid, store)
    assert [c.seq for c in r.proofs] == [1] and r.status == "VERIFIED"
    assert [(h.seq, h.kind, h.state) for h in r.abandoned] == [
        (2, "no_other_qualifying", "ABANDONED")
    ]
    assert r.pending == []


# --- tamper resistance: everything Verify trusts or displays is bound to the anchor ------


async def _set(inc, match: str, prop: str, value) -> None:
    await client.query(
        naming.ledger(inc.sid),
        f"MATCH {match} SET n.{prop} = $v",
        {"iid": inc.iid, "v": value},
    )


@pytest.mark.parametrize(
    "seq, args",
    [
        # A narrower alert window: the certificate would check a different candidate set.
        (
            ABSENCE,
            lambda inc: {"incident_id": inc.iid, "suspect_id": inc.root, "window_minutes": 1},
        ),
        # A different entity: the re-run would exonerate something else entirely.
        (REFUTED_1, lambda inc: {"incident_id": inc.iid, "change_id": inc.decoys[1]}),
    ],
    ids=["window_minutes", "entity_id"],
)
async def test_edited_falsifier_args_are_tampered(incident, seq, args):
    await _set(
        incident,
        f"(n:Hypothesis {{incident_id: $iid, seq: {seq}}})",
        "falsifier_args",
        canonical_json(args(incident)),
    )
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED"
    c = by_seq(r)[seq]
    assert c.status == "TAMPERED" and "falsifier" in (c.tampered_reason or "")


async def test_edited_falsifier_tool_is_tampered(incident):
    await _set(
        incident,
        f"(n:Hypothesis {{incident_id: $iid, seq: {ABSENCE}}})",
        "falsifier_tool",
        "necessity",
    )
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED" and by_seq(r)[ABSENCE].status == "TAMPERED"


@pytest.mark.parametrize("delta", [-3600, 3600])
async def test_edited_incident_time_is_tampered(incident, delta):
    await _set(incident, "(n:LedgerIncident {id: $iid})", "t", incident.t + delta)
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED"
    assert r.t == incident.t  # the anchored alert time is the one replayed


async def test_edited_incident_time_is_tampered_with_a_legacy_anchor(incident):
    """An anchor row from before t/world were stored: the genesis hash still catches it."""
    head = incident.store.get_anchor(incident.sid, incident.iid)["head_hash"]
    incident.store.put_anchor(session_id=incident.sid, incident_id=incident.iid, head_hash=head)
    await _set(incident, "(n:LedgerIncident {id: $iid})", "t", incident.t - 3600)
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED" and r.anchor_ok is False


async def test_edited_incident_world_is_tampered(incident):
    await _set(incident, "(n:LedgerIncident {id: $iid})", "world", incident.handles.true_graph)
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED"


@pytest.mark.parametrize(
    "prop, value",
    [
        ("cypher", '["MATCH (n) RETURN n"]'),
        ("params", '[{"forged":true}]'),
        ("nodes_touched", '["chg-forged"]'),
        ("graph", "s:elsewhere"),
    ],
)
async def test_edited_proof_evidence_breaks_integrity(incident, prop, value):
    await _set(incident, "(n:Proof {incident_id: $iid, seq: 2})", prop, value)
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED" and r.chain_ok is False and r.broken_at == 2


async def test_reordered_cypher_breaks_integrity(incident):
    """Statement order is meaning (cypher[i] pairs with params[i]), so it is hashed too."""
    [[cy]] = await rows(
        naming.ledger(incident.sid),
        "MATCH (p:Proof {incident_id: $iid, seq: 3}) RETURN p.cypher",
        {"iid": incident.iid},
    )
    stmts = json.loads(cy)
    assert len(stmts) > 1 and stmts != stmts[::-1]
    await _set(incident, "(n:Proof {incident_id: $iid, seq: 3})", "cypher", json.dumps(stmts[::-1]))
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED" and r.broken_at == 3


@pytest.mark.parametrize(
    "prop, value",
    [
        ("claim", "the judge did it"),
        ("expected", '{"ok":false}'),
        ("entity_id", "chg-forged"),
        ("kind", "bogus_kind"),
        ("falsifier_args", "{not json"),
    ],
)
async def test_edited_hypothesis_is_tampered(incident, prop, value):
    await _set(incident, "(n:Hypothesis {incident_id: $iid, seq: 1})", prop, value)
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED" and r.chain_ok is False


async def test_proof_hidden_as_abandoned_is_tampered(incident):
    """Marking a proven hypothesis ABANDONED must not quietly drop it from the replay."""
    await _set(incident, "(n:Hypothesis {incident_id: $iid, seq: 3})", "state", "ABANDONED")
    r = await verify_incident(incident.sid, incident.iid, incident.store)
    assert r.status == "TAMPERED" and r.chain_ok is False and r.broken_at == 3


async def test_unanchored_chain_is_never_verified(sid, store, tmp_path):
    inc = await setup_incident(sid, store)
    from ic.store import Store

    r = await verify_incident(sid, inc.iid, Store(tmp_path / "empty.sqlite3"))
    assert r.status == "UNANCHORED" and r.anchor_ok is None and r.chain_ok
    assert all(c.status == "VERIFIED" for c in r.proofs)  # per-proof replay still runs


async def test_a_recorded_rival_that_stops_qualifying_falsifies_the_refuted_proof(sid, store):
    """An INCONCLUSIVE chain names its rivals: a REFUTED certificate / necessity proof
    recorded rivals X and Y. When X no longer qualifies as-true the boolean is unchanged
    (Y still does), but the recorded evidence no longer stands: FALSIFIED, not DRIFTED."""
    inc = await setup_incident(sid, store, run=False)
    vf = await root_valid_from(inc)
    for cid, dt in (("chg-rival-x", 1), ("chg-rival-y", 2)):
        await client.query(
            inc.world,
            "MATCH (s:Service {id: $svc}) "
            "CREATE (c:Change {id: $id, kind: 'deploy', service_id: $svc, "
            "summary: 'Deploy rival', valid_from: $vf, valid_to: $vt, recorded_at: $rec}) "
            "CREATE (c)-[:CHANGES]->(s)",
            {"svc": inc.root_service, "id": cid, "vf": vf + dt, "vt": OPEN_END, "rec": vf + 10},
        )
    await build_snapshots(inc.world, sid, inc.iid, inc.t)
    for kind in ("no_other_qualifying", "suspect_necessary"):
        p = await ledger.test_hypothesis(inc.ctx, kind, inc.root, f"{kind} {inc.root}")
        assert p.state == "REFUTED"
        named = p.conclusion.get("qualifying", p.conclusion.get("rivals"))
        assert {"chg-rival-x", "chg-rival-y"} <= set(named), p.conclusion
    await ledger.anchor(sid, inc.iid, store)
    assert {c.status for c in (await verify_incident(sid, inc.iid, store)).proofs} == {"VERIFIED"}

    await client.query(inc.world, "MATCH (c:Change {id: 'chg-rival-x'}) DETACH DELETE c")
    r = await verify_incident(sid, inc.iid, store)
    assert r.status == "FALSIFIED"
    for c in r.proofs:
        assert c.status == "FALSIFIED", c
        assert c.responsible[0] == "chg-rival-x"
