"""Triage agent (U7): LLM-driven investigation behind a verdict gate, with a deterministic
fallback (KTD5, KTD6, AE1).

``run_triage`` opens the incident in the proof ledger, moves it OPEN -> TRIAGING, and lets the
model investigate through the registry tools. The model ends the run with ``submit_verdict``;
the gate accepts DIAGNOSED only when the ledger holds HOLDS proofs of ``suspect_qualifies``,
``suspect_necessary`` and ``no_other_qualifying`` for the suspect, and INCONCLUSIVE only when
proofs show at least two qualifying entities. A rejected verdict goes back to the model with
the missing proof kinds named.

If there is no model, the model fails a step twice, the step cap is reached or the spend
breaker trips, the deterministic funnel finishes the job and the verdict is tagged
``degraded``. FAILED is reserved for when that fallback cannot conclude. Either way pending
hypotheses are abandoned and the chain head is anchored in SQLite before the verdict event.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ic.agent import payloads, prompts
from ic.agent.llm import LLMClient
from ic.agent.loop import (
    Emit,
    LoopLimits,
    TerminalDecision,
    ToolRunner,
    UsageHook,
    call_emit,
    run_loop,
    schemas_for,
)
from ic.api.events import Agent, EventType
from ic.graph import naming
from ic.graph.client import query
from ic.incidents import state as st
from ic.proof import ledger
from ic.store import Store
from ic.tools.registry import ScopeError, ToolContext
from ic.tools.result import ToolResult
from ic.world.causality import CLAUSES

# Registry tools the triage model may call. Graph choice stays server-side for all of them.
TRIAGE_TOOLS = (
    "recall",
    "find_precedents",
    "list_symptoms",
    "changes_in_window",
    "screen_candidates",
    "impact_closure",
    "causal_path",
    "exonerate",
    "necessity",
    "absence_certificate",
    "observed_calls",
    "test_hypothesis",
)
SUBMIT_VERDICT = "submit_verdict"
REQUIRED_KINDS = ("suspect_qualifies", "suspect_necessary", "no_other_qualifying")
MAX_REFUTE = 3  # decoys the fallback refutes on the record
MAX_RIVAL_PROOFS = 4  # survivors the fallback proves qualifying for INCONCLUSIVE
MAX_SEEDS_TRIED = 2  # memory-seeded suspects the fallback tests before screening
SUMMARY_MAX = 600
ELLIPSIS = "\u2026"  # marks a summary cut at SUMMARY_MAX

Verdict = Literal["DIAGNOSED", "INCONCLUSIVE", "FAILED"]


def clip_summary(text: str) -> tuple[str, bool]:
    """The model's summary within SUMMARY_MAX; a cut one ends with an ellipsis (and is flagged)."""
    text = text.strip()
    if len(text) <= SUMMARY_MAX:
        return text, False
    return text[: SUMMARY_MAX - len(ELLIPSIS)].rstrip() + ELLIPSIS, True


class VerdictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["DIAGNOSED", "INCONCLUSIVE"]
    suspect_id: str | None = None
    rival_ids: list[str] = []
    summary: str = ""


@dataclass
class Decision:
    verdict: Verdict
    suspect_id: str | None
    rival_ids: list[str]
    summary: str = ""
    truncated: bool = False  # the summary was cut at SUMMARY_MAX (it ends with ELLIPSIS)


@dataclass
class TriageOutcome:
    verdict: Verdict
    suspect_id: str | None
    rival_ids: list[str]
    degraded: bool
    steps: int  # LLM turns
    tool_calls: int  # registry tool calls (model and fallback)
    chain_head: str | None
    summary: str = ""
    reason: str | None = None  # why the fallback ran, when it did
    degraded_reason: str | None = None  # machine code, one of DEGRADED_REASONS
    abandoned: list[str] = field(default_factory=list)
    recall_used: bool = False  # recall ran and memory offered at least one seed
    memory_seeded: bool = False  # the verdict's suspect was a memory seed (F3 fast path)

    @property
    def remediation_allowed(self) -> bool:
        """Only a DIAGNOSED verdict hands off to remediation; INCONCLUSIVE/FAILED page."""
        return self.verdict == "DIAGNOSED"


@dataclass
class IncidentInfo:
    incident_id: str
    title: str | None
    opened_at: int | None
    alert_service: str | None
    t: int


# --- the verdict gate (KTD6) -------------------------------------------------------------


def shown_qualifying(chain: list[ledger.ProofRecord]) -> set[str]:
    """Entities the proofs show qualifying: a HOLDS suspect_qualifies, a REFUTED
    entity_refuted, or rivals named by a REFUTED necessity or certificate."""
    q: set[str] = set()
    for p in chain:
        if p.kind == "suspect_qualifies" and p.holds:
            q.add(p.entity_id)
        elif p.kind == "entity_refuted" and not p.holds:
            q.add(p.entity_id)
        elif p.kind == "suspect_necessary" and not p.holds:
            q.update(p.conclusion.get("rivals") or [])
        elif p.kind == "no_other_qualifying" and not p.holds:
            q.update(p.conclusion.get("qualifying") or [])
    return q


@dataclass
class GateResult:
    ok: bool
    message: str
    suspect_id: str | None = None
    rival_ids: list[str] = field(default_factory=list)


def check_verdict(
    chain: list[ledger.ProofRecord], verdict: str, suspect_id: str | None, rival_ids: list[str]
) -> GateResult:
    if verdict == "DIAGNOSED":
        if not suspect_id:
            return GateResult(False, "DIAGNOSED needs a suspect_id")
        mine = [p for p in chain if p.entity_id == suspect_id]
        held = {p.kind for p in mine if p.holds}
        missing = [k for k in REQUIRED_KINDS if k not in held]
        if missing:
            refuted = [
                f"{p.kind} REFUTED ({p.id}: {p.conclusion})"
                for p in mine
                if p.kind in missing and not p.holds
            ]
            note = f" Already refuted: {'; '.join(refuted)}." if refuted else ""
            return GateResult(
                False,
                f"DIAGNOSED rejected for {suspect_id}: missing HOLDS proofs of kind "
                f"{', '.join(missing)}. Run test_hypothesis with each missing kind for "
                f"{suspect_id}, or submit INCONCLUSIVE if rivals qualify.{note}",
            )
        return GateResult(True, "accepted", suspect_id, [])
    if verdict == "INCONCLUSIVE":
        q = shown_qualifying(chain)
        named = {i for i in [suspect_id, *rival_ids] if i}
        unproven = sorted(named - q)
        if len(q) < 2:
            return GateResult(
                False,
                "INCONCLUSIVE rejected: it needs proofs showing at least two qualifying "
                f"entities; the proofs show {sorted(q) or 'none'}. Run suspect_qualifies for "
                "each entity you believe qualifies, or suspect_necessary to expose rivals.",
            )
        if unproven:
            return GateResult(
                False,
                f"INCONCLUSIVE rejected: no proof shows {', '.join(unproven)} qualifying. "
                f"Proven qualifying: {', '.join(sorted(q))}.",
            )
        suspect = suspect_id if suspect_id in q else sorted(q)[0]
        return GateResult(True, "accepted", suspect, sorted(q - {suspect}))
    return GateResult(False, f"unknown verdict {verdict!r}")


# --- one triage run ----------------------------------------------------------------------


async def _rows(ctx: ToolContext, graph: str, cypher: str, params: dict) -> list[list]:
    if not ctx.allows(graph):
        raise ScopeError(f"triage may not read {graph}")
    return (await query(graph, cypher, params, read_only=True)).result_set


async def incident_info(ctx: ToolContext, incident_id: str) -> IncidentInfo:
    graph = ctx.graph_for("snapshot")
    rows = await _rows(
        ctx,
        graph,
        "OPTIONAL MATCH (i:Incident {id: $iid}) OPTIONAL MATCH (m:SnapshotMeta) "
        "RETURN i.title, i.opened_at, i.alert_service_id, max(m.t)",
        {"iid": incident_id},
    )
    title, opened_at, alert_service, t = rows[0] if rows else (None, None, None, None)
    return IncidentInfo(incident_id, title, opened_at, alert_service, int(t or opened_at or 0))


async def entity_details(
    ctx: ToolContext, entity_id: str, *, incident_id: str | None = None, t: int | None = None
) -> dict:
    """Root-cause facts for the verdict card, read server-side from the snapshots.

    ``lead_s`` is the earliest symptom onset minus the entity's event time (a Change's
    ``valid_from``, an Expiry's ``valid_to``), read from the as-true snapshot when
    ``incident_id`` is given. ``known_at_alert`` is whether the fact was recorded by the
    alert time ``t`` (``recorded_at <= t``); without ``t`` it falls back to whether the
    as-known snapshot holds the entity."""
    q = (
        "MATCH (c:Change {id: $id})-[:CHANGES]->(s:Service) "
        "OPTIONAL MATCH (p:Person)-[:AUTHORED]->(c) "
        "RETURN c.kind AS kind, c.summary AS summary, s.id AS service, c.valid_from AS t, "
        "p.name AS author, c.recorded_at AS recorded_at "
        "UNION ALL "
        "MATCH (c:Expiry {id: $id})-[:EXPIRES_ON]->(s:Service) "
        "RETURN c.kind AS kind, c.summary AS summary, s.id AS service, c.valid_to AS t, "
        "null AS author, c.recorded_at AS recorded_at"
    )
    graphs = [g for g in (ctx.snapshot_known, ctx.snapshot_true) if g]
    found: dict[str, list] = {}
    for g in graphs:
        rows = await _rows(ctx, g, q, {"id": entity_id})
        if rows:
            found[g] = rows[0]
    row = found.get(ctx.snapshot_known or "") or next(iter(found.values()), None)
    if row is None:
        return {
            "change": entity_id,
            "service": "",
            "kind": "",
            "summary": "",
            "known_at_alert": False,
        }
    kind, summary, service, event_t, author, recorded_at = row
    if t is not None:
        known = recorded_at is not None and recorded_at <= t
    else:
        known = bool(ctx.snapshot_known and ctx.snapshot_known in found)
    out: dict[str, Any] = {
        "change": entity_id,
        "service": service,
        "kind": kind or "",
        "summary": summary or "",
        "deployed_at": payloads.iso(event_t),
        "known_at_alert": known,
    }
    if author:
        out["author"] = author
    onset_graph = ctx.snapshot_true or ctx.snapshot_known
    if incident_id and onset_graph and event_t is not None:
        rows = await _rows(
            ctx,
            onset_graph,
            "MATCH (:Incident {id: $iid})-[:HAS]->(y:Symptom) RETURN min(y.onset)",
            {"iid": incident_id},
        )
        earliest = rows[0][0] if rows else None
        if earliest is not None:
            out["earliest_onset"] = payloads.iso(earliest)
            out["lead_s"] = int(earliest - event_t)
    return out


def memory_text(res: dict) -> str:
    """One line for the console about what recall found (advisory)."""
    seeds = [x for x in res.get("seeds") or [] if x.get("plausible")]
    learned = [e for e in res.get("learned") or [] if e.get("explains_hop")]
    if not seeds and not learned and not res.get("precedents"):
        return "Memory: nothing learned about these services yet; screening every candidate."
    bits = []
    for e in learned[:2]:
        bits.append(
            f"learned {e['caller']} -> {e['callee']} (confidence {e['confidence']:.2f}, "
            f"from {e['first_learned_incident']})"
        )
    if res.get("precedents"):
        p = res["precedents"][0]
        bits.append(f"precedent {p['incident_id']} caused on {p['cause_service']}")
    head = "Memory: " + "; ".join(bits) if bits else "Memory"
    if seeds:
        return f"{head}. Testing {seeds[0]['entity_id']} first; the proofs still decide."
    return f"{head}. No seed in the window; screening every candidate."


class _Run:
    def __init__(
        self,
        ctx: ToolContext,
        info: IncidentInfo,
        emit: Emit | None,
    ):
        self.ctx = ctx
        self.info = info
        self.emit = emit
        self.runner = ToolRunner(
            ctx, emit, pinned={"incident_id": info.incident_id}, on_result=self._on_result
        )
        self.recall: dict | None = None  # last recall result (memory: advisory only)
        self.seed_path = False  # the fallback concluded on a memory seed without screening

    @property
    def sid(self) -> str:
        assert self.ctx.sid
        return self.ctx.sid

    @property
    def iid(self) -> str:
        return self.info.incident_id

    async def say(self, text: str, **extra: Any) -> None:
        data = {"topic": "general", "text": text, **extra}
        await call_emit(self.emit, EventType.NARRATION, data, Agent.TRIAGE)

    async def set_state(self, to: st.State) -> None:
        await st.transition(naming.ledger(self.sid), self.iid, to)
        await call_emit(self.emit, EventType.STATE, {"state": to.value}, Agent.SYSTEM)

    async def _on_result(self, name: str, args: dict, r: ToolResult) -> None:
        if name == "recall":
            self.recall = r.result
            await self.say(memory_text(r.result), topic="memory")
        elif name == "screen_candidates":
            for data in payloads.funnel(r):
                await call_emit(self.emit, EventType.FUNNEL, data, Agent.TRIAGE)
        elif name == "absence_certificate":
            data = payloads.certificate(r.result)
            await call_emit(self.emit, EventType.CERTIFICATE, data, Agent.TRIAGE)
        elif name == "test_hypothesis" and r.result.get("kind") == "no_other_qualifying":
            data = payloads.certificate(r.result["falsifier_result"], hash=r.result["hash"])
            await call_emit(self.emit, EventType.CERTIFICATE, data, Agent.TRIAGE)

    async def chain(self) -> list[ledger.ProofRecord]:
        return await ledger.chain(self.sid, self.iid)

    async def gate_handler(self, args: dict) -> TerminalDecision:
        try:
            v = VerdictArgs.model_validate({k: x for k, x in args.items() if x is not None})
        except ValidationError as e:
            return TerminalDecision(False, f"invalid submit_verdict arguments: {e}")
        g = check_verdict(await self.chain(), v.verdict, v.suspect_id, v.rival_ids)
        if not g.ok:
            return TerminalDecision(False, g.message)
        summary, truncated = clip_summary(v.summary)
        return TerminalDecision(
            True,
            "verdict accepted",
            Decision(v.verdict, g.suspect_id, g.rival_ids, summary, truncated),
        )

    # --- deterministic fallback (KTD5) ---

    async def prove(self, kind: str, entity_id: str, claim: str) -> dict | None:
        try:
            r = await self.runner.run(
                "test_hypothesis", {"kind": kind, "entity_id": entity_id, "claim": claim}
            )
        except Exception:
            return None  # the ledger marked it ABANDONED; the gate decides what that means
        return r.result

    async def seeded(self) -> Decision | None:
        """F3 (KTD8): ask memory first and test its top seeds before any screening. A seed
        concludes only when every proof the gate needs HOLDS for it; otherwise the full
        funnel runs as usual (memory reorders the work, it never replaces a proof)."""
        iid = self.iid
        try:
            r = await self.runner.run("recall", {"incident_id": iid})
        except Exception:
            return None
        seeds = [s["entity_id"] for s in r.result.get("seeds") or [] if s.get("plausible")]
        for s in seeds[:MAX_SEEDS_TRIED]:
            p = await self.prove(
                "suspect_qualifies",
                s,
                f"{s} (seeded by memory) passes every KTD23 clause for {iid}",
            )
            if not p or not p.get("holds"):
                continue
            for kind, claim in (
                ("suspect_necessary", f"with {s} removed, no rival explains {iid}"),
                ("no_other_qualifying", f"no other change upstream of {iid}'s symptoms qualifies"),
            ):
                p = await self.prove(kind, s, claim)
                if not p or not p.get("holds"):
                    return None  # a rival may qualify: the full funnel decides
            if check_verdict(await self.chain(), "DIAGNOSED", s, []).ok:
                self.seed_path = True
                return Decision(
                    "DIAGNOSED",
                    s,
                    [],
                    f"{s} is the only qualifying entity (memory seeded it first; the absence "
                    "certificate and necessity proofs hold)",
                )
            return None
        return None

    async def fallback(self) -> Decision:
        iid = self.iid
        d = await self.seeded()
        if d is not None:
            return d
        try:
            scr = await self.runner.run("screen_candidates", {"incident_id": iid})
        except Exception as e:
            return Decision("FAILED", None, [], f"fallback could not screen candidates: {e}")
        survivors: list[str] = scr.result["survivors"]
        verdicts: list[dict] = scr.result["verdicts"]
        if not survivors:
            return Decision("FAILED", None, [], "no change or expiry in the window qualifies")
        if len(survivors) == 1:
            s = survivors[0]
            claims = {
                "suspect_qualifies": f"{s} passes every KTD23 clause for {iid}",
                "suspect_necessary": f"with {s} removed, no rival explains {iid}",
                "no_other_qualifying": f"no other change upstream of {iid}'s symptoms qualifies",
            }
            for kind in REQUIRED_KINDS:
                await self.prove(kind, s, claims[kind])
            rank = {c: i for i, c in enumerate(CLAUSES)}
            decoys = sorted(
                (v for v in verdicts if not v["ok"]),
                key=lambda v: (-rank.get(v["clause"], -1), v["id"]),
            )[:MAX_REFUTE]
            for v in decoys:
                await self.prove("entity_refuted", v["id"], f"{v['id']} fails {v['clause']}")
            chain = await self.chain()
            g = check_verdict(chain, "DIAGNOSED", s, [])
            if g.ok:
                return Decision("DIAGNOSED", s, [], f"{s} is the only qualifying entity")
            return self._inconclusive_or_failed(chain, g.message)
        for s in survivors[:MAX_RIVAL_PROOFS]:
            await self.prove("suspect_qualifies", s, f"{s} passes every KTD23 clause for {iid}")
        return self._inconclusive_or_failed(
            await self.chain(), f"{len(survivors)} entities qualify"
        )

    def _inconclusive_or_failed(self, chain: list[ledger.ProofRecord], why: str) -> Decision:
        q = sorted(shown_qualifying(chain))
        if len(q) >= 2:
            return Decision("INCONCLUSIVE", q[0], q[1:], f"{', '.join(q)} each qualify ({why})")
        return Decision("FAILED", None, [], f"fallback could not conclude: {why}")

    # --- the verdict event ---

    async def path_for(self, entity_id: str, label: str) -> dict:
        d = await entity_details(self.ctx, entity_id, incident_id=self.iid, t=self.info.t)
        nodes = [d["service"]] if d["service"] else []
        target = self.info.alert_service
        if target:
            try:
                r = await self.runner.run(
                    "causal_path", {"entity_id": entity_id, "service_id": target}
                )
                if r.result.get("path"):
                    nodes = [n for n in r.result["path"] if n != entity_id]
            except Exception:
                pass
        return {"details": d, "ref": payloads.path_ref(label, entity_id, d["service"], nodes)}

    async def verdict_payload(
        self, d: Decision, *, degraded: bool, reason: str | None, head: str | None
    ) -> dict:
        data: dict[str, Any] = {
            "outcome": d.verdict,
            "degraded": degraded,
            "paths": [],
            "chain_head": head,
            "suspect_id": d.suspect_id,
            "rival_ids": d.rival_ids,
            "summary": d.summary,
        }
        if d.truncated:
            data["truncated"] = True
        if d.verdict == "DIAGNOSED" and d.suspect_id:
            p = await self.path_for(d.suspect_id, "Causal path")
            rc = p["details"]
            data["root_cause"] = rc
            data["paths"] = [p["ref"]]
            what = f"{rc['kind']} on {rc['service']}" if rc["kind"] else rc["service"]
            data["headline"] = (
                f"{d.suspect_id} ({what}) is the only change that qualifies; "
                "removing it leaves no explanation."
            )
        elif d.verdict == "INCONCLUSIVE":
            ids = [i for i in [d.suspect_id, *d.rival_ids] if i]
            for n, i in enumerate(ids):
                p = await self.path_for(i, f"Path {chr(65 + n) if n < 26 else n + 1}")
                data["paths"].append(p["ref"])
            data["headline"] = (
                f"{' and '.join(ids)} each explain every symptom; neither is necessary once "
                "the other is removed. No remediation; the on-call is paged with every path."
            )
        else:
            data["headline"] = "No verdict: " + (d.summary or "the investigation did not converge")
        if degraded:
            data["confidence_note"] = f"Deterministic fallback ({reason or 'no LLM'})."
        return data


def _degraded_reason(status: str, error: str | None) -> str:
    return {
        "step_cap": error or "step cap reached",
        "llm_error": f"LLM failed after retry: {error}",
        "stopped": error or "spend cap reached",
    }.get(status, error or status)


DEGRADED_REASONS = ("no_llm", "step_cap", "spend_cap", "llm_error", "timeout")


def _reason_code(status: str, error: str | None) -> str:
    """The machine-readable ``degraded_reason`` for a loop that ended without a verdict."""
    if status == "step_cap":
        return "step_cap"
    if status == "stopped":
        return "spend_cap"
    if "timed out" in (error or "").lower() or "timeout" in (error or "").lower():
        return "timeout"
    return "llm_error"


async def run_triage(
    ctx: ToolContext,
    incident_id: str,
    *,
    llm: LLMClient | None,
    emit: Emit | None,
    store: Store,
    limits: LoopLimits | None = None,
    on_usage: UsageHook | None = None,
    should_stop: Callable[[], bool] | None = None,
    alert_text: str | None = None,
) -> TriageOutcome:
    """Investigate ``incident_id`` on the snapshots in ``ctx`` and return the verdict.

    ``ctx`` is built by the server for this session and incident (snapshots, world, ledger);
    ``alert_text`` is untrusted text from the alert source, passed to the model as data."""
    if not ctx.sid or ctx.iid != incident_id:
        raise ValueError("ctx must be scoped to this session and incident")
    if ctx.world_graph is None:
        raise ValueError("ctx.world_graph is required to open the ledger incident")
    if ctx.ledger_graph is None:
        ctx = dataclasses.replace(ctx, ledger_graph=naming.ledger(ctx.sid))
    if ctx.ledger_graph != naming.ledger(ctx.sid):
        raise ScopeError(f"{ctx.ledger_graph} is not this session's ledger")

    if ctx.trusted_world is not None and ctx.trusted_world != ctx.world_graph:
        raise ScopeError("ctx.trusted_world must be the run's world graph")

    info = await incident_info(ctx, incident_id)
    if ctx.trusted_t is not None:  # the server's alert time beats the snapshot's copy
        info = dataclasses.replace(info, t=int(ctx.trusted_t))
    await ledger.open_incident(ctx.sid, incident_id, info.t, ctx.world_graph)
    run = _Run(ctx, info, emit)
    await run.set_state(st.State.TRIAGING)

    decision: Decision | None = None
    reason: str | None = None
    degraded_reason: str | None = None
    steps = 0
    if llm is None:
        reason = "no LLM configured"
        degraded_reason = "no_llm"
    else:
        msgs = prompts.messages(
            incident_id,
            alert_service=info.alert_service,
            opened_at=payloads.iso(info.opened_at),
            title=info.title,
            alert_text=alert_text,
        )
        tools = [*schemas_for(TRIAGE_TOOLS), prompts.SUBMIT_VERDICT_SCHEMA]
        out = await run_loop(
            llm,
            msgs,
            tools,
            run.runner,
            allowed=TRIAGE_TOOLS,
            terminal={SUBMIT_VERDICT: run.gate_handler},
            limits=limits,
            on_usage=on_usage,
            should_stop=should_stop,
        )
        steps = out.steps
        if out.status == "verdict" and out.decision is not None:
            decision = out.decision.data
        else:
            reason = _degraded_reason(out.status, out.error)
            degraded_reason = _reason_code(out.status, out.error)

    degraded = decision is None
    if decision is None:
        await run.say(
            f"{reason[0].upper()}{reason[1:]}. Falling back to the deterministic funnel.",
            degraded_reason=degraded_reason,
        )
        decision = await run.fallback()
    elif decision.summary:
        await run.say(decision.summary)

    abandoned = await ledger.abandon_pending(ctx.sid, incident_id)
    head = await ledger.anchor(
        ctx.sid, incident_id, store, t=ctx.trusted_t, world=ctx.trusted_world
    )
    data = await run.verdict_payload(decision, degraded=degraded, reason=reason, head=head)
    if degraded:
        data["degraded_reason"] = degraded_reason
    seeds = [x["entity_id"] for x in (run.recall or {}).get("seeds") or []]
    recall_used = bool(seeds)
    memory_seeded = bool(
        decision.verdict == "DIAGNOSED" and decision.suspect_id and decision.suspect_id in seeds
    )
    data["steps"] = steps
    data["tool_calls"] = run.runner.calls  # every registry call, the verdict's path included
    data["memory"] = {
        "recalled": run.recall is not None,
        "used": recall_used,
        "seeded": memory_seeded,
        "seed_path": run.seed_path,
        "seeds": seeds,
        "learned": [
            {k: e[k] for k in ("caller", "callee", "confidence")}
            for e in (run.recall or {}).get("learned") or []
        ],
    }
    await call_emit(emit, EventType.VERDICT, data, Agent.TRIAGE)
    await run.set_state(st.State(decision.verdict))
    return TriageOutcome(
        verdict=decision.verdict,
        suspect_id=decision.suspect_id,
        rival_ids=decision.rival_ids,
        degraded=degraded,
        steps=steps,
        tool_calls=run.runner.calls,
        chain_head=head,
        summary=decision.summary,
        reason=reason,
        degraded_reason=degraded_reason,
        abandoned=abandoned,
        recall_used=recall_used,
        memory_seeded=memory_seeded,
    )
