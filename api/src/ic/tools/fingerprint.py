"""Path-shape fingerprints for precedent search (U8, KTD4).

Two incidents "look alike" when their *shape* matches, whatever the service names or the
prose say. Two fingerprints exist:

* symptom fingerprint: what the pager shows (signature set, how many services hurt, how far
  the damage spread, which tiers it climbed through);
* cause fingerprint: the same plus the suspected cause (change / expiry kind), the hop count
  from the cause and the label sequence of the causal path.

Both are pure values with an exact-equality test and a similarity in [0, 1]. Similarity is
gated: different signature sets (or, for cause fingerprints, a different cause kind) score 0,
so equal path length alone can never produce a match.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MATCH_THRESHOLD = 0.75  # similarity at or above which two shapes count as "the same"

_BUCKET_EDGES = (1, 2, 4)  # 1 | 2 | 3-4 | 5+


@dataclass(frozen=True)
class HopInfo:
    """How far a symptomatic service sits from the root, and the tiers on the way there."""

    hops: int
    tiers: tuple[int, ...] = ()  # tier of every service on the shortest path, root first


@dataclass(frozen=True)
class SymptomFingerprint:
    signatures: tuple[str, ...]  # sorted distinct signatures
    count_bucket: int  # 0: one symptomatic service, 1: two, 2: three-four, 3: five or more
    max_hop: int
    tier_seq: tuple[int, ...]  # tiers along the deepest chain


@dataclass(frozen=True)
class CauseFingerprint:
    cause_kind: str  # deploy | config | flag | cert | token | quota
    signatures: tuple[str, ...]
    hops: int  # cause -> deepest symptomatic service
    labels: tuple[str, ...]  # node labels along the deepest causal path, cause first


def count_bucket(n: int) -> int:
    return sum(n > edge for edge in _BUCKET_EDGES)


def _signatures(symptoms: Sequence[Mapping]) -> tuple[str, ...]:
    return tuple(sorted({str(s["signature"]) for s in symptoms}))


def symptom_fingerprint(
    symptoms: Sequence[Mapping], hop_by_service: Mapping[str, HopInfo]
) -> SymptomFingerprint:
    """Shape of an incident's symptoms; ``symptoms`` need ``service_id`` and ``signature``.

    Services missing from ``hop_by_service`` (unreachable from the root) do not count towards
    depth. The deepest chain is the one to the farthest service, smallest service id on ties.
    """
    services = sorted({str(s["service_id"]) for s in symptoms})
    reached = [hop_by_service[svc] for svc in services if svc in hop_by_service]
    if reached:
        deepest = max(reached, key=lambda h: h.hops)  # max() keeps the first (smallest id)
        max_hop, tiers = deepest.hops, deepest.tiers
    else:
        max_hop, tiers = 0, ()
    return SymptomFingerprint(_signatures(symptoms), count_bucket(len(services)), max_hop, tiers)


def cause_fingerprint(
    cause_kind: str, symptoms: Sequence[Mapping], path_labels: Sequence[str]
) -> CauseFingerprint:
    """Shape of the cause: its kind, the symptom signatures and the deepest causal path."""
    labels = tuple(path_labels)
    return CauseFingerprint(cause_kind, _signatures(symptoms), max(len(labels) - 1, 0), labels)


def edit_distance(a: Sequence, b: Sequence) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def seq_similarity(a: Sequence, b: Sequence) -> float:
    """1 - normalised Levenshtein distance; two empty sequences are identical."""
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1.0 - edit_distance(a, b) / longest


def _closeness(a: int, b: int) -> float:
    return 1.0 - abs(a - b) / max(a, b, 1)


def symptom_similarity(a: SymptomFingerprint, b: SymptomFingerprint) -> float:
    if a.signatures != b.signatures:
        return 0.0
    return round(
        0.2
        + 0.2 * (1.0 - abs(a.count_bucket - b.count_bucket) / 3)
        + 0.3 * _closeness(a.max_hop, b.max_hop)
        + 0.3 * seq_similarity(a.tier_seq, b.tier_seq),
        4,
    )


def cause_similarity(a: CauseFingerprint, b: CauseFingerprint) -> float:
    if a.cause_kind != b.cause_kind or a.signatures != b.signatures:
        return 0.0
    return round(
        0.3 + 0.3 * _closeness(a.hops, b.hops) + 0.4 * seq_similarity(a.labels, b.labels), 4
    )


def matches(similarity: float) -> bool:
    return similarity >= MATCH_THRESHOLD
