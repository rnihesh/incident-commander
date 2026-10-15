"""Proof ledger (U6, R12, R13): pre-registration order, hash chain, the registry tool."""

from __future__ import annotations

import asyncio

import pytest
from proof_fixture import rows, setup_incident

from ic.graph import client, naming
from ic.proof import ledger
from ic.proof.canonical import canonical_json, content_hash, sha256_hex
from ic.tools.registry import ScopeError, ToolArgumentError, call_tool, openai_tool_schemas
from ic.tools.result import ToolError


def test_canonical_json_drops_timings_and_sorts_id_sets():
    a = {"ms": 3.2, "b": ["svc-b", "svc-a"], "path": {"path": ["c", "a", "b"]}, "x": 1.0}
    b = {"x": 1, "path": {"path": ["c", "a", "b"], "display_path": ["c", "z", "b"]}}
    b["b"], b["build_ms"] = ["svc-a", "svc-b"], 9
    assert canonical_json(a) == canonical_json(b)
    assert '"path":["c","a","b"]' in canonical_json(a)  # paths keep their order
    assert content_hash(a) == sha256_hex(canonical_json(a))


async def test_chain_records_every_hypothesis_with_expected_states(incident):
    proofs = incident.proofs
    assert [p.seq for p in proofs] == [1, 2, 3, 4, 5]
    assert [p.state for p in proofs] == ["HOLDS"] * 5
    assert proofs[0].conclusion == {"ok": True, "clause": None}
    assert proofs[1].conclusion == {"necessary": True, "rivals": []}
    assert proofs[2].conclusion == {"qualifying": []}
    for p in proofs[3:]:
        assert p.conclusion["ok"] is False and p.conclusion["clause"]  # failing clause kept
    # A decoy claimed as the cause is REFUTED, recording the clause it fails.
    p = await ledger.test_hypothesis(incident.ctx, "suspect_qualifies", incident.decoys[0], "it")
    assert p.state == "REFUTED" and p.holds is False and p.conclusion["clause"]

    stored = await ledger.chain(incident.sid, incident.iid)
    assert [s.hash for s in stored] == [*(q.hash for q in proofs), p.hash]
    assert stored[0].prev_hash == ledger.genesis_hash(
        incident.sid, incident.iid, incident.t, incident.world
    )
    assert [s.prev_id for s in stored] == [None, *(s.id for s in stored[:-1])]
    assert all(s.prev_hash == q.hash for s, q in zip(stored[1:], stored, strict=False))
    assert ledger.verify_chain_integrity(stored) is None
    assert await ledger.head_hash(incident.sid, incident.iid) == p.hash
    assert incident.store.get_anchor(incident.sid, incident.iid)["head_hash"] == proofs[-1].hash
    # The stored falsifier is exactly what Verify will replay.
    assert stored[1].falsifier_tool == "necessity"
    assert stored[1].falsifier_args == {"incident_id": incident.iid, "suspect_id": incident.root}
    assert stored[1].cypher and len(stored[1].cypher) == len(stored[1].params)
    assert stored[1].graph == incident.handles.known_graph


async def test_hypothesis_is_registered_before_its_falsifier_runs(sid, store, monkeypatch):
    inc = await setup_incident(sid, store, run=False)
    real = ledger._run_falsifier
    seen = []

    async def spy(ctx, tool, args):
        state = await rows(
            naming.ledger(sid),
            "MATCH (h:Hypothesis {incident_id: $iid}) "
            "OPTIONAL MATCH (p:Proof)-[:TESTS]->(h) "
            "RETURN h.seq, h.state, h.falsifier_tool, h.falsifier_args, count(p) "
            "ORDER BY h.seq DESC LIMIT 1",
            {"iid": inc.iid},
        )
        seen.append(state[0])
        return await real(ctx, tool, args)

    monkeypatch.setattr(ledger, "_run_falsifier", spy)
    kinds = ["suspect_qualifies", "suspect_necessary", "no_other_qualifying", "entity_refuted"]
    targets = [inc.root, inc.root, inc.root, inc.decoys[0]]
    proofs = [
        await ledger.test_hypothesis(inc.ctx, k, e, "c")
        for k, e in zip(kinds, targets, strict=True)
    ]
    assert len(seen) == 4
    for n, (seq, state, tool, args, n_proofs) in enumerate(seen, 1):
        assert (seq, state, n_proofs) == (n, "REGISTERED", 0)  # on record, no proof yet
        assert tool == ledger.KINDS[kinds[n - 1]].falsifier_tool and args
    for p in proofs:
        assert p.registered_at < p.executed_at
        assert p.state in ("HOLDS", "REFUTED")
    stored = await ledger.chain(sid, inc.iid)
    assert all(s.registered_at < s.executed_at for s in stored)


async def test_editing_one_stored_result_hash_breaks_integrity_at_that_seq(incident):
    g = naming.ledger(incident.sid)
    await client.query(
        g,
        "MATCH (p:Proof {incident_id: $iid, seq: 3}) SET p.result_hash = $h",
        {"iid": incident.iid, "h": sha256_hex("forged")},
    )
    stored = await ledger.chain(incident.sid, incident.iid)
    assert ledger.verify_chain_integrity(stored) == 3
    # Restoring it heals the chain; editing a conclusion (hash fields untouched) breaks it.
    await client.query(
        g,
        "MATCH (p:Proof {incident_id: $iid, seq: 3}) SET p.result_hash = $h",
        {"iid": incident.iid, "h": incident.proofs[2].result_hash},
    )
    assert ledger.verify_chain_integrity(await ledger.chain(incident.sid, incident.iid)) is None
    await client.query(
        g,
        "MATCH (p:Proof {incident_id: $iid, seq: 2}) SET p.conclusion = $c",
        {"iid": incident.iid, "c": '{"necessary":true,"rivals":["chg-x"]}'},
    )
    assert ledger.verify_chain_integrity(await ledger.chain(incident.sid, incident.iid)) == 2


async def test_failing_falsifier_abandons_the_hypothesis(sid, store):
    inc = await setup_incident(sid, store, run=False)
    with pytest.raises(ToolError, match="abandoned"):
        await ledger.test_hypothesis(inc.ctx, "entity_refuted", "chg-nope", "no such change")
    [h] = await ledger.hypotheses(sid, inc.iid)
    assert h.state == "ABANDONED" and h.entity_id == "chg-nope"
    assert await ledger.chain(sid, inc.iid) == []


async def test_stopped_run_leaves_pending_hypotheses_abandoned(sid, store, monkeypatch):
    inc = await setup_incident(sid, store, run=False)
    await ledger.test_hypothesis(inc.ctx, "suspect_qualifies", inc.root, "root qualifies")

    async def stopped(ctx, tool, args):
        raise asyncio.CancelledError  # the run is cancelled while the falsifier runs

    monkeypatch.setattr(ledger, "_run_falsifier", stopped)
    with pytest.raises(asyncio.CancelledError):
        await ledger.test_hypothesis(inc.ctx, "suspect_necessary", inc.root, "pending")
    states = [h.state for h in await ledger.hypotheses(sid, inc.iid)]
    assert states == ["HOLDS", "REGISTERED"]
    assert await ledger.abandon_pending(sid, inc.iid) == [f"hyp-{inc.iid}-2"]
    states = [h.state for h in await ledger.hypotheses(sid, inc.iid)]
    assert states == ["HOLDS", "ABANDONED"]
    assert len(await ledger.chain(sid, inc.iid)) == 1


def test_tool_schema_is_kind_enum_entity_and_claim_only():
    by_name = {s["function"]["name"]: s["function"] for s in openai_tool_schemas()}
    params = by_name["test_hypothesis"]["parameters"]
    assert set(params["properties"]) == {"kind", "entity_id", "claim"}
    assert params["properties"]["kind"]["enum"] == list(ledger.KIND_NAMES)
    assert set(ledger.KIND_NAMES) == {
        "suspect_qualifies",
        "suspect_necessary",
        "no_other_qualifying",
        "entity_refuted",
    }


async def test_tool_via_registry_and_scope(sid, store):
    inc = await setup_incident(sid, store, run=False)
    emitted = []
    inc.ctx.emit = emitted.append
    args = {"kind": "suspect_qualifies", "entity_id": inc.root, "claim": "root qualifies"}
    r = await call_tool("test_hypothesis", args, inc.ctx)
    assert r.graph == naming.ledger(sid)
    assert r.result["state"] == "HOLDS" and r.result["falsifier_result"]["qualifies"] is True
    assert r.cypher and f"prf-{inc.iid}-1" in r.nodes_touched
    assert [e.tool for e in emitted] == ["test_hypothesis"]  # falsifier not emitted twice

    with pytest.raises(ToolArgumentError):
        await call_tool("test_hypothesis", {**args, "kind": "anything_goes"}, inc.ctx)
    with pytest.raises(ToolArgumentError):
        await call_tool("test_hypothesis", {**args, "falsifier_tool": "reflex"}, inc.ctx)

    other = naming.new_session_id()
    inc.ctx.ledger_graph = naming.ledger(other)
    with pytest.raises(ScopeError):
        await call_tool("test_hypothesis", args, inc.ctx)
    with pytest.raises(ScopeError):
        await ledger.test_hypothesis(inc.ctx, "suspect_qualifies", inc.root, "direct")
    assert not await client.graph_exists(naming.ledger(other))


# --- trusted t/world (the run's own values, not the editable ledger copies) ------------

REQUIRED = ("suspect_qualifies", "suspect_necessary", "no_other_qualifying")


async def _prove_with_edited_t(sid, store, *, trusted: bool):
    """Open the incident, edit LedgerIncident.t before the first proof, prove, anchor."""
    import dataclasses

    inc = await setup_incident(sid, store, run=False)  # open_incident at the real t
    await client.query(
        naming.ledger(sid),
        "MATCH (li:LedgerIncident {id: $iid}) SET li.t = $t",
        {"iid": inc.iid, "t": inc.t - 3600},
    )
    ctx = inc.ctx
    if trusted:
        ctx = dataclasses.replace(ctx, trusted_t=inc.t, trusted_world=inc.world)
    proofs = [await ledger.test_hypothesis(ctx, k, inc.root, k) for k in REQUIRED]
    kw = {"t": inc.t, "world": inc.world} if trusted else {}
    await ledger.anchor(sid, inc.iid, store, **kw)
    return inc, proofs


async def test_ledger_t_edited_before_the_first_proof_is_caught_with_trusted_values(sid, store):
    from ic.proof.verify import verify_incident

    inc, proofs = await _prove_with_edited_t(sid, store, trusted=True)
    # The chain is rooted at the run's own t, not the edited ledger copy...
    assert proofs[0].prev_hash == ledger.genesis_hash(sid, inc.iid, inc.t, inc.world)
    assert proofs[0].t == inc.t
    anchor = store.get_anchor(sid, inc.iid)
    assert (anchor["t"], anchor["world"]) == (inc.t, inc.world)
    # ...so Verify replays at the true t and flags the edited ledger.
    r = await verify_incident(sid, inc.iid, store)
    assert r.status == "TAMPERED" and r.t == inc.t
    assert any("differs from the anchored t" in why for why in r.tampered_reasons)


async def test_without_trusted_values_the_edited_t_is_silently_adopted(sid, store):
    """The residual hole the trusted parameters close: by default the edited t roots the
    chain and the anchor, and Verify replays at the attacker's t without saying TAMPERED."""
    from ic.proof.verify import verify_incident

    inc, proofs = await _prove_with_edited_t(sid, store, trusted=False)
    assert proofs[0].prev_hash == ledger.genesis_hash(sid, inc.iid, inc.t - 3600, inc.world)
    assert store.get_anchor(sid, inc.iid)["t"] == inc.t - 3600
    r = await verify_incident(sid, inc.iid, store)
    assert r.status != "TAMPERED" and r.t == inc.t - 3600


async def test_anchor_with_trusted_values_ignores_edited_ledger_copies(incident):
    await client.query(
        naming.ledger(incident.sid),
        "MATCH (li:LedgerIncident {id: $iid}) SET li.t = 1, li.world = 's:x:world'",
        {"iid": incident.iid},
    )
    await ledger.anchor(
        incident.sid, incident.iid, incident.store, t=incident.t, world=incident.world
    )
    a = incident.store.get_anchor(incident.sid, incident.iid)
    assert (a["t"], a["world"]) == (incident.t, incident.world)
