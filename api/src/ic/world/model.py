"""In-memory model of the synthetic company, plus the IMPACTS graph view (KTD9).

The generator builds a ``World``; the loader turns it into FalkorDB rows; the causality
oracle and the tests read it directly. Everything here is plain data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields

import networkx as nx

from ic.graph.schema import OBSERVED_CALL_THRESHOLD, OPEN_END, Rel


@dataclass
class Service:
    id: str  # svc-<name>@<region>
    name: str
    region: str
    tier: int
    domain: str
    x: float = 0.0  # layout position (excluded from the content hash)
    y: float = 0.0


@dataclass
class Team:
    id: str
    name: str
    domain: str


@dataclass
class Person:
    id: str
    name: str
    team_id: str


@dataclass
class OnCallSlot:
    person_id: str
    team_id: str
    valid_from: int
    valid_to: int
    recorded_at: int


@dataclass
class Ownership:
    team_id: str
    service_id: str
    valid_from: int
    valid_to: int
    recorded_at: int


@dataclass
class Dependency:
    caller_id: str
    callee_id: str
    valid_from: int
    valid_to: int
    recorded_at: int


@dataclass
class ObservedCall:
    caller_id: str
    callee_id: str
    first_seen: int
    last_seen: int
    count: int


@dataclass
class Config:
    id: str
    service_id: str
    key: str
    value: str


@dataclass
class Flag:
    id: str
    service_id: str
    name: str
    config_id: str  # the Config this flag is GATED_BY
    enabled: bool


@dataclass
class Change:
    id: str
    kind: str  # ChangeKind
    service_id: str
    summary: str
    valid_from: int
    valid_to: int  # when superseded by the next change on the same target
    recorded_at: int
    author_id: str
    target_key: str  # "deploy" | config id | flag id: identifies the version chain
    touches_id: str | None  # Flag/Config id for flag/config kinds
    value: str  # version string / config value / "on" | "off"
    prev_value: str
    previous_id: str | None = None  # the change this one superseded

    @property
    def event_time(self) -> int:
        return self.valid_from

    @property
    def sig_kind(self) -> str:
        return self.kind


@dataclass
class Expiry:
    id: str
    kind: str  # ExpiryKind
    service_id: str
    summary: str
    valid_from: int  # when the credential was issued
    valid_to: int  # when it expires (may be in the future)
    recorded_at: int

    @property
    def event_time(self) -> int:
        return self.valid_to

    @property
    def sig_kind(self) -> str:
        return self.kind


@dataclass
class Symptom:
    id: str
    incident_id: str
    service_id: str
    onset: int
    signature: str


@dataclass
class Incident:
    id: str
    title: str
    opened_at: int
    resolved_at: int
    cause_id: str
    cause_kind: str  # "change" | "expiry"
    remediation_id: str
    coupling: list[str] = field(default_factory=list)  # [caller, callee] if undeclared coupling
    decoy_ids: list[str] = field(default_factory=list)  # answer key; never loaded into the graph


@dataclass
class Postmortem:
    id: str
    incident_id: str
    title: str
    body: str
    symptoms_text: str


@dataclass
class Recommendation:
    id: str
    postmortem_id: str
    text: str
    status: str  # "open" | "closed"
    target_ids: list[str]


@dataclass
class ADR:
    id: str
    title: str
    body: str
    decided_at: int
    service_ids: list[str]


@dataclass
class World:
    seed: int
    n_services: int
    services: list[Service] = field(default_factory=list)
    teams: list[Team] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    ownerships: list[Ownership] = field(default_factory=list)
    oncall: list[OnCallSlot] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    observed_calls: list[ObservedCall] = field(default_factory=list)
    configs: list[Config] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    expiries: list[Expiry] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    symptoms: list[Symptom] = field(default_factory=list)
    postmortems: list[Postmortem] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    adrs: list[ADR] = field(default_factory=list)

    def symptoms_of(self, incident_id: str) -> list[Symptom]:
        return [s for s in self.symptoms if s.incident_id == incident_id]

    def entity(self, entity_id: str) -> Change | Expiry:
        for e in (*self.changes, *self.expiries):
            if e.id == entity_id:
                return e
        raise KeyError(entity_id)

    def node_counts(self) -> dict[str, int]:
        """Expected node count per label; the loader test compares the graph to this."""
        return {
            "Service": len(self.services),
            "Team": len(self.teams),
            "Person": len(self.people),
            "Change": len(self.changes),
            "Flag": len(self.flags),
            "Config": len(self.configs),
            "Expiry": len(self.expiries),
            "Incident": len(self.incidents),
            "Symptom": len(self.symptoms),
            "Postmortem": len(self.postmortems),
            "Recommendation": len(self.recommendations),
            "ADR": len(self.adrs),
        }

    def edge_counts(self) -> dict[str, int]:
        """Expected edge count per relationship type."""
        return {
            Rel.DEPENDS_ON: len(self.dependencies),
            Rel.OBSERVED_CALL: len(self.observed_calls),
            Rel.OWNS: len(self.ownerships),
            Rel.MEMBER_OF: len(self.people),
            Rel.ON_CALL: len(self.oncall),
            Rel.CHANGES: len(self.changes),
            Rel.TOUCHES: sum(c.touches_id is not None for c in self.changes),
            Rel.PREVIOUS_VERSION: sum(c.previous_id is not None for c in self.changes),
            Rel.AUTHORED: len(self.changes),
            Rel.GATED_BY: len(self.flags),
            Rel.HAS_FLAG: len(self.flags),
            Rel.HAS_CONFIG: len(self.configs),
            Rel.EXPIRES_ON: len(self.expiries),
            Rel.HAS: len(self.symptoms),
            Rel.ON: len(self.symptoms),
            Rel.CAUSED_BY: len(self.incidents),
            Rel.REMEDIATED_BY: len(self.incidents),
            Rel.ABOUT: len(self.postmortems),
            Rel.RECOMMENDS: len(self.recommendations),
            Rel.TARGETS: sum(len(r.target_ids) for r in self.recommendations),
            Rel.DECIDED: sum(len(a.service_ids) for a in self.adrs),
        }


# --- IMPACTS graph (KTD9) -------------------------------------------------------------


def add_entity_edge(g: nx.DiGraph, entity: Change | Expiry) -> None:
    """change -> changed service, expiry -> service."""
    g.add_edge(entity.id, entity.service_id, via="entity")


def impact_graph(
    world: World, t: int, *, as_known: bool, include_observed: bool = True
) -> nx.DiGraph:
    """IMPACTS digraph at time ``t``, edges oriented in the direction failure travels.

    - change -> its service, expiry -> its service (once the event has happened)
    - callee -> caller for every DEPENDS_ON valid at ``t``
    - callee -> caller for OBSERVED_CALL with count >= threshold and first_seen <= t
    ``as_known`` additionally drops anything recorded after ``t``.
    """

    def known(recorded_at: int) -> bool:
        return not as_known or recorded_at <= t

    g = nx.DiGraph()
    g.add_nodes_from((s.id for s in world.services), kind="service")
    for c in world.changes:
        if c.valid_from <= t and known(c.recorded_at):
            add_entity_edge(g, c)
    for e in world.expiries:
        if e.valid_to <= t and known(e.recorded_at):
            add_entity_edge(g, e)
    for d in world.dependencies:
        if d.valid_from <= t < d.valid_to and known(d.recorded_at):
            g.add_edge(d.callee_id, d.caller_id, via="depends_on")
    if include_observed:
        for o in world.observed_calls:
            if o.count >= OBSERVED_CALL_THRESHOLD and o.first_seen <= t:
                if not g.has_edge(o.callee_id, o.caller_id):
                    g.add_edge(o.callee_id, o.caller_id, via="observed_call")
    return g


def call_graph(world: World) -> nx.DiGraph:
    """Declared caller -> callee graph (currently valid dependencies only)."""
    g = nx.DiGraph()
    g.add_nodes_from(s.id for s in world.services)
    g.add_edges_from(
        (d.caller_id, d.callee_id) for d in world.dependencies if d.valid_to == OPEN_END
    )
    return g


# --- content hash ---------------------------------------------------------------------

_LAYOUT_FIELDS = {"x", "y"}  # platform-dependent floats; excluded from the hash


def world_to_dict(world: World) -> dict:
    out: dict = {"seed": world.seed, "n_services": world.n_services}
    for f in fields(world):
        if f.name in out:
            continue
        rows = []
        for item in getattr(world, f.name):
            d = asdict(item)
            rows.append({k: v for k, v in d.items() if k not in _LAYOUT_FIELDS})
        out[f.name] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
    return out


def world_content_hash(world: World) -> str:
    """Stable sha256 over canonical JSON of the world (layout positions excluded)."""
    blob = json.dumps(world_to_dict(world), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
