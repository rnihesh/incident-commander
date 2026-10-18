"""The visitor's own incident, read back from the proof ledger (Ask the graph).

``incident_proofs`` answers "why did you blame chg-05689?" and "what got ruled out?" from
what the investigation stored: every hypothesis it registered for the incident, whether its
proof HOLDS or was REFUTED, and the first clause of the explanation rule each ruled-out
change fails. Read-only, on the session's ledger graph only, for the incident the server put
in the context (the model never names a graph or another session's incident).
"""

from __future__ import annotations

import json

from pydantic import Field

from ic.agent.payloads import CLAUSE_LABELS
from ic.tools.registry import ToolContext, ToolInput, register
from ic.tools.result import QueryLog, ToolError, ToolResult

PROOFS = """
MATCH (p:Proof)-[:TESTS]->(h:Hypothesis {incident_id: $incident_id})
RETURN h.seq, h.kind, h.entity_id, h.state, h.claim, p.conclusion, p.hash
ORDER BY h.seq
"""

# What each kind of proof establishes, in plain words.
KIND_MEANING = {
    "suspect_qualifies": "the change passes every clause of the explanation rule",
    "suspect_necessary": "with the change removed, no other change explains the symptoms",
    "no_other_qualifying": "no other change upstream of the symptoms qualifies",
    "entity_refuted": "the change fails at least one clause of the explanation rule",
}

RULE = (
    "A change explains the incident only if it passes every clause: "
    + "; ".join(f"{k}: {v.lower()}" for k, v in CLAUSE_LABELS.items())
    + "."
)


class IncidentProofsArgs(ToolInput):
    entity_id: str | None = Field(
        None,
        max_length=128,
        description="Only the proofs about this change id (null = every proof of the incident)",
    )


def _conclusion(raw: object) -> dict:
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}
    return {}


@register(IncidentProofsArgs, target="ledger", with_context=True)
async def incident_proofs(
    graph: str, *, ctx: ToolContext, entity_id: str | None = None
) -> ToolResult:
    """The incident you are looking at: the change the investigation blamed and why, and every
    change it ruled out with the first clause of the explanation rule that change fails. Read
    from the stored, hash-chained proofs of the investigation."""
    iid = ctx.iid
    if not iid:
        raise ToolError("no incident is open in this conversation")
    log = QueryLog(graph)
    rows = await log.rows(PROOFS, {"incident_id": iid})
    proofs = []
    for seq, kind, entity, state, claim, conclusion, h in rows:
        c = _conclusion(conclusion)
        clause = c.get("clause") if kind == "entity_refuted" else None
        proofs.append(
            {
                "seq": seq,
                "kind": kind,
                "meaning": KIND_MEANING.get(kind, kind),
                "change_id": entity,
                "state": state,
                "claim": claim,
                "failed_clause": clause,
                "failed_clause_means": CLAUSE_LABELS.get(clause) if clause else None,
                "hash": h,
            }
        )
    holds = {(p["kind"], p["change_id"]) for p in proofs if p["state"] == "HOLDS"}
    blamed = [
        p["change_id"]
        for p in proofs
        if p["kind"] == "suspect_qualifies"
        and ("suspect_necessary", p["change_id"]) in holds
        and p["state"] == "HOLDS"
    ]
    ruled_out = [
        {
            "change_id": p["change_id"],
            "failed_clause": p["failed_clause"],
            "failed_clause_means": p["failed_clause_means"],
        }
        for p in proofs
        if p["kind"] == "entity_refuted" and p["state"] == "HOLDS"
    ]
    if entity_id:
        proofs = [p for p in proofs if p["change_id"] == entity_id]
    result = {
        "incident_id": iid,
        "blamed": blamed[0] if len(blamed) == 1 else None,
        "candidates_left": blamed,
        "ruled_out": ruled_out,
        "proofs": proofs,
        "explanation_rule": RULE,
    }
    touched = [iid, *blamed, *(r["change_id"] for r in ruled_out)]
    touched += [p["change_id"] for p in proofs]
    return log.result("incident_proofs", result, touched)
