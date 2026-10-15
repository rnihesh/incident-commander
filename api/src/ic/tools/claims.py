"""Claim check (R1, R2, KTD1): is a named entity the incident's root cause?

``verify_claim`` judges one claimed id with the agent's own predicate on the incident's
as-of snapshot: the windowed funnel (``predicate.screen``) supplies the rivals, and the
claim's own verdict comes from the same per-candidate query. It is read-only, so it never
creates proofs, ledger entries, memory edges or state transitions, and the MCP server
exposes it to external agents. The benchmark explanations and the judge challenge reuse it,
so all three surfaces agree by construction.

Verdicts:
  VERIFIED  the claim passes every clause and no other candidate qualifies
  RIVAL     the claim passes, but another candidate qualifies too
  REFUTED   the claim fails; ``clause`` names the first funnel step it fails
            (``outside_window`` or a predicate clause, or ``not_a_change`` for a Service)
            and ``query`` is the index of the query that decided it
"""

from __future__ import annotations

from pydantic import Field

from ic.tools import predicate as P
from ic.tools.investigate import WindowArgs
from ic.tools.registry import register
from ic.tools.result import QueryLog, ToolResult

CLAIM_LABELS = (*P.ENTITY_LABELS, "Service")


class ClaimArgs(WindowArgs):
    entity_id: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Claimed root cause: a Change or Expiry id (e.g. chg-123). A Service id is "
            "REFUTED with clause not_a_change; a change outside the alert window is REFUTED "
            "with clause outside_window"
        ),
    )


def _rival(v: P.Verdict) -> dict:
    return {
        "id": v.id,
        "entity": v.entity,
        "kind": v.kind,
        "service_id": v.service_id,
        "event_time": v.event_time,
    }


@register(ClaimArgs)
async def verify_claim(
    graph: str,
    incident_id: str,
    entity_id: str,
    window_minutes: int = P.DEFAULT_WINDOW_MINUTES,
) -> ToolResult:
    """Check a claimed root cause: VERIFIED (it qualifies and nothing else does), RIVAL (it
    qualifies but so does another candidate) or REFUTED (with the first clause it fails and
    the query that decided it). ``entity_id`` must be a change or expiry id: a Service id
    is REFUTED as not_a_change, and a change outside the alert window as outside_window.
    Read-only; the rule is the same one the funnel applies."""
    log = QueryLog(graph)
    s = await P.screen(log, incident_id, window_minutes)
    claim = next((v for v in s.verdicts if v.id == entity_id), None)
    rivals = [_rival(v) for v in s.survivors if v.id != entity_id]
    if claim is not None:
        clause, detail, query = claim.clause, claim.detail, claim.query
    else:
        label = await P.entity_label(log, entity_id, CLAIM_LABELS)
        if label == "Service":
            clause, query = "not_a_change", log.last_index
            detail = f"{entity_id} is a service; a root cause is a change or an expiry"
        else:
            claim = await P.explain(log, s.facts, entity_id, label)
            clause, query = "outside_window", s.candidates_query
            detail = f"event at {claim.event_time} is outside the alert window [{s.lo}, {s.hi}]"
    if clause is None:
        verdict = "RIVAL" if rivals else "VERIFIED"
    else:
        verdict = "REFUTED"
    result = {
        "incident_id": incident_id,
        "entity_id": entity_id,
        "verdict": verdict,
        "clause": clause,
        "detail": detail,
        "query": query,
        "query_text": log.cypher[query],
        "claim": claim.as_dict() if claim else None,
        "rivals": rivals,
        "window": {"from": s.lo, "to": s.hi, "minutes": window_minutes},
        "checked": [v.id for v in s.verdicts],
    }
    touched = [incident_id, entity_id, *s.facts.symptomatic_services, *(r["id"] for r in rivals)]
    return log.result("verify_claim", result, touched)
