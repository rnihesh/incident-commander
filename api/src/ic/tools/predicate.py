"""The KTD23 explanation predicate, in Cypher, on a materialized snapshot graph.

This must agree with the pure-Python oracle in ``ic.world.causality`` (tests enforce it).

Candidates for an incident are the entities whose event falls in the alert window
``[earliest_onset - window, snapshot_t]``: Changes by ``valid_from``, Expiries by
``valid_to``. The window is a pre-filter (funnel step ``outside_window``); the oracle's
clauses then decide, in order, using one query per candidate:

  after_onset  the event precedes the earliest symptom onset
  origin       the candidate's own service is symptomatic and among the first to fail
  coverage     every symptomatic service is reachable from it over IMPACTS
  order        nearer hops start no later (hop(a) < hop(b) implies onset(a) <= onset(b))
  signature    its kind can produce every observed signature (COMPATIBLE_SIGNATURES)

The per-candidate query reads the symptoms from the graph itself, so re-running the
stored query on a tampered or re-built snapshot re-derives the verdict from scratch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ic.graph.schema import COMPATIBLE_SIGNATURES, OPEN_END
from ic.tools.result import QueryLog, ToolError
from ic.world.causality import CLAUSES

FUNNEL_STEPS = ("outside_window", *CLAUSES)
ENTITY_LABELS = ("Change", "Expiry")
DEFAULT_WINDOW_MINUTES = 120

# kind -> signatures it can produce, as a plain map parameter for Cypher.
COMPATIBLE = {str(k): sorted(str(s) for s in v) for k, v in COMPATIBLE_SIGNATURES.items()}

INCIDENT = """
MATCH (i:Incident {id: $incident_id})
OPTIONAL MATCH (m:SnapshotMeta)
RETURN i.id AS incident_id, i.opened_at AS opened_at, max(m.t) AS snapshot_t
"""

SYMPTOMS = """
MATCH (:Incident {id: $incident_id})-[:HAS]->(sym:Symptom)-[:ON]->(s:Service)
RETURN sym.id AS symptom_id, s.id AS service_id, sym.onset AS onset, sym.signature AS signature
ORDER BY onset, service_id, symptom_id
"""

# Every Change/Expiry whose event time is inside [lo, hi]; optionally restricted to a set
# of services and with some ids excluded (necessity removes the suspect this way).
CANDIDATES = """
MATCH (c:Change)-[:CHANGES]->(s:Service)
WHERE $lo <= c.valid_from AND c.valid_from <= $hi
  AND ($services IS NULL OR s.id IN $services) AND NOT c.id IN $exclude
RETURN c.id AS id, 'Change' AS entity, c.kind AS kind, s.id AS service_id,
       c.valid_from AS event_time, c.summary AS summary
UNION ALL
MATCH (e:Expiry)-[:EXPIRES_ON]->(s:Service)
WHERE $lo <= e.valid_to AND e.valid_to <= $hi
  AND ($services IS NULL OR s.id IN $services) AND NOT e.id IN $exclude
RETURN e.id AS id, 'Expiry' AS entity, e.kind AS kind, s.id AS service_id,
       e.valid_to AS event_time, e.summary AS summary
"""

# The predicate for one candidate. {label} is Change or Expiry (from ENTITY_LABELS only).
EXPLAINS = """
MATCH (c:{label} {{id: $entity_id}})-[:CHANGES|EXPIRES_ON]->(home:Service)
MATCH (:Incident {{id: $incident_id}})-[:HAS]->(sym:Symptom)-[:ON]->(s:Service)
WITH c, home, sym, s, shortestPath((c)-[:IMPACTS*]->(s)) AS path
WITH c, home,
     CASE WHEN c:Expiry THEN c.valid_to ELSE c.valid_from END AS event_time,
     collect({{service: s.id, onset: sym.onset, signature: sym.signature,
              hop: length(path)}}) AS syms
WITH c, home, event_time, syms,
     reduce(m = null, x IN syms |
            CASE WHEN m IS NULL OR x.onset < m THEN x.onset ELSE m END) AS earliest
WITH c, home, event_time, syms, earliest,
     any(x IN syms WHERE x.service = home.id AND x.onset = earliest) AS home_fails_first,
     [x IN syms WHERE x.hop IS NULL | x.service] AS unreached,
     [a IN syms WHERE any(b IN syms WHERE a.hop < b.hop AND a.onset > b.onset)] AS out_of_order,
     [x IN syms WHERE NOT x.signature IN coalesce($compatible[c.kind], []) | x.signature]
       AS bad_signatures
RETURN c.id AS id, c.kind AS kind, home.id AS service_id, event_time, earliest, syms,
       unreached, bad_signatures,
       CASE
         WHEN event_time >= earliest THEN 'after_onset'
         WHEN NOT home_fails_first THEN 'origin'
         WHEN size(unreached) > 0 THEN 'coverage'
         WHEN size(out_of_order) > 0 THEN 'order'
         WHEN size(bad_signatures) > 0 THEN 'signature'
       END AS failed_clause
"""

LABEL_OF = """
MATCH (n:Change {id: $id}) RETURN 'Change' AS label
UNION ALL MATCH (n:Expiry {id: $id}) RETURN 'Expiry' AS label
UNION ALL MATCH (n:Service {id: $id}) RETURN 'Service' AS label
"""


@dataclass
class IncidentFacts:
    incident_id: str
    opened_at: int | None
    snapshot_t: int | None
    symptoms: list[dict]

    @property
    def earliest_onset(self) -> int | None:
        return min((s["onset"] for s in self.symptoms), default=None)

    @property
    def symptomatic_services(self) -> list[str]:
        return sorted({s["service_id"] for s in self.symptoms})

    def window(self, window_minutes: int) -> tuple[int, int]:
        """[earliest onset - window, snapshot time]; no upper bound without SnapshotMeta."""
        earliest = self.earliest_onset
        if earliest is None:
            raise ToolError(f"incident {self.incident_id} has no symptoms in this snapshot")
        hi = self.snapshot_t if self.snapshot_t is not None else OPEN_END
        return earliest - window_minutes * 60, hi


@dataclass
class Candidate:
    id: str
    entity: str  # Change | Expiry
    kind: str
    service_id: str
    event_time: int
    summary: str | None = None


@dataclass
class Verdict:
    id: str
    entity: str
    kind: str
    service_id: str | None
    event_time: int | None
    ok: bool
    clause: str | None  # first failing clause, None when every clause holds
    detail: str
    hops: dict[str, int | None] = field(default_factory=dict)  # symptomatic service -> hop
    query: int = -1  # index of the EXPLAINS query in the tool's cypher list

    def as_dict(self) -> dict:
        return asdict(self)


async def incident_facts(log: QueryLog, incident_id: str) -> IncidentFacts:
    """The incident and its symptoms as seen in this snapshot; ToolError if absent."""
    rows = await log.rows(INCIDENT, {"incident_id": incident_id})
    if not rows:
        raise ToolError(f"incident {incident_id} is not in this snapshot")
    _, opened_at, snapshot_t = rows[0]
    sym_rows = await log.rows(SYMPTOMS, {"incident_id": incident_id})
    symptoms = [
        {"symptom_id": r[0], "service_id": r[1], "onset": r[2], "signature": r[3]} for r in sym_rows
    ]
    return IncidentFacts(incident_id, opened_at, snapshot_t, symptoms)


async def candidates_in_window(
    log: QueryLog,
    lo: int,
    hi: int,
    *,
    services: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Candidate]:
    params = {"lo": lo, "hi": hi, "services": services, "exclude": exclude or []}
    rows = await log.rows(CANDIDATES, params)
    cands = [Candidate(*r) for r in rows]
    return sorted(cands, key=lambda c: (c.event_time, c.id))


async def entity_label(log: QueryLog, entity_id: str, allowed: tuple[str, ...]) -> str:
    """Which of ``allowed`` labels carries ``entity_id`` (indexed lookups, no scans)."""
    rows = await log.rows(LABEL_OF, {"id": entity_id})
    labels = [r[0] for r in rows if r[0] in allowed]
    if not labels:
        raise ToolError(f"no {' or '.join(allowed)} with id {entity_id} in this snapshot")
    return labels[0]


async def explain(log: QueryLog, facts: IncidentFacts, entity_id: str, label: str) -> Verdict:
    """Run the KTD23 predicate for one Change/Expiry against the incident's symptoms."""
    if label not in ENTITY_LABELS:
        raise ToolError(f"{entity_id} is a {label}; only changes and expiries can explain")
    params = {"entity_id": entity_id, "incident_id": facts.incident_id, "compatible": COMPATIBLE}
    rows = await log.rows(EXPLAINS.format(label=label), params)
    q = log.last_index
    if not facts.symptoms:
        return Verdict(entity_id, label, "", None, None, False, "coverage", "no symptoms", query=q)
    if not rows:
        detail = f"{entity_id} is not attached to any service"
        return Verdict(entity_id, label, "", None, None, False, "origin", detail, query=q)
    eid, kind, home, event_time, earliest, syms, unreached, bad_sigs, clause = rows[0]
    hops = {x["service"]: x["hop"] for x in sorted(syms, key=lambda x: x["service"])}
    detail = _detail(clause, kind, home, event_time, earliest, syms, unreached, bad_sigs)
    return Verdict(eid, label, kind, home, event_time, clause is None, clause, detail, hops, q)


def _detail(clause, kind, home, event_time, earliest, syms, unreached, bad_sigs) -> str:
    """Human-readable reason, phrased like the oracle's."""
    if clause is None:
        return "all clauses hold"
    if clause == "after_onset":
        return f"event at {event_time} is not before onset {earliest}"
    if clause == "origin":
        if not any(x["service"] == home for x in syms):
            return f"{home} shows no symptom"
        return f"{home} was not the first service to fail"
    if clause == "coverage":
        return f"does not reach {', '.join(sorted(unreached))}"
    if clause == "order":
        ordered = sorted(syms, key=lambda x: (x["hop"], x["onset"], x["service"]))
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                if a["hop"] < b["hop"] and a["onset"] > b["onset"]:
                    return (
                        f"{a['service']} (hop {a['hop']}) started after "
                        f"{b['service']} (hop {b['hop']})"
                    )
    if clause == "signature":
        return f"{kind} cannot produce {', '.join(sorted(set(bad_sigs)))}"
    return clause


@dataclass
class Screening:
    """Everything the funnel, certificate and necessity views are built from."""

    facts: IncidentFacts
    lo: int
    hi: int
    candidates: list[Candidate]
    verdicts: list[Verdict]
    candidates_query: int

    @property
    def survivors(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.ok]


async def screen(
    log: QueryLog,
    incident_id: str,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    *,
    services: list[str] | None = None,
    exclude: list[str] | None = None,
) -> Screening:
    """Predicate verdict for every windowed candidate, in event-time order."""
    facts = await incident_facts(log, incident_id)
    lo, hi = facts.window(window_minutes)
    cands = await candidates_in_window(log, lo, hi, services=services, exclude=exclude)
    q = log.last_index
    verdicts = [await explain(log, facts, c.id, c.entity) for c in cands]
    return Screening(facts, lo, hi, cands, verdicts, q)


async def run_predicate(
    graph: str, incident_id: str, window_minutes: int = DEFAULT_WINDOW_MINUTES
) -> dict:
    """Convenience entry point: per-candidate verdicts plus every Cypher and its params."""
    log = QueryLog(graph)
    s = await screen(log, incident_id, window_minutes)
    return {
        "incident_id": incident_id,
        "window": {"from": s.lo, "to": s.hi},
        "verdicts": [v.as_dict() for v in s.verdicts],
        "cypher": log.cypher,
        "params": log.params,
    }
