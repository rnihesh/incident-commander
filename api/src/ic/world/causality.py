"""Reference implementation of the KTD23 explanation predicate.

This is the ground-truth oracle: the Cypher tools built later must agree with it.
An *entity* is a Change (event time = valid_from) or an Expiry (event time = valid_to);
all it needs is ``id``, ``service_id``, ``event_time`` and ``sig_kind``.

Clauses, checked in this order (the first failure is reported):
  after_onset  the event precedes the earliest symptom onset
  origin       the entity's own service is symptomatic and shows the earliest onset
               (rules out healthy dependencies whose reach happens to cover the symptoms)
  coverage     every symptomatic service is reachable from the entity in IMPACTS
  order        onsets are consistent with hop distance (nearer hops start no later)
  signature    the entity's kind can produce every observed symptom signature
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import networkx as nx

from ic.graph.schema import COMPATIBLE_SIGNATURES

CLAUSES = ("after_onset", "origin", "coverage", "order", "signature")


class Entity(Protocol):
    id: str
    service_id: str
    event_time: int
    sig_kind: str


class SymptomLike(Protocol):
    service_id: str
    onset: int
    signature: str


@dataclass(frozen=True)
class Verdict:
    entity_id: str
    ok: bool
    clause: str | None
    detail: str


def qualifies(
    entity: Entity, g: nx.DiGraph, symptoms: list[SymptomLike]
) -> tuple[bool, str | None, str]:
    """Return (qualifies, first failing clause or None, human-readable detail)."""
    if not symptoms:
        return False, "coverage", "no symptoms to explain"
    earliest = min(s.onset for s in symptoms)
    if entity.event_time >= earliest:
        return False, "after_onset", f"event at {entity.event_time} is not before onset {earliest}"

    origin = [s for s in symptoms if s.service_id == entity.service_id]
    if not origin:
        return False, "origin", f"{entity.service_id} shows no symptom"
    if min(s.onset for s in origin) > earliest:
        return False, "origin", f"{entity.service_id} was not the first service to fail"

    hops = nx.single_source_shortest_path_length(g, entity.id) if entity.id in g else {}
    missing = sorted(s.service_id for s in symptoms if s.service_id not in hops)
    if missing:
        return False, "coverage", f"does not reach {', '.join(missing)}"

    ordered = sorted(symptoms, key=lambda s: hops[s.service_id])
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if hops[a.service_id] < hops[b.service_id] and a.onset > b.onset:
                return (
                    False,
                    "order",
                    f"{a.service_id} (hop {hops[a.service_id]}) started after "
                    f"{b.service_id} (hop {hops[b.service_id]})",
                )

    allowed = COMPATIBLE_SIGNATURES[entity.sig_kind]
    bad = sorted({s.signature for s in symptoms} - {str(a) for a in allowed})
    if bad:
        return False, "signature", f"{entity.sig_kind} cannot produce {', '.join(bad)}"
    return True, None, "all clauses hold"


def screen(entities: list[Entity], g: nx.DiGraph, symptoms: list[SymptomLike]) -> list[Verdict]:
    """Verdict for every candidate: the certificate's 'checked and disqualified' list."""
    return [Verdict(e.id, *qualifies(e, g, symptoms)) for e in entities]


def explaining_entities(
    entities: list[Entity], g: nx.DiGraph, symptoms: list[SymptomLike]
) -> list[Entity]:
    return [e for e in entities if qualifies(e, g, symptoms)[0]]


def necessity(
    suspect: Entity, entities: list[Entity], g: nx.DiGraph, symptoms: list[SymptomLike]
) -> tuple[bool, list[Entity]]:
    """Necessary = no rival qualifies once the suspect is removed. Returns (necessary, rivals)."""
    rivals = explaining_entities([e for e in entities if e.id != suspect.id], g, symptoms)
    return not rivals, rivals
