"""Background maintenance (KTD19): session cleanup, the PR auto-close pass, PR graph
cleanup and run pruning, each stage isolated from the others' failures.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ic.actions import pr_store
from ic.actions.github import GitHubClient
from ic.api.state import AppState

log = logging.getLogger(__name__)

PR_CLOSE_INTERVAL_S = 600.0  # the GitHub auto-close pass runs at most every 10 minutes


def github_client(s: AppState) -> GitHubClient:
    if s.github is not None:
        return s.github
    return GitHubClient(s.settings.github_token, s.settings.github_repo)


async def close_stale_prs_once(s: AppState, *, now: float | None = None) -> set[str] | None:
    """KTD19 auto-close: close ``ic/rollback-*`` PRs older than the PR TTL. Returns the
    closed PRs' urls, or None when the pass is not due. Never raises: a GitHub failure is
    logged and the pass still counts (the PR copies then expire by age)."""
    now = time.time() if now is None else now
    if s.last_pr_close and now - s.last_pr_close < PR_CLOSE_INTERVAL_S:
        return None
    s.last_pr_close = now
    gh = github_client(s)
    try:
        numbers = await gh.close_stale_prs(older_than_s=pr_store.PR_GRAPH_TTL_S)
    except Exception:
        log.exception("closing stale rollback PRs failed")
        return set()
    if numbers:
        log.info("auto-closed rollback PRs %s", numbers)
    repo = getattr(gh, "repo", None)
    return {f"https://github.com/{repo}/pull/{n}" for n in numbers} if repo else set()


async def _stage(name: str, fn, default):
    """Run one maintenance stage; a failure is logged and the next stage still runs."""
    try:
        out = fn()
        return await out if asyncio.iscoroutine(out) else out
    except Exception:
        log.exception("maintenance stage %s failed", name)
        return default


async def maintenance_once(s: AppState, *, now: float | None = None) -> dict:
    """One cleanup pass. Every stage is isolated: a FalkorDB or GitHub failure in one never
    skips the others (leaked graph copies would otherwise pile up on a noeviction server)."""
    removed = await _stage("sessions.cleanup", s.sessions.cleanup, [])
    closed = await _stage("close_stale_prs", lambda: close_stale_prs_once(s, now=now), set())
    pr: list[str] = []
    if closed is not None:
        # PR copies go in the same pass that closes their PRs (closed, or past the TTL), so
        # a proof page never outlives its PR and an open PR never loses its proof page early.
        pr = await _stage(
            "cleanup_pr_graphs",
            lambda: pr_store.cleanup_pr_graphs(s.store, closed_pr_urls=closed),
            [],
        )
        for ref in [r for r in s.proof_reports if not pr_store.resolve_proof_ref(s.store, r)]:
            s.proof_reports.pop(ref, None)
    pruned = await _stage("runs.prune", s.runs.prune, 0)
    return {
        "session_graphs": removed,
        "pr_graphs": pr,
        "prs_closed": sorted(closed or ()),
        "runs_pruned": pruned,
    }


async def cleanup_loop(s: AppState) -> None:
    while True:
        await asyncio.sleep(s.settings.cleanup_interval_s)
        try:
            await maintenance_once(s)
        except Exception:
            log.exception("cleanup pass failed")
