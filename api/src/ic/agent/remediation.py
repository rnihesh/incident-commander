"""Remediation agent (U12; KTD7, KTD14, R16, R22): claim, read the ledger, rehearse, act.

Triage and remediation share nothing in memory: remediation takes ids and the run's trusted
``t`` / alert service only, and reads everything it acts on from the session ledger graph
(R22: the investigation subgraph is the handoff):

  1. ``claim`` DIAGNOSED -> REHEARSING (one conditional SET: exactly one caller wins); an
     incident that already has an open PR is REFUSED here: its ``pr:{sid}:{iid}:*`` copies
     and frozen anchor back that PR's public proof page and are never overwritten;
  2. ``handoff``: what it read from the ledger: the verdict the claim proved, the suspect
     (the one entity with HOLDS proofs of every required kind), the proofs, the ruled-out
     set and the chain head;
  3. Verify before acting (KTD4/KTD19): a chain that does not re-verify is REFUSED;
  4. ``rehearsal``: ``rehearse_rollback`` on a clone of the as-true snapshot (KTD14). The
     outcome is written to the ledger as ``(:Rehearsal)-[:FOR]->(:LedgerIncident)`` and its
     content hash sealed in the store (``rehearsal_seal``);
  5. REFUSED (no PR) when the rollback does not clear the fault: the page names the
     surviving explanation and its path (AE4);
  6. otherwise ACTING: freeze the PR anchor (chain head, ``t``/``world``, rehearsal hash),
     copy world + ledger to ``pr:{sid}:{iid}:*``, open the rollback PR, post the proof
     commit status computed by Verify, page the on-call; PR_OPEN -> RESOLVED once the PR
     exists and its status is posted.

A failing PR or commit status emits ``error`` (``action_failed``, with ``retryable``) and
leaves the incident in ACTING, but the on-call is paged regardless, with the failure noted:
the page is the action that must not be lost. Refusals page through
``pr_store.page_without_pr``.
"""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ic.actions.errors import ActionError
from ic.actions.github import STATUS_CONTEXT, GitHubClient, PrResult
from ic.actions.pager import DiscordPager
from ic.actions.pr_store import (
    ProofRefNotFound,
    emit_action,
    emit_action_failed,
    freeze_pr_anchor,
    page_target,
    page_without_pr,
    pr_is_open,
    proof_ref,
    proof_url,
    seal_rehearsal,
    set_pr_url,
    track_pr_graph,
)
from ic.agent import payloads
from ic.agent.loop import Emit, call_emit
from ic.agent.triage import REQUIRED_KINDS, check_verdict
from ic.api.events import Agent, EventType
from ic.config import Settings
from ic.graph import naming
from ic.graph.client import copy_graph, query
from ic.incidents import state as st
from ic.memory import recommendations as recs
from ic.proof import ledger
from ic.proof.verify import verify_incident
from ic.sessions.action_budget import ActionBudget
from ic.store import Store
from ic.tools.registry import ToolContext, call_tool
from ic.tools.rehearse import surviving_text

GOOD_STATUSES = frozenset({"VERIFIED", "DRIFTED"})

CHANGE_Q = (
    "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) "
    "OPTIONAL MATCH (c)-[:TOUCHES]->(x) "
    "RETURN c.kind, c.summary, c.value, c.prev_value, s.id, s.name, "
    "coalesce(x.key, x.name), x.id LIMIT 1"
)

WRITE_REHEARSAL = """
MATCH (li:LedgerIncident {id: $iid})
OPTIONAL MATCH (prev:Rehearsal)-[:FOR]->(li)
WITH li, count(prev) + 1 AS n
CREATE (r:Rehearsal {incident_id: $iid, change_id: $change_id, result: $result,
                     still_explains: $still, checked: $checked, clone_ms: $clone_ms,
                     detail: $detail, at: $at})
SET r.id = 'reh-' + $iid + '-' + toString(n)
CREATE (r)-[:FOR]->(li)
RETURN r
"""
PR_OPEN_REASON = "a PR is already open for this incident"


# --- handoff ----------------------------------------------------------------------------


@dataclass
class Handoff:
    verdict: str
    state: str
    suspect_id: str
    chain_head: str | None
    proofs: list[dict] = field(default_factory=list)
    ruled_out: list[dict] = field(default_factory=list)
    hypotheses: int = 0


async def read_handoff(sid: str, iid: str) -> Handoff | None:
    """What remediation reads from the ledger graph, and nothing else: the incident state,
    the proofs and hypotheses, and the suspect: the one entity whose proofs pass the
    DIAGNOSED gate (None unless exactly one does)."""
    graph = naming.ledger(sid)
    state = await st.get_state(graph, iid)
    chain = await ledger.chain(sid, iid)
    hyps = await ledger.hypotheses(sid, iid)
    candidates = sorted({p.entity_id for p in chain if p.kind in REQUIRED_KINDS and p.holds})
    suspects = [e for e in candidates if check_verdict(chain, "DIAGNOSED", e, []).ok]
    if len(suspects) != 1:
        return None
    return Handoff(
        verdict="DIAGNOSED",  # the chain passes the DIAGNOSED gate for this suspect
        state=state.value if state else "UNKNOWN",
        suspect_id=suspects[0],
        chain_head=chain[-1].hash if chain else None,
        proofs=[
            {
                "id": p.id,
                "seq": p.seq,
                "kind": p.kind,
                "entity_id": p.entity_id,
                "state": p.state,
                "hash": p.hash,
            }
            for p in chain
        ],
        ruled_out=[
            {"change": p.entity_id, "clause": p.conclusion.get("clause"), "proof_id": p.id}
            for p in chain
            if p.kind == "entity_refuted" and p.holds
        ],
        hypotheses=len(hyps),
    )


# --- remediation ------------------------------------------------------------------------


@dataclass
class RemediationResult:
    claimed: bool
    pr: PrResult | None = None
    proof_status: str | None = None
    commit_state: str | None = None
    rehearsal: dict | None = None  # the rehearsal event payload
    paged: str | None = None
    final_state: str | None = None
    errors: list[str] = field(default_factory=list)


async def _set_state(graph: str, iid: str, to: st.State, emit: Emit | None) -> None:
    await st.transition(graph, iid, to)
    await call_emit(emit, EventType.STATE, {"state": to.value}, Agent.SYSTEM)


def _sim(simulated: bool) -> str:
    return " (simulated)" if simulated else ""


async def _change(world: str, change_id: str) -> tuple[SimpleNamespace, str | None, str | None]:
    rows = (await query(world, CHANGE_Q, {"id": change_id}, read_only=True)).result_set
    if not rows:
        raise ActionError(f"{change_id} is not a Change: nothing to roll back", retryable=False)
    kind, summary, value, prev, svc, svc_name, target_name, touches = rows[0]
    change = SimpleNamespace(
        id=change_id,
        kind=kind,
        service_id=svc,
        summary=summary or "",
        value=value,
        prev_value=prev if prev is not None else "",
        touches_id=touches,
        target_key=touches or "deploy",
    )
    return change, svc_name, target_name


async def _causal_path(ctx: ToolContext, entity_id: str, alert_service_id: str) -> list[str]:
    try:
        r = await call_tool(
            "causal_path", {"entity_id": entity_id, "service_id": alert_service_id}, ctx
        )
    except Exception:
        return []
    return [n for n in r.result.get("path") or [] if n != entity_id]


def _context(sid: str, iid: str, t: int, given: ToolContext | None, track) -> ToolContext:
    """The rehearsal runs on this run's as-true snapshot. Built from the graph names alone
    when the caller has no context (a fresh process picking up a DIAGNOSED incident)."""
    world = naming.session_world(sid)
    ctx = given or ToolContext(
        sid=sid,
        iid=iid,
        world_graph=world,
        snapshot_true=naming.asof(sid, iid, "true"),
        snapshot_known=naming.asof(sid, iid, "known"),
        ledger_graph=naming.ledger(sid),
        trusted_t=t,
        trusted_world=world,
    )
    return dataclasses.replace(ctx, view="true", emit=None, track_graph=track or ctx.track_graph)


def _rehearsal_payload(r) -> dict:
    res = r.result
    still = res["still_explains"]
    result = "CLEARED" if res["cleared"] else "REFUSED"
    data: dict[str, Any] = {
        "result": result,
        "status": result,
        "change": res["change_id"],
        "still_explains": still,
        "checked": res["checked"],
        "clone_ms": res["clone_ms"],
        "ms": res["clone_ms"],
        "detail": res["detail"],
        "explained_before": res["before"]["qualifies"],
        "previous_version": res["rollback"]["previous_version"],
        "graph": res["clone"],
        "source": res["source"],
    }
    if still:
        data["surviving"] = {"change": still[0]["entity_id"], "path": still[0]["path"]}
    return data


def _failed_rehearsal(change_id: str, e: Exception) -> dict:
    msg = f"Rehearsal could not run ({type(e).__name__}: {str(e)[:200]}); not acting blind."
    return {
        "result": "REFUSED",
        "status": "REFUSED",
        "change": change_id,
        "still_explains": [],
        "checked": 0,
        "clone_ms": 0,
        "detail": msg,
        "error": True,
    }


async def _record_rehearsal(
    sid: str, iid: str, data: dict, store: Store
) -> tuple[str | None, str | None]:
    """The rehearsal outcome is part of the incident's record (and of the PR ledger copy).
    Returns (record id, content hash); the hash, over the record as read back from the
    graph, is sealed in the store so an edit of the node is detectable."""
    res = await query(
        naming.ledger(sid),
        WRITE_REHEARSAL,
        {
            "iid": iid,
            "change_id": data["change"],
            "result": data["result"],
            "still": json.dumps(data["still_explains"], sort_keys=True),
            "checked": data["checked"],
            "clone_ms": data["clone_ms"],
            "detail": data["detail"],
            "at": time.time(),
        },
    )
    if not res.result_set:
        return None, None
    record = ledger.rehearsal_record(dict(res.result_set[0][0].properties))
    h = ledger.rehearsal_hash(record)
    seal_rehearsal(store, sid, iid, record["id"], h)
    return record["id"], h


async def _refuse(
    *,
    out: RemediationResult,
    reason: str,
    surviving: list[str] | None,
    sid: str,
    iid: str,
    t: int,
    alert_service_id: str,
    title: str,
    emit: Emit | None,
    settings: Settings,
    budget: ActionBudget | None,
    pager: DiscordPager | None,
) -> RemediationResult:
    await _set_state(naming.ledger(sid), iid, st.State.REFUSED, emit)
    out.final_state = st.State.REFUSED.value
    paged = await page_without_pr(
        sid=sid,
        iid=iid,
        t=t,
        alert_service_id=alert_service_id,
        title=title,
        verdict="REFUSED",
        reason=reason,
        emit=emit,
        settings=settings,
        budget=budget,
        pager=pager,
        surviving=surviving,
    )
    out.paged = paged.get("paged")
    if paged.get("error"):
        out.errors.append(paged["error"])
    return out


async def run_remediation(
    *,
    sid: str,
    iid: str,
    t: int,
    alert_service_id: str,
    title: str,
    emit: Emit | None,
    store: Store,
    settings: Settings,
    snapshot_ctx: ToolContext | None = None,
    budget: ActionBudget | None = None,
    github: GitHubClient | None = None,
    pager: DiscordPager | None = None,
    track_graph=None,
    recommendations: list[dict] | None = None,
) -> RemediationResult:
    """Remediate a DIAGNOSED incident from the ledger graph alone. See the module docstring.

    ``snapshot_ctx`` is optional (the snapshots are found by name); ``track_graph`` records
    the rehearsal clone and Verify's snapshots for the session cleanup before they are
    created. ``recommendations`` (open postmortem recommendations on the causal path, R23)
    are cited in the PR body and the page; they are advisory text and never change what is
    rolled back."""
    graph = naming.ledger(sid)
    world = naming.session_world(sid)
    if not await st.claim(graph, iid, st.State.DIAGNOSED, st.State.REHEARSING):
        await call_emit(
            emit,
            EventType.NARRATION,
            {"topic": "general", "text": f"{iid} is not DIAGNOSED or was already claimed."},
            Agent.REMEDIATION,
        )
        return RemediationResult(claimed=False)
    await call_emit(emit, EventType.STATE, {"state": st.State.REHEARSING.value}, Agent.SYSTEM)
    out = RemediationResult(claimed=True)
    refuse = dict(
        out=out,
        sid=sid,
        iid=iid,
        t=t,
        alert_service_id=alert_service_id,
        title=title,
        emit=emit,
        settings=settings,
        budget=budget,
        pager=pager,
    )

    if pr_is_open(store, sid, iid):
        return await _refuse(reason=PR_OPEN_REASON, surviving=None, **refuse)

    h = await read_handoff(sid, iid)
    if h is None:
        reason = "The ledger does not prove a single suspect; nothing to roll back."
        return await _refuse(reason=reason, surviving=None, **refuse)
    change, svc_name, target_name = None, None, None
    try:
        change, svc_name, target_name = await _change(world, h.suspect_id)
    except ActionError:
        pass  # reported when the PR is attempted
    what = f"{change.kind} {h.suspect_id} on {change.service_id}" if change else h.suspect_id
    await call_emit(
        emit,
        EventType.HANDOFF,
        {
            "to": "remediation",
            "summary": (
                f"Read from the ledger graph: {h.verdict}, roll back {what}. "
                f"{len(h.proofs)} proofs on the chain, {len(h.ruled_out)} changes ruled out "
                "on the record. Rehearsing the rollback on a cloned world first."
            ),
            "channel": "ledger graph",
            "verdict": h.verdict,
            "ledger_state": h.state,
            "suspect": h.suspect_id,
            "chain_head": h.chain_head,
            "proofs": h.proofs,
            "ruled_out": h.ruled_out,
            "hypotheses": h.hypotheses,
        },
        Agent.SYSTEM,
    )

    # Verify before acting (KTD4/KTD19): a chain that does not re-verify never opens a PR.
    try:
        track = track_graph or (snapshot_ctx.track_graph if snapshot_ctx else None)
        report = await verify_incident(sid, iid, store, track_graph=track)
        proof_status, n_checked = report.status, len(report.proofs)
    except Exception as e:  # cannot vouch for the proofs: refuse
        proof_status, n_checked = "ERROR", 0
        out.errors.append(f"verify: {e}")
    out.proof_status = proof_status
    if proof_status not in GOOD_STATUSES:
        reason = f"Proofs did not re-verify before remediation (Verify {proof_status})."
        return await _refuse(reason=reason, surviving=None, **refuse)

    # Rehearsal on a cloned world (KTD14): refuse a rollback that does not clear the fault.
    ctx = _context(sid, iid, t, snapshot_ctx, track_graph)
    args = {"incident_id": iid, "change_id": h.suspect_id}
    try:
        r = await call_tool("rehearse_rollback", args, ctx)
    except Exception as e:  # no rehearsal, no action
        rehearsal = _failed_rehearsal(h.suspect_id, e)
        out.errors.append(f"rehearsal: {e}")
    else:
        await call_emit(emit, EventType.TOOL_CALL, payloads.tool_call(r), Agent.REMEDIATION)
        rehearsal = _rehearsal_payload(r)
    rehearsal["record"], rehearsal["content_hash"] = await _record_rehearsal(
        sid, iid, rehearsal, store
    )
    out.rehearsal = rehearsal
    await call_emit(emit, EventType.REHEARSAL, rehearsal, Agent.REMEDIATION)
    if rehearsal["result"] != "CLEARED":
        survivors = surviving_text(rehearsal["still_explains"])
        reason = f"Rollback of {h.suspect_id} refused: {rehearsal['detail']}"
        return await _refuse(reason=reason, surviving=survivors, **refuse)

    await _set_state(graph, iid, st.State.ACTING, emit)

    # PR copies: the proof page and status re-runs outlive the session (KTD1).
    pr_copies = ((world, naming.pr_world(sid, iid)), (graph, naming.pr_ledger(sid, iid)))
    for _, dst in pr_copies:
        track_pr_graph(store, dst, iid, sid)  # refuses before anything is overwritten
    # What the copies are verified against from now on, whatever the session does later.
    anchor = store.get_anchor(sid, iid) or {}
    freeze_pr_anchor(
        store,
        sid,
        iid,
        head_hash=anchor.get("head_hash") or h.chain_head or "",
        t=anchor.get("t") if anchor.get("t") is not None else t,
        world=anchor.get("world") or world,
        rehearsal_hash=rehearsal.get("content_hash"),
    )
    for src, dst in pr_copies:
        await copy_graph(src, dst)

    ref = proof_ref(store, sid, iid)
    if ref is None:  # tracked just above; never fall back to a guessable id
        raise ProofRefNotFound(f"no proof ref recorded for {sid}/{iid}")
    url = proof_url(settings, ref)
    gh = github or GitHubClient(settings.github_token, settings.github_repo, budget=budget)
    nodes = await _causal_path(ctx, h.suspect_id, alert_service_id)
    extra = {
        "Rollback rehearsal": (
            f"CLEARED on a cloned as-true snapshot: {rehearsal['checked']} remaining "
            "candidates re-checked against the KTD23 predicate; none explains any symptom."
        ),
        **recs.pr_sections(recommendations or []),
    }
    failed: list[str] = []  # outbound steps that failed; the page still goes out
    pr: PrResult | None = None
    try:
        if change is None:
            raise ActionError(f"{h.suspect_id} has no rollback target", retryable=False)
        pr = await gh.open_rollback_pr(
            iid,
            change,
            None,
            nodes,
            h.chain_head or "",
            url,
            extra,
            service_name=svc_name,
            target_name=target_name,
            branch_suffix=ref,
        )
    except ActionError as e:
        await emit_action_failed(emit, "pr", e)
        out.errors.append(str(e))
        failed.append(f"rollback PR failed: {str(e)[:200]}")

    if pr is not None:
        out.pr = pr
        set_pr_url(store, sid, iid, pr.url, pr.head_sha, pr.simulated)
        await emit_action(
            emit,
            {
                "kind": "pr",
                "status": "ok",
                "label": f"Rollback PR #{pr.number} opened{_sim(pr.simulated)}",
                "url": pr.url,
                "number": pr.number,
                "simulated": pr.simulated,
                "head_sha": pr.head_sha,
                "detail": f"Restores the pre-change value of {h.suspect_id}; the body carries "
                f"the causal path, the rehearsal and chain head {(h.chain_head or '')[:12]}.",
            },
        )

        # Proof status: the Verify that gated this action (KTD19).
        commit_state = "success" if proof_status in GOOD_STATUSES else "failure"
        out.commit_state = commit_state
        status_client = gh
        if pr.simulated and not gh.mock:  # a simulated PR has no real head commit
            status_client = GitHubClient(None, settings.github_repo, mock=True)
        try:
            posted = await status_client.post_proof_status(
                pr.head_sha, commit_state, f"Verify {proof_status}: {n_checked} proofs re-run", url
            )
        except ActionError as e:
            await emit_action_failed(emit, "commit_status", e)
            out.errors.append(str(e))
            failed.append(f"proof commit status failed: {str(e)[:200]}")
        else:
            await emit_action(
                emit,
                {
                    "kind": "commit_status",
                    "status": "ok",
                    "label": f"{STATUS_CONTEXT}: {commit_state}{_sim(posted['simulated'])}",
                    "url": url,
                    "state": commit_state,
                    "proof_status": proof_status,
                    "simulated": bool(posted["simulated"]),
                    "detail": f"Verify {proof_status} on {n_checked} proofs; chain head "
                    f"{(h.chain_head or '')[:12]} posted to {pr.head_sha[:12]}.",
                },
            )

    # Page (KTD20): always, even when the PR or its status failed (then saying so).
    target = await page_target(world, t, alert_service_id, h.suspect_id)
    who = target.name or "nobody (no on-call found)"
    pg = pager or DiscordPager(settings.discord_webhook_url, budget=budget)
    if change is not None:
        root = f"{change.kind} change {h.suspect_id} on {change.service_id}: {change.summary}"
    else:
        root = f"{h.suspect_id} (no rollback target)"
    fields = {"Chain head": h.chain_head or "-", "Rehearsal": "CLEARED"}
    if failed:
        fields["Action failed"] = "; ".join(failed)
    try:
        res = await pg.page(
            iid,
            title,
            root,
            who,
            target.reason,
            pr.url if pr is not None else "none (the rollback PR could not be opened)",
            proof_status,
            {**fields, **recs.page_fields(recommendations or [])},
        )
    except ActionError as e:
        await emit_action_failed(emit, "page", e)
        out.errors.append(str(e))
    else:
        out.paged = target.name
        await emit_action(
            emit,
            {
                "kind": "page",
                "status": "ok",
                "label": f"Discord page sent to {who}{_sim(res.simulated)}",
                "detail": target.reason + (f" ({'; '.join(failed)})" if failed else ""),
                "person": target.person_id,
                "simulated": res.simulated,
            },
        )

    if failed:  # retryable from ACTING: no PR (or no posted status) yet
        out.final_state = st.State.ACTING.value
        return out
    await _set_state(graph, iid, st.State.PR_OPEN, emit)
    await _set_state(graph, iid, st.State.RESOLVED, emit)
    out.final_state = st.State.RESOLVED.value
    return out
