"""Vector-RAG baseline: embed the incident's symptom text, every candidate change/expiry
(kind, service, summary) and postmortem chunks; rank by cosine similarity. No graph
traversal.

Without an LLM the prediction is the candidate chunk most similar to the symptom text
(postmortem chunks are in the corpus but cannot name a change in this incident's window).
With an LLM the top-k chunks of the mixed corpus are put in a prompt and the model names the
cause; only what retrieval surfaced is available to it."""

from __future__ import annotations

import asyncio

import numpy as np

from ic.bench.common import (
    BenchContext,
    CaseView,
    IncidentData,
    Prediction,
    ask_llm,
    est_tokens,
    iso,
    pick_id,
    read_incident_data,
    read_postmortems,
)

SYSTEM = (
    "You are an on-call engineer. Using only the retrieved context, name the single change "
    "or expiry that caused the incident. Answer with its id (chg-... or exp-...) only."
)


def symptom_text(d: IncidentData) -> str:
    parts = [f"{s['name']} {s['signature'].replace('_', ' ')}" for s in d.symptoms]
    return f"{d.incident['title']}. Symptoms: {'; '.join(parts)}"


def candidate_text(c: dict) -> str:
    return f"{c['kind']} on {c['name']}: {c['summary'] or ''}"


def postmortem_chunks(pms: list[dict]) -> list[tuple[str, str]]:
    """(chunk id, text): one chunk for title + symptoms, one for the body."""
    out = []
    for p in pms:
        out.append((f"{p['id']}#symptoms", f"{p['title']}. {p['symptoms_text'] or ''}"))
        out.append((f"{p['id']}#body", p["body"] or ""))
    return out


def build_prompt(d: IncidentData, top: list[tuple[str, str, float]]) -> str:
    lines = [
        f"Incident: {d.incident['title']} (alert on {d.incident['alert_service']}, "
        f"opened {iso(d.incident['opened_at'])})",
        "Symptoms: "
        + "; ".join(f"{s['service_id']} {s['signature']} at {iso(s['onset'])}" for s in d.symptoms),
        "",
        "Retrieved context:",
    ]
    lines += [f"[{cid}] {text}" for cid, text, _ in top]
    return "\n".join(lines)


async def vector_rag(case: CaseView, bctx: BenchContext) -> Prediction:
    d = await read_incident_data(case)
    if not d.candidates:
        return Prediction(None, notes={"reason": "no candidates in window"})
    pms = await read_postmortems(case.snapshot_known)
    corpus = [(c["id"], candidate_text(c)) for c in d.candidates] + postmortem_chunks(pms)
    embedder = bctx.get_embedder()
    query = symptom_text(d)
    vecs = await asyncio.to_thread(embedder.embed, [query, *(t for _, t in corpus)])
    m = np.asarray(vecs, dtype=float)
    norms = np.linalg.norm(m, axis=1)
    norms[norms == 0] = 1.0
    m = m / norms[:, None]
    sims = m[1:] @ m[0]
    order = sorted(range(len(corpus)), key=lambda i: (-round(float(sims[i]), 9), corpus[i][0]))
    top = [(corpus[i][0], corpus[i][1], float(sims[i])) for i in order[: bctx.top_k]]
    prompt = build_prompt(d, top)
    cand_ids = [c["id"] for c in d.candidates]
    best = next(corpus[i][0] for i in order if corpus[i][0] in set(cand_ids))
    notes = {
        "top_k": [cid for cid, _, _ in top],
        "candidates_in_top_k": sum(cid in set(cand_ids) for cid, _, _ in top),
        "n_candidates": len(cand_ids),
    }
    if bctx.llm is None:
        return Prediction(best, prompt_tokens_est=est_tokens(SYSTEM + prompt), notes=notes)
    content, tokens = await ask_llm(bctx.llm, SYSTEM, prompt)
    return Prediction(
        pick_id(content, cand_ids),
        llm_tokens=tokens,
        prompt_tokens_est=est_tokens(SYSTEM + prompt),
        notes=notes,
    )
