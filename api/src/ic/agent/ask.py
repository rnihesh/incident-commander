"""Ask the graph (KTD2-KTD4): plain-English questions about the company, answered from
read-only graph tools with every claim tied to the nodes and Cypher behind it.

With a model, ``ask`` runs the thin agent loop over the company tools (plus the incident's
snapshot tools when the visitor is looking at one) and ends at ``submit_answer``. The
grounding guard is server-side: a cited id survives only if some tool call in this answer
touched it, and an answer whose citations all fall away is marked ungrounded (shown as
unverified). Without a model, or when it fails, hits a limit or the spend breaker trips, a
deterministic router answers a fixed set of question shapes (owner, on call, recent
changes, incidents, decisions) with one to three tool calls and a templated answer, and the
response says it is in limited mode. Nothing here writes to any graph.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ic.agent import payloads, prompts
from ic.agent.llm import LLMClient
from ic.agent.loop import (
    LoopLimits,
    LoopOutcome,
    TerminalDecision,
    ToolRunner,
    UsageHook,
    run_loop,
    schemas_for,
)
from ic.graph.client import query
from ic.tools.company import search_terms
from ic.tools.registry import ToolContext
from ic.tools.result import ToolResult

# Registry tools the Ask model may call: all read-only, all on the session world.
ASK_TOOLS = (
    "find_entities",
    "service_profile",
    "team_profile",
    "person_profile",
    "decisions_for",
    "incident_history",
    "who_is_on_call",
)
# Added when the visitor is looking at an incident whose snapshots exist.
INCIDENT_TOOLS = ("observed_calls", "find_precedents", "recall")
# Added when the session's proof ledger holds that incident: its verdict and proofs.
LEDGER_TOOLS = ("incident_proofs",)
SUBMIT_ANSWER = "submit_answer"
ASK_LIMITS = LoopLimits(max_steps=8, step_timeout_s=20.0, retries=1)
MAX_TOKENS = 40_000  # prompt + completion per question; the loop stops past it
QUESTION_MAX = 500
ANSWER_MAX = 1200
LABEL_MAX = 90
ELLIPSIS = "…"
NO_INFO = "The graph has no information about that."

NOTE_NO_MODEL = (
    "Answered in limited mode: no LLM is configured, so a fixed set of question shapes "
    "(owner, on call, recent changes, incidents, decisions) is answered without one."
)
NOTE_SPEND = (
    "Answered in limited mode: the daily LLM spend cap is reached, so a fixed set of "
    "question shapes is answered without the model."
)

Mode = Literal["llm", "limited"]


class AnswerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[str] = []
    found: bool = True


@dataclass
class AskResult:
    answer: str
    mode: Mode
    grounded: bool  # at least one citation survived the guard
    found: bool  # false: the graph has no information about the question
    citations: list[dict] = field(default_factory=list)  # [{id, kind, label}]
    calls: list[dict] = field(default_factory=list)  # [{tool, args, cypher, params, ...}]
    ms: float = 0.0
    tokens: int = 0
    dropped: list[str] = field(default_factory=list)  # cited ids no tool call touched
    note: str | None = None  # why the answer is in limited mode

    def to_dict(self) -> dict:
        return asdict(self)


def clip(text: str, limit: int = ANSWER_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + ELLIPSIS


def day(t: int | None) -> str:
    return (payloads.iso(t) or "?")[:10]


DESCRIBE = """
MATCH (n) WHERE n.id IN $ids
RETURN n.id, labels(n)[0], coalesce(n.name, n.title, n.summary, n.text, n.id)
"""


async def describe(graph: str | None, ids: list[str]) -> list[dict]:
    """``[{id, kind, label}]`` for the cited ids, in citation order."""
    if not ids or graph is None:
        return []
    rows = (await query(graph, DESCRIBE, {"ids": ids}, read_only=True)).result_set
    known = {r[0]: (r[1], clip(str(r[2]), LABEL_MAX)) for r in rows}
    out = []
    for i in ids:
        kind, label = known.get(i, ("Node", i))
        out.append({"id": i, "kind": kind, "label": label})
    return out


def _call_record(name: str, args: dict, r: ToolResult) -> dict:
    return {
        "tool": name,
        "args": {k: v for k, v in args.items() if v is not None},
        "cypher": r.cypher,
        "params": r.params,
        "nodes_touched": len(r.nodes_touched),
        "ms": r.ms,
    }


def _runner(ctx: ToolContext, calls: list[dict]) -> ToolRunner:
    async def record(name: str, args: dict, r: ToolResult) -> None:
        calls.append(_call_record(name, args, r))

    return ToolRunner(ctx, None, on_result=record)


async def ask(
    question: str,
    ctx: ToolContext,
    llm: LLMClient | None,
    *,
    incident_id: str | None = None,
    incident_tools: bool = False,
    ledger_tools: bool = False,
    limits: LoopLimits | None = None,
    on_usage: UsageHook | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AskResult:
    """Answer one question (single turn). ``incident_tools`` adds the snapshot tools, for
    which ``ctx`` must carry the incident's snapshots; ``ledger_tools`` adds the incident's
    stored proofs, for which ``ctx`` must carry ``iid`` and the session's ledger graph."""
    t0 = time.perf_counter()
    if llm is None:
        return await limited(question, ctx, t0, NOTE_NO_MODEL, ledger_tools=ledger_tools)
    if should_stop is not None and should_stop():
        return await limited(question, ctx, t0, NOTE_SPEND, ledger_tools=ledger_tools)

    calls: list[dict] = []
    runner = _runner(ctx, calls)
    allowed = (
        ASK_TOOLS
        + (INCIDENT_TOOLS if incident_tools else ())
        + (LEDGER_TOOLS if ledger_tools else ())
    )
    tokens = 0

    async def on_submit(args: dict) -> TerminalDecision:
        try:
            a = AnswerArgs.model_validate(args)
        except ValidationError as e:
            return TerminalDecision(False, f"invalid submit_answer: {e.errors()[:2]}")
        if not a.answer.strip():
            return TerminalDecision(False, "the answer is empty")
        return TerminalDecision(True, "answer recorded", a)

    async def usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
        nonlocal tokens
        tokens += prompt_tokens + completion_tokens
        if on_usage is not None:
            out = on_usage(model, prompt_tokens, completion_tokens)
            if inspect.isawaitable(out):
                await out

    def stop() -> bool:
        return tokens >= MAX_TOKENS or (should_stop is not None and should_stop())

    out = await run_loop(
        llm,
        prompts.ask_messages(question, incident_id=incident_id),
        [*schemas_for(allowed), prompts.SUBMIT_ANSWER_SCHEMA],
        runner,
        allowed=allowed,
        terminal={SUBMIT_ANSWER: on_submit},
        limits=limits or ASK_LIMITS,
        on_usage=usage,
        should_stop=stop,
        nudge=prompts.ASK_NUDGE,
    )
    if out.status != "verdict" or out.decision is None:
        note = _fallback_note(out, tokens)
        res = await limited(question, ctx, t0, note, ledger_tools=ledger_tools)
        res.tokens = tokens
        return res

    a: AnswerArgs = out.decision.data
    touched = {n for r in runner.results for n in r.nodes_touched}
    cited = list(dict.fromkeys(c.strip() for c in a.citations if c.strip()))
    kept = [c for c in cited if c in touched] if a.found else []
    citations = await describe(ctx.world_graph, kept)
    return AskResult(
        answer=clip(a.answer) if a.found else clip(a.answer or NO_INFO),
        mode="llm",
        grounded=bool(citations),
        found=a.found,
        citations=citations,
        calls=calls,
        ms=round((time.perf_counter() - t0) * 1000, 1),
        tokens=tokens,
        dropped=[c for c in cited if c not in kept],
    )


def _fallback_note(out: LoopOutcome, tokens: int) -> str:
    if out.status == "stopped":
        why = "its token budget is spent" if tokens >= MAX_TOKENS else "the spend cap is reached"
    elif out.status == "step_cap":
        why = f"it did not answer within {out.steps} steps"
    else:
        why = "the model call failed"
    return f"Answered in limited mode: {why}, so the deterministic router answered instead."


# --- the deterministic router (KTD4) -----------------------------------------------------

INTENTS: dict[str, re.Pattern[str]] = {
    "decisions": re.compile(
        r"\b(decid\w*|decision\w*|adrs?|recommend\w*|postmortems?|lessons?|follow[- ]?ups?)\b"
    ),
    "on_call": re.compile(r"\b(on[- ]?call|pager|paged)\b"),
    "owner": re.compile(r"\b(own|owns|owned|owner|owners|ownership|responsible)\b"),
    "incidents": re.compile(r"\b(incidents?|outages?|caused|broke|broken|failures?)\b"),
    "changes": re.compile(r"\b(chang\w*|deploy\w*|rollouts?|releases?|shipped)\b"),
}
INCIDENT_WORDS = re.compile(r"\b(after|outages?|incidents?|postmortems?|last|latest)\b")
# Questions about the investigation the visitor is looking at (answered from its proofs).
CURRENT_INCIDENT = re.compile(
    r"\b(blam\w*|ruled? out|rule\w* out|eliminat\w*|root[- ]cause|culprit|verdict|proofs?|"
    r"this incident|the incident|why (did|was|is) (you|it)|exonerat\w*|refut\w*)\b"
)
WINDOWS = (
    (re.compile(r"\b(last|past|this) (\d{1,3}) days?\b"), None),
    (re.compile(r"\b(last|past|this) week\b"), 7),
    (re.compile(r"\b(last|past|this) month\b"), 30),
    (re.compile(r"\b(today|yesterday)\b"), 2),
)
TRY_ASKING = (
    "Limited mode answers a few question shapes, and this is not one of them. Try: "
    "“Who owns checkout and who is on call?”, “Recent changes to checkout”, "
    "“Incidents on search” or “What did we decide after the last search "
    "outage?”"
)
TRY_ASKING_INCIDENT = (
    " About this incident, try: “Why did you blame the root cause?” or “What got ruled out?”"
)
SHOWN = 4  # rows listed in a templated answer


@dataclass
class Routed:
    answer: str
    cites: list[str]
    found: bool = True


def intents_of(question: str) -> set[str]:
    q = question.lower()
    return {name for name, pat in INTENTS.items() if pat.search(q)}


def window_days(question: str) -> int | None:
    q = question.lower()
    for pat, days in WINDOWS:
        m = pat.search(q)
        if m:
            return int(m.group(2)) if days is None else days
    return None


def no_info(question: str) -> Routed:
    terms = [t for t in search_terms(question) if "-" not in t or "@" in t][:3]
    about = " ".join(terms)
    text = f"The graph has no information about “{about}”." if about else NO_INFO
    return Routed(text, [], False)


def _who(p: dict | None) -> str:
    return f"{p['name']} ({p['person_id']})" if p else "an unknown author"


def _others(matches: list[dict]) -> str:
    rest = [m["id"] for m in matches[1:SHOWN]]
    return f" Other matches: {', '.join(rest)}." if rest else ""


async def limited(
    question: str, ctx: ToolContext, t0: float, note: str, *, ledger_tools: bool = False
) -> AskResult:
    calls: list[dict] = []
    runner = _runner(ctx, calls)
    r = await route(question, runner, incident=ledger_tools)
    citations = await describe(ctx.world_graph, list(dict.fromkeys(r.cites)))
    return AskResult(
        answer=clip(r.answer),
        mode="limited",
        grounded=bool(citations),
        found=r.found,
        citations=citations,
        calls=calls,
        ms=round((time.perf_counter() - t0) * 1000, 1),
        note=note,
    )


async def route(question: str, runner: ToolRunner, *, incident: bool = False) -> Routed:
    """``incident``: the visitor's incident has stored proofs, so questions about it (why
    this change, what was ruled out) are answered from them."""
    found = intents_of(question)
    try:
        if incident and CURRENT_INCIDENT.search(question.lower()):
            return await _incident_story(question, runner)
        if "decisions" in found:
            return await _decisions(question, runner)
        if "owner" in found or "on_call" in found:
            return await _ownership(question, runner, "owner" in found, "on_call" in found)
        if "incidents" in found:
            return await _incidents(question, runner)
        if "changes" in found:
            return await _changes(question, runner)
    except Exception as e:  # a tool refused (unknown id...): say so, never a 500
        return Routed(f"The graph could not answer that: {str(e)[:200]}", [], False)
    return Routed(TRY_ASKING + (TRY_ASKING_INCIDENT if incident else ""), [], False)


CHANGE_ID = re.compile(r"\b(chg-\d+|exp-\d+)\b")


async def _incident_story(question: str, runner: ToolRunner) -> Routed:
    """Why the investigation blamed its change and what it ruled out, from the ledger."""
    m = CHANGE_ID.search(question.lower())
    asked = m.group(1) if m else None
    p = (await runner.run("incident_proofs", {})).result
    iid, blamed, out = p["incident_id"], p["blamed"], p["ruled_out"]
    if not p["proofs"]:
        return Routed(f"{iid} has no stored proofs yet: the investigation is still running.", [])
    ruled = {r["change_id"]: r for r in out}
    lines: list[str] = []
    cites: list[str] = []
    if asked and asked in ruled:
        r = ruled[asked]
        why = f" ({r['failed_clause_means'].lower()})" if r["failed_clause_means"] else ""
        clause = r["failed_clause"]
        lines.append(f"{asked} was ruled out for {iid}: it fails the {clause} clause{why}.")
        cites.append(asked)
    elif blamed and asked in (None, blamed):
        lines.append(
            f"{blamed} was blamed for {iid} because it passes every clause of the explanation "
            "rule, and with it removed no other change explains the symptoms."
        )
        cites.append(blamed)
    elif asked:
        lines.append(f"{asked} does not appear in the proofs for {iid}.")
    else:
        left = ", ".join(p["candidates_left"]) or "none"
        lines.append(f"No single change was blamed for {iid}; still standing: {left}.")
        cites += p["candidates_left"]
    if out and not (asked and asked in ruled):
        items = "; ".join(f"{r['change_id']} (fails {r['failed_clause']})" for r in out[:SHOWN])
        more = f" and {len(out) - SHOWN} more" if len(out) > SHOWN else ""
        lines.append(f"Ruled out: {items}{more}.")
        cites += [r["change_id"] for r in out[:SHOWN]]
    n = len(p["proofs"])
    lines.append(f"Each step is one of {n} stored proofs you can re-run with Verify.")
    return Routed(" ".join(lines), [*cites, iid])


async def _find(runner: ToolRunner, question: str, kinds: list[str], limit: int = 10):
    if not search_terms(question):
        return None
    r = await runner.run("find_entities", {"search": question, "kinds": kinds, "limit": limit})
    return r.result["matches"]


async def _ownership(question: str, runner: ToolRunner, owner: bool, on_call: bool) -> Routed:
    matches = await _find(runner, question, ["Service", "Team"])
    if not matches:
        return no_info(question)
    top = matches[0]
    parts: list[str] = []
    if top["kind"] == "Team":
        p = (await runner.run("team_profile", {"team_id": top["id"]})).result
        subject = f"{p['team']['name']} ({p['team']['id']})"
        shown = p["services"][:SHOWN]
        if owner:
            n = p["service_count"]
            more = f" and {n - len(shown)} more" if n > len(shown) else ""
            parts.append(f"{subject} owns {n} services: {', '.join(shown)}{more}.")
        cites = [p["team"]["id"], *(shown if owner else [])]
    else:
        p = (await runner.run("service_profile", {"service_id": top["id"]})).result
        subject = f"{p['service']['name']} ({p['service']['id']})"
        o = p["owner"]
        if owner:
            parts.append(
                f"{subject} is owned by {o['team_name']} ({o['team_id']})."
                if o
                else f"{subject} has no current owner."
            )
        cites = [p["service"]["id"], *([o["team_id"]] if o else [])]
    if on_call:
        people = p["on_call"]
        names = ", ".join(_who(x) for x in people)
        target = "" if owner else f" for {subject}"
        when = day(p["as_of"])
        parts.append(
            f"On call{target} as of {when}: {names}."
            if names
            else f"Nobody is on call{target} as of {when}."
        )
        cites += [x["person_id"] for x in people]
    return Routed(" ".join(parts) + _others(matches), cites)


async def _changes(question: str, runner: ToolRunner) -> Routed:
    matches = await _find(runner, question, ["Service", "Person"])
    if matches is None:
        return Routed("Name a service or a person, e.g. “recent changes to checkout”.", [], False)
    if not matches:
        return no_info(question)
    top = matches[0]
    if top["kind"] == "Person":
        p = (await runner.run("person_profile", {"person_id": top["id"]})).result
        who = f"{p['person']['name']} ({p['person']['id']})"
        rows = p["recent_changes"][:SHOWN]
        lines = [
            f"{c['id']} ({day(c['at'])}) {c['kind']} “{c['summary']}” on {c['service_id']}"
            for c in rows
        ]
        cites = [p["person"]["id"], *(x for c in rows for x in (c["id"], c["service_id"]))]
        head = f"Most recent changes by {who}"
    else:
        p = (await runner.run("service_profile", {"service_id": top["id"]})).result
        who = f"{p['service']['name']} ({p['service']['id']})"
        rows = p["recent_changes"][:SHOWN]
        lines = [
            f"{c['id']} ({day(c['at'])}) {c['kind']} “{c['summary']}” by {_who(c['author'])}"
            for c in rows
        ]
        cites = [p["service"]["id"]]
        cites += [x for c in rows for x in (c["id"], (c["author"] or {}).get("person_id"))]
        head = f"Most recent changes to {who}"
    if not rows:
        return Routed(f"No changes recorded for {who} as of {day(p['as_of'])}.", cites[:1])
    text = f"{head}: " + "; ".join(lines) + "." + _others(matches)
    return Routed(text, [c for c in cites if c])


async def _incidents(question: str, runner: ToolRunner) -> Routed:
    days = window_days(question)
    matches = await _find(runner, question, ["Service", "Team", "Person"])
    if matches is not None and not matches:
        return no_info(question)
    args: dict[str, Any] = {}
    if matches:
        args["entity_id"] = matches[0]["id"]
    if days is not None:
        args["days"] = days
    p = (await runner.run("incident_history", args)).result
    scope = f" on {matches[0]['id']}" if matches else ""
    window = f" in the last {days} days" if days else ""
    rows = p["incidents"][:SHOWN]
    if not rows:
        cites = [matches[0]["id"]] if matches else []
        text = f"No incidents{scope}{window} as of {day(p['as_of'])}."
        return Routed(text, cites, bool(cites))
    lines, cites = [], [m["id"] for m in (matches or [])[:1]]
    for i in rows:
        c = i["cause"]
        cause = (
            f"caused by {c['id']} ({c['kind']} “{c['summary']}”) by {_who(c['author'])}"
            if c
            else "cause not recorded"
        )
        lines.append(f"{i['id']} “{i['title']}” ({day(i['opened_at'])}): {cause}")
        cites += [i["id"], *([c["id"], (c["author"] or {}).get("person_id")] if c else [])]
    head = f"Incidents{scope}{window}, newest first"
    return Routed(f"{head}: " + "; ".join(lines) + ".", [c for c in cites if c])


async def _decisions(question: str, runner: ToolRunner) -> Routed:
    if INCIDENT_WORDS.search(question.lower()):
        incidents = await _find(runner, question, ["Incident"], limit=25)
        if incidents:
            latest = max(incidents, key=lambda m: (m["at"] or 0, m["id"]))
            p = (await runner.run("decisions_for", {"entity_id": latest["id"]})).result
            head = f"After {latest['id']} “{latest['label']}” ({day(latest['at'])})"
            return _decisions_text(head, p, [latest["id"]])
    matches = await _find(runner, question, ["Service", "Team"])
    if not matches:
        return no_info(question)
    top = matches[0]
    p = (await runner.run("decisions_for", {"entity_id": top["id"]})).result
    return _decisions_text(f"For {top['label']} ({top['id']})", p, [top["id"]])


def _decisions_text(head: str, p: dict, cites: list[str]) -> Routed:
    pms = p["postmortems"]
    if pms:
        head += ", postmortem " + ", ".join(pm["id"] for pm in pms)
        cites += [pm["id"] for pm in pms]
    parts = []
    recs = p["recommendations"][:SHOWN]
    if recs:
        items = "; ".join(f"“{r['text']}” ({r['id']}, {r['status']})" for r in recs)
        parts.append(f"recommended {items}")
        cites += [r["id"] for r in recs]
    adrs = p["adrs"][:SHOWN]
    if adrs:
        items = "; ".join(f"{a['title']} ({a['id']}, {day(a['decided_at'])})" for a in adrs)
        parts.append(f"decided {items}")
        cites += [a["id"] for a in adrs]
    if not parts:
        return Routed(f"{head}: no recommendations or ADRs are recorded.", cites)
    return Routed(f"{head} " + "; ".join(parts) + ".", cites)
