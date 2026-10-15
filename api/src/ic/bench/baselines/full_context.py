"""Full-context baseline: the declared topology, the change log in the window and the
symptoms serialized into one prompt; no tools, no graph traversal.

The topology is the declared service catalog: DEPENDS_ON edges only. OBSERVED_CALL traffic
is runtime telemetry an engineer pasting the catalog into a chat would not have, so it is
left out on purpose. The prompt's size is reported as len(text)/4 tokens, and the prompt is
marked truncated when that exceeds the context limit (with an LLM the topology is cut to fit).

Without an LLM the prediction comes from a heuristic stand-in for an LLM without graph
tools: the most recent change or expiry before the first symptom on the alerting service or
one of its declared direct dependencies; failing that, the most recent one before the first
symptom anywhere; failing that, the most recent one in the window."""

from __future__ import annotations

from ic.bench.common import (
    BenchContext,
    CaseView,
    IncidentData,
    Prediction,
    ask_llm,
    est_tokens,
    iso,
    pick_id,
    read_declared,
    read_incident_data,
)

SYSTEM = (
    "You are an on-call engineer. Below are the declared service catalog (who calls whom), "
    "the change and expiry log for the alert window, and the incident's symptoms. Name the "
    "single change or expiry that caused the incident. Answer with its id (chg-... or exp-...) "
    "only."
)
HEURISTIC = (
    "heuristic stand-in for an LLM without graph tools: most recent change/expiry before the "
    "first symptom on the alert service or its declared direct dependencies"
)


def build_prompt(d: IncidentData, edges: list[tuple[str, str]], edge_limit: int | None = None):
    shown = edges if edge_limit is None else edges[:edge_limit]
    inc = d.incident
    parts = [
        "## Declared service catalog (caller -> callee, DEPENDS_ON)",
        *(f"{a} -> {b}" for a, b in shown),
        "",
        "## Change and expiry log (alert window)",
        "id | type | kind | service | time | summary",
        *(
            f"{c['id']} | {c['entity'].lower()} | {c['kind']} | {c['service_id']} | "
            f"{iso(c['event_time'])} | {c['summary'] or ''}"
            for c in d.candidates
        ),
        "",
        "## Incident",
        f"title: {inc['title']}",
        f"alert service: {inc['alert_service']}",
        f"opened at: {iso(inc['opened_at'])}",
        "symptoms (service | onset | signature):",
        *(f"{s['service_id']} | {iso(s['onset'])} | {s['signature']}" for s in d.symptoms),
    ]
    return "\n".join(parts)


def heuristic(d: IncidentData, edges: list[tuple[str, str]]) -> str | None:
    alert = d.incident["alert_service"]
    near = {alert} | {b for a, b in edges if a == alert}
    before = [c for c in d.candidates if c["event_time"] < d.earliest]
    for pool in ([c for c in before if c["service_id"] in near], before, d.candidates):
        if pool:
            return max(pool, key=lambda c: (c["event_time"], c["id"]))["id"]
    return None


async def full_context(case: CaseView, bctx: BenchContext) -> Prediction:
    d = await read_incident_data(case)
    if not d.candidates:
        return Prediction(None, notes={"reason": "no candidates in window"})
    edges = await read_declared(case.snapshot_known)
    prompt = build_prompt(d, edges)
    tokens = est_tokens(SYSTEM + prompt)
    truncated = tokens > bctx.context_limit
    notes = {"edges": len(edges), "n_candidates": len(d.candidates)}
    if bctx.llm is None:
        notes["mode"] = HEURISTIC
        return Prediction(
            heuristic(d, edges), prompt_tokens_est=tokens, truncated=truncated, notes=notes
        )
    if truncated:  # cut the catalog until the prompt fits; the model sees less topology
        keep = len(edges)
        while keep > 0 and est_tokens(SYSTEM + build_prompt(d, edges, keep)) > bctx.context_limit:
            keep = int(keep * 0.9)
        prompt = build_prompt(d, edges, keep)
        notes["edges_sent"] = keep
    content, used = await ask_llm(bctx.llm, SYSTEM, prompt)
    return Prediction(
        pick_id(content, [c["id"] for c in d.candidates]),
        llm_tokens=used,
        prompt_tokens_est=tokens,
        truncated=truncated,
        notes=notes,
    )
