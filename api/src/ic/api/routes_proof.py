"""Public proof page (KTD19, R18): what a rollback PR's commit status links to.

  GET  /api/proof/{ref}          the incident summary and proof chain, read from the PR's
                                 ``pr:{sid}:{iid}:ledger`` / ``:world`` copies
  POST /api/proof/{ref}/verify   re-run Verify against those copies (never the live session)
                                 and refresh the PR's commit status

``ref`` is the random proof ref minted when the PR copies were tracked (24 hex chars); it is
the only handle a visitor holds. No session cookie is needed or read, and nothing that comes
back names the session: the sid is a bearer credential, so it is never serialized and every
string in a response is scrubbed of it (ledger text and Verify's tamper reasons can quote
graph names). The answer key (SQLite) is never read here.

Both routes trust the anchor frozen when the PR copies were made (``pr_anchor``: chain head,
``t``/``world``, rehearsal hash), never the session's live ``chain_anchor``, so a later
re-anchor of the session cannot change what an open PR's page reports. The rehearsal shown
is checked against its sealed hash (``status``: SEALED / TAMPERED / UNANCHORED).

Re-verify is public, so it is bounded: a per-IP hourly cap (``Limiter.allow_proof_verify``),
a per-ref debounce (``ActionBudget.allow_status_rerun``; while debounced the last report is
served with ``cached: true``), one re-verify at a time process-wide, and a real commit status
only within the hourly action budget (otherwise it is posted in mock mode, labelled
simulated).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ic.actions import pr_store
from ic.actions.errors import ActionError
from ic.actions.github import STATUS_CONTEXT, GitHubClient
from ic.agent import payloads
from ic.agent.remediation import GOOD_STATUSES
from ic.agent.triage import REQUIRED_KINDS, check_verdict
from ic.api import auth
from ic.api.common import JSON_ONLY
from ic.api.maintenance import github_client
from ic.api.state import AppState, get_state
from ic.graph import naming
from ic.graph.client import graph_exists, query
from ic.proof import ledger
from ic.proof.verify import VerifyReport, verify_incident
from ic.store import Store
from ic.tools.result import ToolError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proof")
REDACTED = "[session]"
VERIFY_LOCK = "proof-verify"  # one public re-verify at a time: each builds two world copies


# --- helpers ----------------------------------------------------------------------------


def _scrub(obj: Any, sid: str) -> Any:
    """``obj`` with every occurrence of ``sid`` replaced, keys and values alike."""
    if isinstance(obj, str):
        return obj.replace(sid, REDACTED)
    if isinstance(obj, dict):
        return {_scrub(k, sid): _scrub(v, sid) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_scrub(v, sid) for v in obj]
    return obj


def _resolve(store: Store, ref: str) -> tuple[str, str]:
    try:
        return pr_store.require_proof_ref(store, ref)  # validates the ref's format first
    except pr_store.ProofRefNotFound:
        raise HTTPException(404, "unknown proof") from None


async def _require_copies(sid: str, iid: str) -> tuple[str, str]:
    lg, wg = naming.pr_ledger(sid, iid), naming.pr_world(sid, iid)
    if not (await graph_exists(lg) and await graph_exists(wg)):
        raise HTTPException(404, "this proof has expired (its PR was auto-closed)")
    return lg, wg


async def _rows(graph: str, cypher: str, params: dict) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


def _proof_entry(p: ledger.ProofRecord) -> dict:
    return {
        "seq": p.seq,
        "proof_id": p.id,
        "hypothesis_id": p.hypothesis_id,
        "kind": p.kind,
        "entity_id": p.entity_id,
        "claim": p.claim,
        "state": p.state,
        "holds": p.holds,
        "conclusion": p.conclusion,
        "result_hash": p.result_hash,
        "hash": p.hash,
        "prev_hash": p.prev_hash,
        "cypher": p.cypher,
        "params": p.params,
        "executed_at": p.executed_at,
    }


async def _rehearsal(lg: str, iid: str, anchor: dict | None) -> dict | None:
    """The PR's latest rehearsal record with its integrity: ``SEALED`` when its content hash
    matches the one frozen with the PR anchor, ``TAMPERED`` when it differs (or was never
    sealed), ``UNANCHORED`` when the PR copies have no frozen anchor at all. A sealed record
    that has disappeared is reported as TAMPERED too."""
    sealed = (anchor or {}).get("rehearsal_hash")
    rec = await ledger.latest_rehearsal(lg, iid)
    if rec is None:
        if sealed is None:
            return None
        return {"status": "TAMPERED", "tampered_reason": "the sealed rehearsal record is missing"}
    out = {
        "result": rec["result"],
        "change_id": rec["change_id"],
        "checked": rec["checked"],
        "detail": rec["detail"],
        "clone_ms": rec["clone_ms"],
        "status": "SEALED",
    }
    if anchor is None:
        out["status"] = "UNANCHORED"
    elif sealed is None:
        out["status"], out["tampered_reason"] = "TAMPERED", "the rehearsal record was never sealed"
    elif ledger.rehearsal_hash(rec) != sealed:
        out["status"], out["tampered_reason"] = "TAMPERED", "the rehearsal record was edited"
    return out


def _public_report(report: VerifyReport) -> dict:
    """Verify's report minus the session id (``sid``) and anything only the session uses."""
    data = report.model_dump(mode="json", exclude={"sid", "abandoned", "pending"})
    keep = (
        "seq", "proof_id", "kind", "entity_id", "claim", "recorded_state", "status",
        "responsible", "late_arriving", "late_ids", "hash_matches", "error",
        "tampered_reason", "recorded_conclusion", "known_conclusion", "true_conclusion",
    )  # fmt: skip
    data["proofs"] = [{k: p.get(k) for k in keep} for p in data["proofs"]]
    return data


async def _post_status(s: AppState, ref: str, sid: str, iid: str, report: VerifyReport) -> dict:
    """Refresh the PR's ``incident-commander/proofs`` status with this result: for real only
    when the PR is real, a token is configured and the hourly action budget allows it."""
    rec = pr_store.pr_record(s.store, sid, iid) or {}
    head = rec.get("head_sha")
    state = "success" if report.status in GOOD_STATUSES else "failure"
    if not head:
        return {"posted": False, "state": state, "reason": "no PR head commit recorded"}
    gh = github_client(s)
    real = not getattr(gh, "mock", True) and rec.get("simulated") is False
    over_budget = real and not s.budget.allow("status")
    client = gh if real and not over_budget else GitHubClient(None, gh.repo, mock=True)
    desc = f"Verify {report.status}: {len(report.proofs)} proofs re-run (proof page)"
    try:
        posted = await client.post_proof_status(
            head, state, desc, pr_store.proof_url(s.settings, ref)
        )
    except ActionError as e:
        log.warning("proof status re-run for %s failed: %s", ref, e)
        return {"posted": False, "state": state, "error": str(e)[:200]}
    out = {
        "posted": True,
        "state": state,
        "context": STATUS_CONTEXT,
        "simulated": bool(posted.get("simulated")),
    }
    if over_budget:
        out["reason"] = "hourly action budget reached"
    return out


# --- routes -----------------------------------------------------------------------------


async def proof_payload(store: Store, ref: str, last_verify: dict | None = None) -> dict:
    """The public proof page for ``ref`` (session id scrubbed). Raises ``HTTPException`` 404
    for an unknown or expired proof; shared by the route and the MCP ``get_proof`` tool."""
    sid, iid = _resolve(store, ref)
    lg, wg = await _require_copies(sid, iid)
    try:
        info = await ledger.incident_info(lg, iid)
    except ToolError:
        raise HTTPException(404, "unknown proof") from None
    chain_error = None
    try:
        chain = await ledger.chain(sid, iid, graph=lg)
    except (ToolError, ledger.LedgerCorrupt) as e:
        chain, chain_error = [], str(e)[:300]
    candidates = sorted({p.entity_id for p in chain if p.kind in REQUIRED_KINDS and p.holds})
    suspects = [e for e in candidates if check_verdict(chain, "DIAGNOSED", e, []).ok]
    rows = await _rows(
        wg,
        "MATCH (i:Incident {id: $iid}) RETURN i.title, i.alert_service_id LIMIT 1",
        {"iid": iid},
    )
    title, alert_service = rows[0] if rows else (None, None)
    anchor = pr_store.pr_anchor(store, sid, iid)  # frozen with the copies, never the live one
    head = chain[-1].hash if chain else None
    t = anchor["t"] if anchor and anchor.get("t") is not None else info.get("t")
    rec = pr_store.pr_record(store, sid, iid) or {}
    pr_url = rec.get("pr_url")
    body = {
        "ref": ref,
        "incident_id": iid,
        "title": title or f"Incident {iid}",
        "alert_service": alert_service,
        "alert_time": payloads.iso(t) if isinstance(t, int | float) else None,
        "verdict": "DIAGNOSED" if len(suspects) == 1 else "UNPROVEN",
        "suspect": suspects[0] if len(suspects) == 1 else None,
        "chain_head": head,
        "anchored": anchor is not None,
        "anchor_matches": bool(anchor) and anchor.get("head_hash") == head,
        "proofs": [_proof_entry(p) for p in chain],
        "chain_error": chain_error,
        "rehearsal": await _rehearsal(lg, iid, anchor),
        "pr": {
            "url": pr_url,
            "number": int(pr_url.rsplit("/", 1)[-1])
            if pr_url and pr_url.rsplit("/", 1)[-1].isdigit()
            else None,
            "simulated": rec.get("simulated"),
            "opened_at": payloads.iso(rec["created_at"]) if rec.get("created_at") else None,
            "expires_at": payloads.iso(rec["created_at"] + pr_store.PR_GRAPH_TTL_S)
            if rec.get("created_at")
            else None,
        },
        "status_context": STATUS_CONTEXT,
        "last_verify": last_verify,
    }
    return _scrub(body, sid)


@router.get("/{ref}")
async def proof_page(ref: str, request: Request):
    s = get_state(request)
    return await proof_payload(s.store, ref, s.proof_reports.get(ref))


@router.post("/{ref}/verify", dependencies=JSON_ONLY)
async def proof_verify(ref: str, request: Request):
    s = get_state(request)
    sid, iid = _resolve(s.store, ref)
    await _require_copies(sid, iid)
    ip = auth.client_ip(request, s.settings)
    if not s.limiter.allow_proof_verify(ip):
        msg = "too many re-verifies from this address; try again later"
        return JSONResponse({"error": msg, "reason": msg}, status_code=429)
    async with s.lock_for(VERIFY_LOCK):
        interval = s.settings.proof_rerun_interval_s
        if not s.budget.allow_status_rerun(f"proof:{ref}", interval):
            cached = s.proof_reports.get(ref)
            if cached is not None:
                return {**cached, "cached": True}
            msg = "re-verified moments ago; try again shortly"
            return JSONResponse(
                {"error": msg, "reason": msg, "retry_after": interval}, status_code=429
            )
        await _require_copies(sid, iid)  # still there after waiting for the lock
        # Track the snapshot names first so the PR cleanup deletes them even if we die here.
        try:
            for v in naming.VIEWS:
                pr_store.track_pr_graph(s.store, naming.pr_verify(sid, iid, v), iid, sid)
        except pr_store.PrGraphConflict:
            msg = "this proof's snapshot graphs are tracked for another session"
            return JSONResponse({"error": msg, "reason": msg}, status_code=409)
        try:
            report = await verify_incident(sid, iid, s.store, pr=True)
        except (ToolError, ValueError) as e:
            return JSONResponse({"error": _scrub(str(e)[:300], sid)}, status_code=422)
        data = _public_report(report)
        data["verified_at"] = payloads.iso(time.time())
        data["commit_status"] = await _post_status(s, ref, sid, iid, report)
        data = _scrub(data, sid)
        s.proof_reports[ref] = data
    return {**data, "cached": False}
