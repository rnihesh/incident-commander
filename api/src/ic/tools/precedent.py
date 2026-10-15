"""Precedent search (U8, R9, KTD22): which past incidents looked like this one?

Three independent methods, each reported with its own score so a reader can see *why* a
precedent surfaced:

  vector       cosine similarity (1 - distance) of the symptom text to postmortem embeddings
  fulltext     fuzzy (edit distance 1) full-text hits on postmortem title/body/symptoms
  fingerprint  path-shape similarity (see ``ic.tools.fingerprint``), prose-independent

Precedent is advisory (KTD4): it sits outside the proof chain and never feeds a verdict.
Every query goes through ``QueryLog`` so the UI can show exactly what was asked.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Any

from pydantic import Field

from ic.tools import fingerprint as F
from ic.tools.registry import ToolInput, register
from ic.tools.result import QueryLog, ToolError, ToolResult
from ic.world.embed import get_embedder
from ic.world.text import SIGNATURE_PHRASES

MAX_HOPS = 8  # bound for shortestPath over IMPACTS
FUZZY_MIN_LEN = 5  # words at least this long get edit-distance-1 matching
MAX_TERMS = 12
MAX_TERM_LEN = 32

SYMPTOMS = """
MATCH (i:Incident {id: $incident_id})-[:HAS]->(sym:Symptom)-[:ON]->(s:Service)
RETURN s.id AS service_id, s.name AS name, s.tier AS tier, sym.signature AS signature,
       sym.onset AS onset
ORDER BY onset, service_id, sym.id
"""

# Past incidents' symptoms (the current incident is never its own precedent).
PAST_SYMPTOMS = """
MATCH (i:Incident)-[:CAUSED_BY]->()
WHERE i.id <> $incident_id
MATCH (i)-[:HAS]->(sym:Symptom)-[:ON]->(s:Service)
RETURN i.id AS incident_id, s.id AS service_id, s.tier AS tier, sym.signature AS signature,
       sym.onset AS onset
ORDER BY incident_id, onset, service_id, sym.id
"""

# Cause of each past incident and the service it hits first.
PAST_CAUSES = """
MATCH (i:Incident)-[:CAUSED_BY]->(c:Change)-[:IMPACTS]->(h:Service)
WHERE i.id <> $incident_id
RETURN i.id AS incident_id, 'Change' AS label, c.kind AS kind, h.id AS home
UNION ALL
MATCH (i:Incident)-[:CAUSED_BY]->(c:Expiry)-[:IMPACTS]->(h:Service)
WHERE i.id <> $incident_id
RETURN i.id AS incident_id, 'Expiry' AS label, c.kind AS kind, h.id AS home
"""

SUSPECT = """
MATCH (c:Change {id: $suspect_id})-[:IMPACTS]->(h:Service)
RETURN 'Change' AS label, c.kind AS kind, h.id AS home
UNION ALL
MATCH (c:Expiry {id: $suspect_id})-[:IMPACTS]->(h:Service)
RETURN 'Expiry' AS label, c.kind AS kind, h.id AS home
"""

# Shortest IMPACTS path (and the tiers along it) for each (root, target) pair.
PATHS = f"""
UNWIND $pairs AS pr
MATCH (o:Service {{id: pr[0]}}), (t:Service {{id: pr[1]}})
WHERE o <> t
WITH pr, shortestPath((o)-[:IMPACTS*..{MAX_HOPS}]->(t)) AS p
RETURN pr[0] AS root, pr[1] AS target, length(p) AS hops, [n IN nodes(p) | n.tier] AS tiers
"""

VECTOR = """
CALL db.idx.vector.queryNodes('Postmortem', 'embedding', $k, vecf32($vector)) YIELD node, score
MATCH (node)-[:ABOUT]->(i:Incident)
WHERE i.id <> $incident_id
RETURN i.id AS incident_id, node.id AS postmortem_id, score
ORDER BY score, incident_id
LIMIT $keep
"""

FULLTEXT = """
CALL db.idx.fulltext.queryNodes('Postmortem', $query) YIELD node, score
MATCH (node)-[:ABOUT]->(i:Incident)
WHERE i.id <> $incident_id
RETURN i.id AS incident_id, node.id AS postmortem_id, score
ORDER BY score DESC, incident_id
LIMIT $keep
"""

DETAILS = """
MATCH (i:Incident)
WHERE i.id IN $ids
OPTIONAL MATCH (pm:Postmortem)-[:ABOUT]->(i)
OPTIONAL MATCH (i)-[:REMEDIATED_BY]->(r:Change)
RETURN i.id AS incident_id, pm.id AS postmortem_id, coalesce(pm.title, i.title) AS title,
       r.summary AS remediation, i.opened_at AS opened_at, i.resolved_at AS resolved_at
"""


class PrecedentArgs(ToolInput):
    incident_id: str = Field(min_length=1, max_length=128, description="Incident id, e.g. inc-007")
    k: int = Field(5, ge=1, le=50, description="How many precedents to return")
    suspect_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Suspected cause (Change or Expiry id); adds the causal path to the match",
    )


# --- query construction ----------------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def symptom_text(symptoms: list[dict]) -> str:
    """Text shaped like a postmortem's ``symptoms_text`` so the embeddings are comparable."""
    sigs = list(dict.fromkeys(s["signature"] for s in symptoms))
    phrase = " and ".join(SIGNATURE_PHRASES.get(s, [s.replace("_", " ")])[0] for s in sigs)
    names = list(dict.fromkeys(s["name"] for s in symptoms if s.get("name")))
    text = f"{phrase} on {names[0]}" if names else phrase
    return text + (f"; also {', '.join(names[1:5])}" if len(names) > 1 else "")


def fulltext_query(symptoms: list[dict]) -> str:
    """RediSearch union of fuzzy terms from signature words and service names.

    Only ``[a-z0-9]`` survives, so no query syntax can be smuggled in; terms are capped.
    """
    sources = [w for s in symptoms for w in _words(str(s["signature"]))]
    sources += [w for s in symptoms for w in _words(str(s.get("name") or ""))]
    terms = [w[:MAX_TERM_LEN] for w in dict.fromkeys(sources) if len(w) >= FUZZY_MIN_LEN]
    return "|".join(f"%{w}%" for w in terms[:MAX_TERMS])


# --- fingerprints ----------------------------------------------------------------------


async def _paths(log: QueryLog, pairs: list[list[str]]) -> dict[tuple[str, str], F.HopInfo]:
    if not pairs:
        return {}
    rows = await log.rows(PATHS, {"pairs": pairs})
    return {
        (root, target): F.HopInfo(hops, tuple(tiers))
        for root, target, hops, tiers in rows
        if hops is not None
    }


def _hops_from(
    root: str, root_tier: Any, targets: set[str], paths: dict[tuple[str, str], F.HopInfo]
) -> dict[str, F.HopInfo]:
    out = {t: paths[(root, t)] for t in targets if (root, t) in paths}
    if root in targets:
        out[root] = F.HopInfo(0, (root_tier,))
    return out


def shape_fingerprints(
    symptoms: list[dict],
    cause: tuple[str, str, str] | None,
    paths: dict[tuple[str, str], F.HopInfo],
) -> tuple[F.SymptomFingerprint, F.CauseFingerprint | None]:
    """Both fingerprints of one incident from its ordered symptoms and fetched paths.

    ``cause`` is (label, kind, home service id). The symptom root is the first service to
    fail; the cause root is the service the cause hits.
    """
    targets = {s["service_id"] for s in symptoms}
    origin = symptoms[0]
    sym_fp = F.symptom_fingerprint(
        symptoms, _hops_from(origin["service_id"], origin["tier"], targets, paths)
    )
    if cause is None:
        return sym_fp, None
    label, kind, home = cause
    tier_of = {s["service_id"]: s["tier"] for s in symptoms}
    hops = _hops_from(home, tier_of.get(home), targets, paths)
    deepest = max((h.hops for h in hops.values()), default=0)
    return sym_fp, F.cause_fingerprint(kind, symptoms, [label, *["Service"] * (deepest + 1)])


def _root_pairs(symptoms: list[dict], home: str | None) -> list[list[str]]:
    targets = sorted({s["service_id"] for s in symptoms})
    roots = {symptoms[0]["service_id"], *([home] if home else [])}
    return [[r, t] for r in sorted(roots) for t in targets if r != t]


def _group_symptoms(rows: list[list]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for iid, service_id, tier, signature, onset in rows:
        out[iid].append(
            {"service_id": service_id, "tier": tier, "signature": signature, "onset": onset}
        )
    return out


# --- the tool --------------------------------------------------------------------------


def _unavailable(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


@register(PrecedentArgs)
async def find_precedents(
    graph: str, incident_id: str, k: int = 5, suspect_id: str | None = None
) -> ToolResult:
    """Advisory precedent search: past incidents that look like this one, by vector, fuzzy
    full-text and path-shape fingerprint, each with its own score."""
    log = QueryLog(graph)
    cols = ("service_id", "name", "tier", "signature", "onset")
    symptoms = [
        dict(zip(cols, r, strict=True))
        for r in await log.rows(SYMPTOMS, {"incident_id": incident_id})
    ]
    if not symptoms:
        raise ToolError(f"incident {incident_id} has no symptoms in this snapshot")

    unavailable: dict[str, str] = {}
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    postmortem_of: dict[str, str] = {}
    by_method: dict[str, list[str]] = {"vector": [], "fulltext": [], "fingerprint": []}
    base = {"incident_id": incident_id, "keep": k}

    # 1. vector: distance -> similarity
    query_text = symptom_text(symptoms)
    embedder = get_embedder()
    vector = (await asyncio.to_thread(embedder.embed, [query_text]))[0]
    try:
        rows = await log.rows(VECTOR, {**base, "k": k + 1, "vector": vector})
        for iid, pm, distance in rows:
            scores[iid]["vector"] = round(1.0 - float(distance), 4)
            postmortem_of[iid] = pm
            by_method["vector"].append(iid)
    except Exception as e:  # no vector index / dimension mismatch: other methods still run
        unavailable["vector"] = _unavailable(e)

    # 2. fuzzy full-text
    ft_query = fulltext_query(symptoms)
    if ft_query:
        try:
            for iid, pm, score in await log.rows(FULLTEXT, {**base, "query": ft_query}):
                scores[iid]["fulltext"] = round(float(score), 4)
                postmortem_of[iid] = pm
                by_method["fulltext"].append(iid)
        except Exception as e:  # no full-text index
            unavailable["fulltext"] = _unavailable(e)

    # 3. fingerprint
    cause_now: tuple[str, str, str] | None = None
    if suspect_id:
        found = await log.rows(SUSPECT, {"suspect_id": suspect_id})
        if not found:
            raise ToolError(f"{suspect_id} is not a Change or Expiry with IMPACTS in this snapshot")
        cause_now = tuple(found[0])  # type: ignore[assignment]
    past_symptoms = _group_symptoms(await log.rows(PAST_SYMPTOMS, {"incident_id": incident_id}))
    past_causes = {
        r[0]: (r[1], r[2], r[3]) for r in await log.rows(PAST_CAUSES, {"incident_id": incident_id})
    }
    pairs = _root_pairs(symptoms, cause_now[2] if cause_now else None)
    for iid, syms in past_symptoms.items():
        if iid in past_causes:
            pairs += _root_pairs(syms, past_causes[iid][2])
    paths = await _paths(log, [list(p) for p in dict.fromkeys(map(tuple, pairs))])

    now_sym, now_cause = shape_fingerprints(symptoms, cause_now, paths)
    fp_detail: dict[str, dict[str, Any]] = {}
    for iid, syms in past_symptoms.items():
        if iid not in past_causes:
            continue
        past_sym, past_cause = shape_fingerprints(syms, past_causes[iid], paths)
        sym_sim = F.symptom_similarity(now_sym, past_sym)
        cause_sim = F.cause_similarity(now_cause, past_cause) if now_cause and past_cause else None
        score = cause_sim if cause_sim is not None else sym_sim
        fp_detail[iid] = {"symptom_similarity": sym_sim, "cause_similarity": cause_sim}
        if F.matches(score):
            scores[iid]["fingerprint"] = score
            by_method["fingerprint"].append(iid)
    by_method["fingerprint"].sort(key=lambda i: (-scores[i]["fingerprint"], i))
    by_method["fingerprint"] = by_method["fingerprint"][:k]
    for iid in set(fp_detail) - set(by_method["fingerprint"]):
        scores[iid].pop("fingerprint", None)

    # merge: more methods first, then the mean of the (fulltext-normalised) scores
    top_ft = max((s["fulltext"] for s in scores.values() if "fulltext" in s), default=1.0) or 1.0

    def combined(iid: str) -> float:
        s = scores[iid]
        parts = [s[m] / (top_ft if m == "fulltext" else 1.0) for m in s]
        return sum(parts) / len(parts)

    ids = sorted(
        (i for i in scores if scores[i]),
        key=lambda i: (-len(scores[i]), -combined(i), i),
    )[:k]
    details = {r[0]: r for r in (await log.rows(DETAILS, {"ids": ids}) if ids else [])}
    precedents = []
    for iid in ids:
        _, pm, title, remediation, opened, resolved = details.get(
            iid, (iid, None, None, None, None, None)
        )
        precedents.append(
            {
                "incident_id": iid,
                "postmortem_id": pm or postmortem_of.get(iid),
                "title": title,
                "methods": {m: scores[iid].get(m) for m in ("vector", "fulltext", "fingerprint")},
                "fingerprint_detail": fp_detail.get(iid),
                "remediation": remediation,
                "resolved_minutes": (
                    (resolved - opened) // 60
                    if opened is not None and resolved is not None
                    else None
                ),
            }
        )
    result = {
        "advisory": True,
        "incident_id": incident_id,
        "suspect_id": suspect_id,
        "precedents": precedents,
        "by_method": by_method,
        "query": {"text": query_text, "fulltext": ft_query},
        "unavailable": unavailable,
    }
    touched = [incident_id, suspect_id, *ids, *(p["postmortem_id"] for p in precedents)]
    touched += [s["service_id"] for s in symptoms]
    return log.result("find_precedents", result, touched)
