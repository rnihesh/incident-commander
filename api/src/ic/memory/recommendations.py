"""Open postmortem recommendations on the causal path (U13, R23).

A recommendation written after an earlier incident (``(:Postmortem)-[:RECOMMENDS]->
(:Recommendation)-[:TARGETS]->(:Service)``) that is still open and targets a service on
this incident's causal path "predicted" the incident: the console shows it beside the
verdict and the PR body and the page cite it.

A recommendation is open from ``opened_at`` when it carries one, otherwise from its source
incident's ``resolved_at`` (the postmortem is written at resolution), otherwise from the
incident's ``opened_at``. Status is current state (``open`` / ``closed``).
"""

from __future__ import annotations

from ic.graph.client import query

DAY = 86400

OPEN_ON_PATH = """
MATCH (r:Recommendation)-[:TARGETS]->(s:Service)
WHERE s.id IN $services AND coalesce(r.status, 'open') = 'open'
OPTIONAL MATCH (pm:Postmortem)-[:RECOMMENDS]->(r)
OPTIONAL MATCH (pm)-[:ABOUT]->(i:Incident)
WITH r, pm, i, collect(DISTINCT s.id) AS targets,
     coalesce(r.opened_at, i.resolved_at, i.opened_at) AS opened_at
WHERE opened_at IS NOT NULL AND opened_at <= $t
RETURN r.id AS id, r.text AS text, opened_at, pm.id AS postmortem_id, i.id AS incident_id,
       targets
ORDER BY opened_at, id
"""


def display_id(entity_id: str | None) -> str:
    """``rec-007`` -> ``REC-007``, ``inc-012`` -> ``INC-012``."""
    return (entity_id or "").upper()


def banner_text(rec: dict) -> str:
    """'Predicted by REC-x (from INC-y postmortem), open N days: <text>'."""
    src = f" (from {display_id(rec['incident_id'])} postmortem)" if rec.get("incident_id") else ""
    days = rec["days_open"]
    unit = "day" if days == 1 else "days"
    return f"Predicted by {display_id(rec['id'])}{src}, open {days} {unit}: {rec['text']}"


async def open_recommendations_on_path(
    graph: str, path_service_ids: list[str], t: int
) -> list[dict]:
    """Open recommendations (opened at or before ``t``) targeting any path service, with the
    days they have been open at ``t`` and their source postmortem and incident."""
    services = sorted({s for s in path_service_ids if s})
    if not services:
        return []
    rows = (
        await query(graph, OPEN_ON_PATH, {"services": services, "t": int(t)}, read_only=True)
    ).result_set
    out = []
    for rid, text, opened_at, pm, iid, targets in rows:
        rec = {
            "id": rid,
            "display_id": display_id(rid),
            "text": text or "",
            "opened_at": int(opened_at),
            "days_open": max(0, (int(t) - int(opened_at)) // DAY),
            "postmortem_id": pm,
            "incident_id": iid,
            "services": sorted(targets),
        }
        rec["banner"] = banner_text(rec)
        out.append(rec)
    return out


def narration_payload(recs: list[dict]) -> dict:
    """The ``narration`` event (topic 'recommendation') the console's banner reads."""
    if recs:
        text = recs[0]["banner"]
        if len(recs) > 1:
            text += f" (+{len(recs) - 1} more open recommendation(s) on the path)"
    else:
        text = "No open postmortem recommendation targets the causal path."
    return {"topic": "recommendation", "text": text, "recommendations": recs}


def pr_sections(recs: list[dict]) -> dict[str, str]:
    """``extra_sections`` for the rollback PR body."""
    if not recs:
        return {}
    return {"Predicted by open recommendations": " | ".join(r["banner"] for r in recs)}


def page_fields(recs: list[dict]) -> dict[str, str]:
    """``extra_fields`` for the Discord page."""
    if not recs:
        return {}
    return {"Predicted by": "; ".join(r["banner"] for r in recs)}
