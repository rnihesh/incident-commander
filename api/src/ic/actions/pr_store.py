"""PR graph tracking and the no-PR completion page, shared by remediation and the routes.

U11 shipped the Tier 1 remediation stub here (claim, rehearsal SKIPPED, actions). U12
replaced its flow with ``ic.agent.remediation`` (cloned-world rehearsal before acting);
what stays is the part other modules use:

  * ``pr:{sid}:{iid}:world`` / ``pr:{sid}:{iid}:ledger`` copies (and the proof page's
    ``pr:{sid}:{iid}:verify:*`` snapshots), tracked in ``pr_graphs`` so the auto-close
    cleanup deletes them by exact name (incident ids repeat across sessions, hence the sid
    in the name), plus the PR's url / head sha and the opaque proof ref behind the public
    proof page (``/proof/{ref}``). One incident's rows form a group sharing ``created_at``
    and are deleted together: when the auto-close pass closed their PR, or once they are
    older than the PR TTL;
  * the PR's frozen anchor (``pr_anchor``): the chain head, ``t``/``world`` and the sealed
    rehearsal hash the PR copies were made from. Verify of the PR copies (``pr=True``) checks
    against it, never the session's ``chain_anchor``, so nothing that later happens to the
    session (a re-derive re-anchors it) moves an open PR's public proof;
  * the session's rehearsal seal (``rehearsal_seal``): the content hash of the latest
    ``(:Rehearsal)`` record, written with it; Verify and the proof page recompute it;
  * ``page_without_pr``: the completion handler for INCONCLUSIVE / FAILED / REFUSED (KTD7);
  * the ``action`` / ``error`` event helpers both use.
"""

from __future__ import annotations

import re
import secrets
import time
from types import SimpleNamespace
from typing import Any

from ic.actions import who_to_page
from ic.actions.errors import ActionError
from ic.actions.pager import DiscordPager
from ic.agent.loop import Emit, call_emit
from ic.agent.triage import shown_qualifying
from ic.api.events import Agent, EventType
from ic.config import Settings
from ic.graph import naming
from ic.graph.client import delete_graph
from ic.proof import ledger
from ic.sessions.action_budget import ActionBudget
from ic.store import Store

PR_GRAPH_TTL_S = 3600.0  # live-session PRs auto-close after an hour (KTD19)
_PROOF_REF = re.compile(r"[a-f0-9]{24}")  # secrets.token_hex(12)

# --- pr:{sid}:{iid}:* graph tracking ----------------------------------------------------
# The pr_graphs / pr_anchor / rehearsal_seal tables are created (and migrated) by Store.init.


def valid_proof_ref(ref: str) -> bool:
    return isinstance(ref, str) and _PROOF_REF.fullmatch(ref) is not None


class PrGraphConflict(RuntimeError):
    """A PR graph name is already tracked for another session: never overwrite it."""


class ProofRefNotFound(LookupError):
    """No proof ref is recorded for what was asked: a not-found (404), not a conflict."""


def track_pr_graph(store: Store, name: str, iid: str, sid: str) -> None:
    """Record a PR copy before it is written. Re-tracking one's own copy is fine (a retry);
    a row for the same name owned by another session refuses the copy."""
    if not name.startswith(naming.pr_prefix(sid, iid)):
        raise PrGraphConflict(f"{name} is not a PR graph of session {sid} / {iid}")
    with store.conn() as c:
        row = c.execute("SELECT sid FROM pr_graphs WHERE graph_name = ?", (name,)).fetchone()
        if row is not None:
            if row[0] != sid:
                raise PrGraphConflict(f"{name} belongs to another session")
            return
        group = c.execute(
            "SELECT proof_ref, created_at, pr_url, head_sha, simulated FROM pr_graphs "
            "WHERE sid = ? AND incident_id = ? LIMIT 1",
            (sid, iid),
        ).fetchone()
        ref, created, url, head, sim = group or (secrets.token_hex(12), time.time(), *[None] * 3)
        c.execute(
            "INSERT INTO pr_graphs (graph_name, incident_id, sid, pr_url, created_at, proof_ref, "
            "head_sha, simulated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, iid, sid, url, created, ref, head, sim),
        )


def proof_ref(store: Store, sid: str, iid: str) -> str | None:
    """Opaque, public id of an incident's PR copies for the proof page link: the session id
    is a bearer credential and never goes into a PR body or a page."""
    with store.conn() as c:
        row = c.execute(
            "SELECT proof_ref FROM pr_graphs WHERE sid = ? AND incident_id = ? LIMIT 1",
            (sid, iid),
        ).fetchone()
    return row[0] if row else None


def resolve_proof_ref(store: Store, ref: str) -> tuple[str, str] | None:
    """(sid, iid) behind a proof ref, for the proof page / status re-run routes."""
    if not valid_proof_ref(ref):
        return None
    with store.conn() as c:
        row = c.execute(
            "SELECT sid, incident_id FROM pr_graphs WHERE proof_ref = ? LIMIT 1", (ref,)
        ).fetchone()
    return (row[0], row[1]) if row else None


def require_proof_ref(store: Store, ref: str) -> tuple[str, str]:
    """``resolve_proof_ref`` that raises ``ProofRefNotFound`` for an unknown or malformed ref."""
    hit = resolve_proof_ref(store, ref)
    if hit is None:
        raise ProofRefNotFound("unknown proof ref")
    return hit


def set_pr_url(
    store: Store,
    sid: str,
    iid: str,
    pr_url: str,
    head_sha: str | None = None,
    simulated: bool | None = None,
) -> None:
    sim = None if simulated is None else int(bool(simulated))
    with store.conn() as c:
        c.execute(
            "UPDATE pr_graphs SET pr_url = ?, head_sha = coalesce(?, head_sha), "
            "simulated = coalesce(?, simulated) WHERE sid = ? AND incident_id = ?",
            (pr_url, head_sha, sim, sid, iid),
        )


def pr_record(store: Store, sid: str, iid: str) -> dict[str, Any] | None:
    """The PR an incident's copies belong to: url, head sha, simulated, created_at."""
    with store.conn() as c:
        row = c.execute(
            "SELECT pr_url, head_sha, simulated, created_at FROM pr_graphs "
            "WHERE sid = ? AND incident_id = ? LIMIT 1",
            (sid, iid),
        ).fetchone()
    if row is None:
        return None
    url, head, sim, created = row
    return {
        "pr_url": url,
        "head_sha": head,
        "simulated": None if sim is None else bool(sim),
        "created_at": created,
    }


def pr_is_open(store: Store, sid: str, iid: str) -> bool:
    """True once a PR was opened for the incident's copies (its url is recorded)."""
    rec = pr_record(store, sid, iid)
    return bool(rec and rec.get("pr_url"))


def has_pr_copies(store: Store, sid: str, iid: str) -> bool:
    """True while any ``pr:{sid}:{iid}:*`` copy is tracked (until the PR auto-close pass)."""
    return proof_ref(store, sid, iid) is not None


def freeze_pr_anchor(
    store: Store,
    sid: str,
    iid: str,
    *,
    head_hash: str,
    t: int | None,
    world: str | None,
    rehearsal_hash: str | None,
) -> None:
    """Record what the PR copies are verified against, before they are written."""
    with store.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO pr_anchor (sid, incident_id, head_hash, t, world, "
            "rehearsal_hash, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, iid, head_hash, t, world, rehearsal_hash, time.time()),
        )


def pr_anchor(store: Store, sid: str, iid: str) -> dict[str, Any] | None:
    """The anchor frozen with the PR copies: head_hash, t, world, rehearsal_hash, frozen_at."""
    with store.conn() as c:
        row = c.execute(
            "SELECT head_hash, t, world, rehearsal_hash, frozen_at FROM pr_anchor "
            "WHERE sid = ? AND incident_id = ?",
            (sid, iid),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(("head_hash", "t", "world", "rehearsal_hash", "frozen_at"), row, strict=True))


def seal_rehearsal(store: Store, sid: str, iid: str, rehearsal_id: str | None, h: str) -> None:
    """Seal the incident's latest rehearsal record (its content hash) in the store."""
    with store.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO rehearsal_seal (sid, incident_id, rehearsal_id, "
            "content_hash, sealed_at) VALUES (?, ?, ?, ?, ?)",
            (sid, iid, rehearsal_id, h, time.time()),
        )


def rehearsal_seal(store: Store, sid: str, iid: str) -> str | None:
    with store.conn() as c:
        row = c.execute(
            "SELECT content_hash FROM rehearsal_seal WHERE sid = ? AND incident_id = ?",
            (sid, iid),
        ).fetchone()
    return row[0] if row else None


def drop_rehearsal_seal(store: Store, sid: str, iid: str) -> None:
    with store.conn() as c:
        c.execute("DELETE FROM rehearsal_seal WHERE sid = ? AND incident_id = ?", (sid, iid))


def tracked_pr_graphs(store: Store, *, older_than_s: float | None = None) -> list[str]:
    sql, args = "SELECT graph_name FROM pr_graphs", []
    if older_than_s is not None:
        sql, args = sql + " WHERE created_at < ?", [time.time() - older_than_s]
    with store.conn() as c:
        return [r[0] for r in c.execute(sql, args)]


async def cleanup_pr_graphs(
    store: Store,
    older_than_s: float = PR_GRAPH_TTL_S,
    *,
    closed_pr_urls: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Delete (exact names only) every PR copy of an incident whose PR was closed
    (``closed_pr_urls``, from ``GitHubClient.close_stale_prs``) or whose group is older
    than the PR auto-close window, then drop their tracking rows."""
    cutoff = time.time() - older_than_s
    with store.conn() as c:
        rows = c.execute("SELECT graph_name, created_at, pr_url FROM pr_graphs").fetchall()
    names = [n for n, created, url in rows if created < cutoff or (url and url in closed_pr_urls)]
    for name in names:
        await delete_graph(name)
        with store.conn() as c:
            c.execute("DELETE FROM pr_graphs WHERE graph_name = ?", (name,))
    with store.conn() as c:  # an anchor goes with the last of its incident's copies
        c.execute(
            "DELETE FROM pr_anchor WHERE NOT EXISTS (SELECT 1 FROM pr_graphs g "
            "WHERE g.sid = pr_anchor.sid AND g.incident_id = pr_anchor.incident_id)"
        )
    return names


# --- outbound actions -------------------------------------------------------------------


def proof_url(settings: Settings, ref: str) -> str:
    return f"{settings.web_origin.rstrip('/')}/proof/{ref}"


async def emit_action(emit: Emit | None, data: dict) -> None:
    await call_emit(emit, EventType.ACTION, data, Agent.REMEDIATION)


async def emit_action_failed(emit: Emit | None, kind: str, e: Exception) -> None:
    retryable = getattr(e, "retryable", False)
    msg = str(e)[:300]
    await call_emit(
        emit,
        EventType.ERROR,
        {"code": "action_failed", "message": msg, "action": kind, "retryable": retryable},
        Agent.REMEDIATION,
    )
    data = {"kind": kind, "status": "failed", "label": f"{kind} failed", "detail": msg}
    await emit_action(emit, data)


async def page_target(world: str, t: int, alert_service_id: str, suspect: str | None):
    incident = SimpleNamespace(opened_at=t, service_id=alert_service_id)
    return await who_to_page.choose(world, incident, suspect, None)


async def page_without_pr(
    *,
    sid: str,
    iid: str,
    t: int,
    alert_service_id: str,
    title: str,
    verdict: str,
    reason: str,
    emit: Emit | None,
    settings: Settings,
    budget: ActionBudget | None = None,
    pager: DiscordPager | None = None,
    surviving: list[str] | None = None,
) -> dict[str, Any]:
    """Completion handler for INCONCLUSIVE / FAILED / REFUSED (KTD7): page the on-call at T
    with the surviving paths or the reason; no PR opens. ``surviving`` overrides the paths
    read from the proof chain (a REFUSED rehearsal names its own survivors)."""
    world = naming.session_world(sid)
    try:
        chain = await ledger.chain(sid, iid)
    except Exception:
        chain = []
    if surviving is None:
        surviving = sorted(shown_qualifying(chain))
    head = chain[-1].hash if chain else None
    target = await page_target(world, t, alert_service_id, None)
    who = target.name or "nobody (no on-call found)"
    if verdict == "INCONCLUSIVE" and surviving:
        root = f"INCONCLUSIVE: {', '.join(surviving)} each explain every symptom"
    else:
        root = f"{verdict}: {reason}"
    pg = pager or DiscordPager(settings.discord_webhook_url, budget=budget)
    extra = {"Surviving paths": "; ".join(surviving) or "-", "Chain head": head or "-"}
    try:
        res = await pg.page(
            iid, title, root, who, target.reason, "none (no PR opened)", verdict, extra
        )
    except ActionError as e:
        await emit_action_failed(emit, "page", e)
        return {"paged": None, "surviving": surviving, "error": str(e)}
    await emit_action(
        emit,
        {
            "kind": "page",
            "status": "ok",
            "label": f"Discord page sent to {who}{' (simulated)' if res.simulated else ''}",
            "detail": f"{root}. {target.reason}. No PR opened.",
            "person": target.person_id,
            "simulated": res.simulated,
            "surviving": surviving,
        },
    )
    return {"paged": target.name, "surviving": surviving, "simulated": res.simulated}
