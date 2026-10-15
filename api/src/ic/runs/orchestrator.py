"""One investigation run, alert to resolution (U9): reflex, snapshots, triage, remediation.

``start_run`` registers the run, counts it as in progress for the session, and spawns
``execute_run`` as a background task; the SSE route streams the run's events as they land.

Event order for a DIAGNOSED run::

    run_started, reflex, snapshot, state(TRIAGING), tool_call/funnel/hypothesis/proof/
    certificate/narration..., verdict, state(DIAGNOSED), state(REHEARSING), handoff,
    tool_call(rehearse_rollback), rehearsal(CLEARED), state(ACTING), action(pr),
    action(commit_status), action(page), state(PR_OPEN), state(RESOLVED), memory,
    state(LEARNED), done

After the verdict, open postmortem recommendations on the causal path (R23) are announced
with ``narration`` (topic 'recommendation') and cited in the PR body and the page. At
RESOLVED the incident is written back into the session world (U13, KTD8: cause, rollback,
postmortem, learned couplings) and the incident moves to LEARNED.

A rehearsal that does not clear the fault ends ``rehearsal(REFUSED), state(REFUSED),
action(page)`` with no PR (AE4). INCONCLUSIVE / FAILED page the on-call (``action(page)``)
and open no PR. Every run ends with ``done``; an unexpected exception emits ``error`` first.
A cancelled run also moves an incident still in triage to FAILED (``state(FAILED)``), or
hands a rehearsal claim back (``state(DIAGNOSED)``), before its ``error`` and ``done``.

Remediation (``ic.agent.remediation``) gets ids and the trusted alert frame only; it reads
the verdict, suspect and proofs back from the ledger graph (R22).

Trust: the alert time ``t`` and the world graph name come from the server (the injected
fault or the authenticated alert) and are pinned on the ``ToolContext``, so the proof
chain's genesis and the SQLite anchor never depend on values read back from the editable
ledger graph.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from ic.actions import pr_store
from ic.actions.github import GitHubClient
from ic.actions.pager import DiscordPager
from ic.agent import payloads
from ic.agent.llm import LLMClient
from ic.agent.remediation import run_remediation
from ic.agent.triage import run_triage
from ic.api.events import Agent, EventType
from ic.config import Settings
from ic.graph import naming
from ic.graph.client import graph_exists, query
from ic.graph.snapshots import build_snapshots
from ic.incidents import state as st
from ic.memory import recommendations as recs
from ic.memory.writeback import WriteBackRefused, write_back
from ic.runs.registry import Run, RunRegistry
from ic.sessions.action_budget import ActionBudget
from ic.sessions.breaker import Breaker
from ic.sessions.manager import Session, SessionManager
from ic.store import Store
from ic.tools import predicate as P
from ic.tools.investigate import MAX_DEPTH, REVERSE_REACH
from ic.tools.registry import ToolContext, call_tool

log = logging.getLogger(__name__)

_registries: dict[str, RunRegistry] = {}


def registry_for(store: Store) -> RunRegistry:
    """The process-wide run registry for a store (one per SQLite file)."""
    key = str(store.path.resolve())
    if key not in _registries:
        _registries[key] = RunRegistry(store)
    return _registries[key]


@dataclass
class RunDeps:
    store: Store
    settings: Settings
    runs: RunRegistry
    sessions: SessionManager
    breaker: Breaker
    budget: ActionBudget
    llm: LLMClient | None = None
    github: GitHubClient | None = None  # overrides (tests, recordings); None = from settings
    pager: DiscordPager | None = None


def default_deps(
    store: Store, settings: Settings, llm: LLMClient | None = None, **kw: Any
) -> RunDeps:
    return RunDeps(
        store=store,
        settings=settings,
        runs=kw.pop("runs", None) or registry_for(store),
        sessions=kw.pop("sessions", None)
        or SessionManager(
            store, max_sessions=settings.max_sessions, idle_ttl_s=settings.session_idle_ttl_s
        ),
        breaker=kw.pop("breaker", None)
        or Breaker(store, daily_usd=settings.daily_usd_breaker, prices=settings.llm_prices),
        budget=kw.pop("budget", None) or ActionBudget(store, per_hour=settings.actions_per_hour),
        llm=llm,
        **kw,
    )


async def start_run(
    session: Session,
    incident_id: str,
    alert_time: int,
    alert_service_id: str,
    *,
    llm: LLMClient | None,
    store: Store,
    settings: Settings,
    deps: RunDeps | None = None,
    scenario: str | None = None,
    alert_text: str | None = None,
) -> str:
    """Register the run and spawn it in the background. Returns the run id."""
    deps = deps or default_deps(store, settings, llm)
    deps.llm = llm
    run = deps.runs.create(session.sid, incident_id)
    deps.sessions.run_started(session.sid)
    task = asyncio.create_task(
        execute_run(
            run,
            incident_id,
            int(alert_time),
            alert_service_id,
            deps=deps,
            scenario=scenario,
            alert_text=alert_text,
            owns_session_slot=True,
        ),
        name=run.id,
    )
    deps.runs.track(run, task)
    return run.id


# --- pieces -----------------------------------------------------------------------------


async def _rows(graph: str, cypher: str, params: dict | None = None) -> list[list]:
    return (await query(graph, cypher, params, read_only=True)).result_set


async def _alert_info(world: str, iid: str, alert_service_id: str, t: int) -> dict:
    rows = await _rows(
        world,
        "MATCH (i:Incident {id: $iid}) "
        "OPTIONAL MATCH (i)-[:HAS]->(y:Symptom) "
        "RETURN i.title, collect(y.service_id), collect(y.signature), min(y.onset)",
        {"iid": iid},
    )
    title, services, sigs, onset = rows[0] if rows else (None, [], [], None)
    pairs = zip(services, sigs, strict=False)
    on_alert = sorted({sig for svc, sig in pairs if svc == alert_service_id})
    signal = ", ".join(on_alert or sorted(set(sigs))) or "alert"
    detail = f"{len(set(services))} services symptomatic"
    if onset is not None:
        detail += f"; earliest onset {payloads.iso(onset)}"
    return {
        "title": title or f"Alert on {alert_service_id}",
        "alert": {
            "service": alert_service_id,
            "signal": signal,
            "severity": "SEV-2",
            "fired_at": payloads.iso(t),
            "detail": detail,
        },
    }


def _reflex_payload(r) -> dict:
    res = r.result
    oc = res.get("on_call") or {}
    people = oc.get("on_call") or []
    owner = oc.get("owner") or {}
    oncall: dict[str, Any] = {"name": people[0]["name"] if people else "unassigned"}
    if owner.get("team_name"):
        oncall["team"] = owner["team_name"]
    return {
        "ms": r.ms,
        "label": "pre-snapshot",
        "pre_snapshot": True,
        "search_space": res.get("search_space", 0),
        "blast_radius": res.get("blast_radius", 0),
        "suspects": [
            {"change": s["id"], "service": s["service_id"], "summary": s.get("summary") or ""}
            for s in res.get("top_suspects") or []
        ],
        "oncall": oncall,
        "cypher": r.cypher,
        "params": r.params,
        "graph": r.graph,
    }


_METHOD_UI = {"vector": "semantic", "fulltext": "fuzzy", "fingerprint": "path_shape"}


def precedent_payload(res: dict) -> dict:
    """The ``narration`` payload (topic 'precedent') the console's precedent panel reads."""
    matches = []
    for p in res.get("precedents") or []:
        scored = {m: v for m, v in (p.get("methods") or {}).items() if v is not None}
        if not scored:
            continue
        best = max(scored, key=lambda m: (scored[m], m))
        matches.append(
            {
                "incident": p["incident_id"],
                "title": p.get("title") or p["incident_id"],
                "method": _METHOD_UI[best],
                "score": round(float(scored[best]), 3),
                "cause": p.get("remediation") or "",
            }
        )
    text = (
        f"{len(matches)} similar past incident(s) found; advisory only, not proof."
        if matches
        else "No comparable past incident found; advisory only."
    )
    return {"topic": "precedent", "text": text, "matches": matches}


async def _emit_precedents(run, runs: RunRegistry, ctx: ToolContext, iid: str, suspect: str | None):
    try:
        r = await call_tool(
            "find_precedents", {"incident_id": iid, "k": 5, "suspect_id": suspect}, ctx
        )
    except Exception:  # advisory: never fail the run over it
        log.warning("precedent search failed for %s", iid, exc_info=True)
        return
    await runs.emit(run, EventType.NARRATION, precedent_payload(r.result), agent=Agent.TRIAGE)


async def _path_services(ctx: ToolContext, entity_ids: list[str], alert_service_id: str) -> list:
    """Services on the causal path(s) from each entity to the alert service."""
    out: list[str] = []
    for eid in entity_ids:
        try:
            r = await call_tool(
                "causal_path", {"entity_id": eid, "service_id": alert_service_id}, ctx
            )
        except Exception:
            continue
        out += [n for n in r.result.get("path") or [] if n != eid]
    return list(dict.fromkeys(out))


async def _recommendations(run, runs: RunRegistry, ctx: ToolContext, outcome, alert_service_id, t):
    """R23: open recommendations targeting the causal path, announced after the verdict."""
    ids = [i for i in [outcome.suspect_id, *outcome.rival_ids] if i]
    try:
        services = await _path_services(ctx, ids, alert_service_id)
        found = await recs.open_recommendations_on_path(ctx.world_graph, services, t)
    except Exception:  # advisory: never fail the run over it
        log.warning("recommendation lookup failed for %s", ctx.iid, exc_info=True)
        return []
    if found:
        data = recs.narration_payload(found)
        await runs.emit(run, EventType.NARRATION, data, agent=Agent.TRIAGE)
    return found


async def _write_back(run, runs: RunRegistry, emit, sid: str, iid: str, t: int, t0: float):
    """U13: teach the session world what this resolution showed. Never fails the run."""
    resolved_at = t + max(1, math.ceil(time.perf_counter() - t0))
    try:
        await write_back(sid=sid, iid=iid, resolved_at=resolved_at, emit=emit)
    except WriteBackRefused as e:
        log.info("no write-back for %s: %s", iid, e)
    except Exception as e:
        log.exception("write-back failed for %s", iid)
        await runs.emit(
            run,
            EventType.NARRATION,
            {"topic": "general", "text": f"Memory write-back failed: {type(e).__name__}"},
            agent=Agent.SYSTEM,
        )


async def _graph_size(graph: str) -> tuple[int, int]:
    n = (await _rows(graph, "MATCH (n) RETURN count(n)"))[0][0]
    e = (await _rows(graph, "MATCH ()-[e]->() RETURN count(e)"))[0][0]
    return n, e


LATE_CHANGES = """
MATCH (c:Change)-[:CHANGES]->(s:Service)
WHERE s.id IN $area AND $lo <= c.valid_from AND c.valid_from <= $t
  AND (c.recorded_at IS NULL OR c.recorded_at > $t)
RETURN s.id
UNION
MATCH (e:Expiry)-[:EXPIRES_ON]->(s:Service)
WHERE s.id IN $area AND $lo <= e.valid_to AND e.valid_to <= $t
  AND (e.recorded_at IS NULL OR e.recorded_at > $t)
RETURN s.id
"""

LATE_EDGES = """
MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service)
WHERE a.id IN $area AND b.id IN $area AND (d.recorded_at IS NULL OR d.recorded_at > $t)
RETURN DISTINCT a.id, b.id ORDER BY a.id, b.id
"""


async def late_arriving(true_graph: str, iid: str, t: int) -> tuple[list[str], list[dict]]:
    """Facts valid at ``t`` but recorded after it that bear on this incident (as-true only).

    Relevant means: Changes/Expiries whose event falls in the incident window
    ``[earliest onset - DEFAULT_WINDOW_MINUTES, t]`` on the impact area (the symptomatic
    services and everything that can reach them over IMPACTS), and DEPENDS_ON edges valid
    at ``t`` with both ends on the impact area. Old seed-world records that happened to be
    recorded late are not part of the incident and are not listed."""
    syms = await _rows(
        true_graph,
        "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom)-[:ON]->(s:Service) RETURN s.id, y.onset",
        {"iid": iid},
    )
    onsets = [int(r[1]) for r in syms if r[1] is not None]
    if not onsets:
        return [], []
    symptomatic = sorted({r[0] for r in syms})
    earliest = min(onsets)
    area = [
        r[0]
        for r in await _rows(
            true_graph, REVERSE_REACH.format(depth=MAX_DEPTH), {"symptomatic": symptomatic}
        )
    ]
    lo = earliest - P.DEFAULT_WINDOW_MINUTES * 60
    late = sorted(
        {r[0] for r in await _rows(true_graph, LATE_CHANGES, {"area": area, "lo": lo, "t": t})}
    )
    edges = [
        {"caller": a, "callee": b}
        for a, b in await _rows(true_graph, LATE_EDGES, {"area": area, "t": t})
    ]
    return late, edges


async def _snapshot_payload(handles, iid: str, t: int) -> dict:
    graphs = []
    for name, g in (("true", handles.true_graph), ("known", handles.known_graph)):
        n, e = await _graph_size(g)
        graphs.append({"name": name, "nodes": n, "edges": e, "graph": g})
    symptomatic = [
        r[0]
        for r in await _rows(
            handles.known_graph,
            "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom) "
            "RETURN DISTINCT y.service_id ORDER BY y.service_id",
            {"iid": iid},
        )
    ]
    late, late_edges = await late_arriving(handles.true_graph, iid, t)
    return {
        "build_ms": handles.build_ms,
        "t": t,
        "graphs": graphs,
        "symptomatic": symptomatic,
        "late_arriving": late,
        "late_edges": late_edges,
    }


# --- the run ----------------------------------------------------------------------------


async def execute_run(
    run: Run,
    incident_id: str,
    alert_time: int,
    alert_service_id: str,
    *,
    deps: RunDeps,
    scenario: str | None = None,
    alert_text: str | None = None,
    recorded: bool = False,
    owns_session_slot: bool = False,
) -> None:
    """Drive one run to ``done``. Never raises (errors become ``error`` events)."""
    t0 = time.perf_counter()
    runs, sid, iid, t = deps.runs, run.sid, incident_id, int(alert_time)
    emit = runs.emitter(run)
    world = naming.session_world(sid)
    try:
        info = await _alert_info(world, iid, alert_service_id, t)
        started = {"incident_id": iid, **info}
        if scenario:
            started["scenario"] = scenario
        if recorded:
            started["recorded"] = True
        await runs.emit(run, EventType.RUN_STARTED, started, agent=Agent.SYSTEM)

        # a. Reflex on the session world, before any snapshot or LLM call (KTD16).
        world_ctx = ToolContext(sid=sid, iid=iid, world_graph=world)
        r = await call_tool(
            "reflex",
            {"alert_service_id": alert_service_id, "t": t, "incident_id": iid},
            world_ctx,
        )
        await runs.emit(run, EventType.REFLEX, _reflex_payload(r), agent=Agent.SYSTEM)

        # b. As-of snapshots at the alert time (tracked for cleanup by exact name).
        for v in naming.VIEWS:
            deps.sessions.track_graph(sid, naming.asof(sid, iid, v))
        deps.sessions.track_graph(sid, naming.ledger(sid))
        deps.sessions.track_graph(sid, naming.rehearse(sid, iid))  # remediation's clone
        handles = await build_snapshots(world, sid, iid, t)
        snap = await _snapshot_payload(handles, iid, t)
        await runs.emit(run, EventType.SNAPSHOT, snap, agent=Agent.SYSTEM)

        # c. Triage with the run's trusted t/world pinned on the context.
        ctx = ToolContext(
            sid=sid,
            iid=iid,
            world_graph=world,
            snapshot_true=handles.true_graph,
            snapshot_known=handles.known_graph,
            ledger_graph=naming.ledger(sid),
            trusted_t=t,
            trusted_world=world,
        )
        outcome = await run_triage(
            ctx,
            iid,
            llm=deps.llm,
            emit=emit,
            store=deps.store,
            on_usage=deps.breaker.record_usage,
            should_stop=deps.breaker.tripped,
            alert_text=alert_text,
        )

        # Advisory precedent after the verdict (no proof; the UI shows it beside the verdict).
        found: list[dict] = []
        if outcome.verdict in ("DIAGNOSED", "INCONCLUSIVE"):
            await _emit_precedents(run, runs, ctx, iid, outcome.suspect_id)
            found = await _recommendations(run, runs, ctx, outcome, alert_service_id, t)

        # d/e. Remediation or the completion page.
        title = info["title"]
        if outcome.verdict == "DIAGNOSED":
            res = await run_remediation(
                sid=sid,
                iid=iid,
                t=t,
                alert_service_id=alert_service_id,
                title=title,
                emit=emit,
                store=deps.store,
                settings=deps.settings,
                budget=deps.budget,
                github=deps.github,
                pager=deps.pager,
                track_graph=lambda name: deps.sessions.track_graph(sid, name),
                recommendations=found,
            )
            cleared = (res.rehearsal or {}).get("result") == "CLEARED"
            if res.final_state == "RESOLVED" and cleared:
                await _write_back(run, runs, emit, sid, iid, t, t0)
        else:
            await pr_store.page_without_pr(
                sid=sid,
                iid=iid,
                t=t,
                alert_service_id=alert_service_id,
                title=title,
                verdict=outcome.verdict,
                reason=outcome.summary or outcome.reason or "",
                emit=emit,
                settings=deps.settings,
                budget=deps.budget,
                pager=deps.pager,
            )
    except asyncio.CancelledError:
        try:
            await _fail_cancelled(run, runs, sid, iid)
        finally:  # error then done, even if the cleanup above is itself cancelled
            await _close(run, runs, t0, error="the run was cancelled")
        raise
    except Exception as e:
        log.exception("run %s failed", run.id)
        await _close(run, runs, t0, error=f"{type(e).__name__}: {e}")
    finally:
        try:
            if not run.finished:
                await _close(run, runs, t0)
        finally:
            if owns_session_slot:
                deps.sessions.run_finished(sid)


async def _fail_cancelled(run: Run, runs: RunRegistry, sid: str, iid: str) -> None:
    """A cancelled run leaves no incident mid-flight: OPEN / TRIAGING move to FAILED, the
    terminal state the state machine allows from there, and REHEARSING hands its claim back
    to DIAGNOSED (the verdict stands and nothing was acted on; the rehearsal clone is deleted
    by the rehearsal itself). DIAGNOSED, ACTING (retryable) and later states are kept."""
    graph = naming.ledger(sid)
    try:
        if not await graph_exists(graph):
            return  # cancelled before triage opened the incident
        rows = await _rows(
            graph,
            "MATCH (i:LedgerIncident {id: $id}) RETURN coalesce(i.state, $open)",
            {"id": iid, "open": st.State.OPEN.value},
        )
        if not rows:
            return
        cur = st.State(rows[0][0])
        if cur == st.State.OPEN:  # opened, triage not yet started: the only way on is TRIAGING
            cur = await st.transition(graph, iid, st.State.TRIAGING)
        to = {st.State.TRIAGING: st.State.FAILED, st.State.REHEARSING: st.State.DIAGNOSED}.get(cur)
        if to is not None and await st.claim(graph, iid, cur, to):
            await runs.emit(run, EventType.STATE, {"state": to.value}, agent=Agent.SYSTEM)
    except Exception:
        log.exception("could not fail the incident of cancelled run %s", run.id)


async def _close(run: Run, runs: RunRegistry, t0: float, *, error: str | None = None) -> None:
    if run.finished:
        return
    try:
        if error is not None:
            await runs.emit(
                run, EventType.ERROR, {"code": "failed", "message": error[:500]}, agent=Agent.SYSTEM
            )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        await runs.emit(run, EventType.DONE, {"ms": ms}, agent=Agent.SYSTEM)
    except Exception:  # the store itself is failing: nothing more to record
        log.exception("could not close run %s", run.id)
