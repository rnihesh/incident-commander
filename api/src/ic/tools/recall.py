"""Recall (U13, KTD8, F3): what memory says about an incident before anything is screened.

``recall(incident_id)`` reads the as-of snapshot and returns, for the incident's symptomatic
services:

  learned     LEARNED_DEPENDS_ON edges (written back after earlier resolutions) touching a
              symptomatic service, with their confidence, confirmations and contradictions;
  precedents  past resolved incidents whose symptom fingerprint matches this one (path
              shape, see ``ic.tools.fingerprint``) and the service their cause hit;
  seeds       candidate changes / expiries in the alert window on the services memory
              points at, ranked: the hypotheses worth testing first.

Memory only orders the investigation. Recall is advisory like precedent search (KTD4): it is
never a falsifier, its edges never become IMPACTS, and a seeded suspect still needs every
proof the verdict gate asks for. Learned edges are read here and nowhere else in the tools.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import Field

from ic.graph.schema import OPEN_END
from ic.tools import fingerprint as F
from ic.tools import precedent as PR
from ic.tools import predicate as P
from ic.tools.registry import ToolInput, register
from ic.tools.result import QueryLog, ToolError, ToolResult

MIN_CONFIDENCE = 0.25  # learned edges below this no longer seed hypotheses
MAX_SEEDS = 5

SNAPSHOT_T = "OPTIONAL MATCH (m:SnapshotMeta) RETURN max(m.t)"

# Learned couplings touching a symptomatic service, learned no later than the snapshot.
LEARNED = """
MATCH (a:Service)-[l:LEARNED_DEPENDS_ON]->(b:Service)
WHERE (a.id IN $symptomatic OR b.id IN $symptomatic)
  AND ($t IS NULL OR coalesce(l.first_learned_at, 0) <= $t)
RETURN a.id AS caller, b.id AS callee, l.confidence AS confidence,
       coalesce(l.confirmations, 0) AS confirmations,
       coalesce(l.contradictions, 0) AS contradictions,
       l.first_learned_incident AS first_learned_incident, l.updated_at AS updated_at
ORDER BY confidence DESC, caller, callee
"""

# Past incidents with a recorded cause, and the service that cause hits first.
PAST_CAUSES = """
MATCH (i:Incident)-[:CAUSED_BY]->(c:Change)-[:CHANGES]->(h:Service)
WHERE i.id <> $incident_id
RETURN i.id AS incident_id, c.id AS cause_id, 'Change' AS label, c.kind AS kind, h.id AS home
UNION ALL
MATCH (i:Incident)-[:CAUSED_BY]->(c:Expiry)-[:EXPIRES_ON]->(h:Service)
WHERE i.id <> $incident_id
RETURN i.id AS incident_id, c.id AS cause_id, 'Expiry' AS label, c.kind AS kind, h.id AS home
"""


class RecallArgs(ToolInput):
    incident_id: str = Field(min_length=1, max_length=128, description="Incident id, e.g. inc-007")


def _seed_rank(seed: dict) -> tuple:
    return (not seed["plausible"], -seed["score"], -seed["event_time"], seed["entity_id"])


@register(RecallArgs)
async def recall(graph: str, incident_id: str) -> ToolResult:
    """Memory for this incident: learned dependencies and matching past incidents for its
    symptomatic services, and ranked seed suspects to test first. Advisory only: memory
    orders hypotheses, it never proves one."""
    log = QueryLog(graph)
    cols = ("service_id", "name", "tier", "signature", "onset")
    symptoms = [
        dict(zip(cols, r, strict=True))
        for r in await log.rows(PR.SYMPTOMS, {"incident_id": incident_id})
    ]
    if not symptoms:
        raise ToolError(f"incident {incident_id} has no symptoms in this snapshot")
    t = (await log.rows(SNAPSHOT_T))[0][0]
    symptomatic = sorted({s["service_id"] for s in symptoms})
    earliest = min(s["onset"] for s in symptoms)
    first_to_fail = sorted({s["service_id"] for s in symptoms if s["onset"] == earliest})
    signatures = sorted({str(s["signature"]) for s in symptoms})
    sym_set = set(symptomatic)

    # 1. learned couplings
    learned = []
    for caller, callee, conf, n_ok, n_bad, first_inc, updated in await log.rows(
        LEARNED, {"symptomatic": symptomatic, "t": t}
    ):
        learned.append(
            {
                "caller": caller,
                "callee": callee,
                "confidence": round(float(conf or 0.0), 4),
                "confirmations": int(n_ok),
                "contradictions": int(n_bad),
                "first_learned_incident": first_inc,
                "updated_at": updated,
                # both ends hurt: the learned edge already explains the hop between them,
                # so no observed-traffic discovery is needed to connect them
                "explains_hop": caller in sym_set and callee in sym_set,
            }
        )

    # 2. precedent fingerprints (symptom shape only: no suspect yet)
    past_symptoms = PR._group_symptoms(
        await log.rows(PR.PAST_SYMPTOMS, {"incident_id": incident_id})
    )
    past_causes: dict[str, tuple[str, str, str, str]] = {}
    for iid, cause_id, label, kind, home in await log.rows(
        PAST_CAUSES, {"incident_id": incident_id}
    ):
        past_causes.setdefault(iid, (cause_id, label, kind, home))
    pairs = PR._root_pairs(symptoms, None)
    for iid, syms in past_symptoms.items():
        if iid in past_causes:
            pairs += PR._root_pairs(syms, past_causes[iid][3])
    paths = await PR._paths(log, [list(p) for p in dict.fromkeys(map(tuple, pairs))])
    now_fp, _ = PR.shape_fingerprints(symptoms, None, paths)
    precedents = []
    for iid, syms in sorted(past_symptoms.items()):
        if iid not in past_causes:
            continue
        past_fp, _ = PR.shape_fingerprints(syms, None, paths)
        sim = F.symptom_similarity(now_fp, past_fp)
        if F.matches(sim):
            cause_id, label, kind, home = past_causes[iid]
            precedents.append(
                {
                    "incident_id": iid,
                    "cause_id": cause_id,
                    "cause_kind": kind,
                    "cause_service": home,
                    "similarity": sim,
                }
            )
    precedents.sort(key=lambda p: (-p["similarity"], p["incident_id"]))

    # 3. suspect services memory points at: the callee of a learned edge into a symptomatic
    #    caller, or the cause service of a matching precedent. Only symptomatic services can
    #    pass the origin clause, so others are not worth a hypothesis.
    score: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)
    for e in learned:
        if e["caller"] in sym_set and e["confidence"] >= MIN_CONFIDENCE:
            svc = e["callee"]
            score[svc] = max(score[svc], e["confidence"])
            reasons[svc].append(
                f"learned {e['caller']} -> {svc} (confidence {e['confidence']:.2f}, "
                f"{e['confirmations']} confirmation(s), from {e['first_learned_incident']})"
            )
    for p in precedents:
        svc = p["cause_service"]
        score[svc] = max(score[svc], 0.5 * p["similarity"])
        reasons[svc].append(
            f"precedent {p['incident_id']} (fingerprint {p['similarity']:.2f}) was caused on {svc}"
        )
    services = sorted((s for s in score if s in sym_set), key=lambda s: (-score[s], s))

    seeds: list[dict] = []
    if services:
        lo = earliest - P.DEFAULT_WINDOW_MINUTES * 60
        hi = t if t is not None else OPEN_END
        for c in await P.candidates_in_window(log, lo, hi, services=services):
            fits = set(signatures) <= set(P.COMPATIBLE.get(str(c.kind), []))
            before = c.event_time < earliest
            origin = c.service_id in first_to_fail
            seeds.append(
                {
                    "entity_id": c.id,
                    "entity": c.entity,
                    "kind": c.kind,
                    "service_id": c.service_id,
                    "event_time": c.event_time,
                    "summary": c.summary,
                    "score": round(score[c.service_id], 4),
                    # cheap pre-checks for ordering only; the proofs decide
                    "plausible": fits and before and origin,
                    "reasons": reasons[c.service_id],
                }
            )
        seeds.sort(key=_seed_rank)
        seeds = seeds[:MAX_SEEDS]

    result = {
        "advisory": True,
        "incident_id": incident_id,
        "symptomatic": symptomatic,
        "first_to_fail": first_to_fail,
        "learned": learned,
        "precedents": precedents,
        "suspect_services": services,
        "seeds": seeds,
        "used": bool(seeds),
        "note": "Memory ranks hypotheses only; every verdict still needs its proofs.",
    }
    touched = [incident_id, *symptomatic, *(s["entity_id"] for s in seeds)]
    touched += [p["incident_id"] for p in precedents]
    return log.result("recall", result, touched)
