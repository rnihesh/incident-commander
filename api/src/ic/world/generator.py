"""Seeded generator for the synthetic company (R1-R3, KTD12, KTD23).

Only ``random.Random(seed)`` is used, so a seed fully determines the world.
Pipeline: topology -> teams/people/on-call -> configs/flags -> change history ->
observed calls -> incidents (cause, symptoms, decoys, remediation) -> text -> layout.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import networkx as nx

from ic.graph.schema import (
    COMPATIBLE_SIGNATURES,
    HISTORY_DAYS,
    OBSERVED_CALL_THRESHOLD,
    OPEN_END,
    REGIONS,
    WORLD_NOW,
    ChangeKind,
    ExpiryKind,
)
from ic.world import text
from ic.world.causality import qualifies
from ic.world.layout import apply_layout
from ic.world.model import (
    ADR,
    Change,
    Config,
    Dependency,
    Expiry,
    Flag,
    Incident,
    ObservedCall,
    OnCallSlot,
    Ownership,
    Person,
    Postmortem,
    Recommendation,
    Service,
    Symptom,
    Team,
    World,
    add_entity_edge,
    impact_graph,
)

DAY = 86_400
WEEK = 7 * DAY
START = WORLD_NOW - HISTORY_DAYS * DAY

BUSINESS_NOUNS = {
    "commerce": [
        "checkout",
        "cart",
        "pricing",
        "catalog",
        "inventory",
        "orders",
        "promotions",
        "wishlist",
    ],
    "payments": [
        "ledger",
        "invoicing",
        "refunds",
        "payouts",
        "fraud-score",
        "billing",
        "tax",
        "settlement",
    ],
    "identity": ["user-profile", "sessions", "sso", "permissions", "signup", "accounts", "consent"],
    "growth": [
        "recommendations",
        "email-campaigns",
        "notifications",
        "referrals",
        "experiments",
        "loyalty",
    ],
    "logistics": ["warehouse", "routing", "fulfillment", "carrier-adapter", "tracking", "returns"],
    "data": ["event-ingest", "analytics-api", "reporting", "exports", "feature-store"],
}
BUSINESS_VARIANTS = [
    "",
    "-api",
    "-worker",
    "-sync",
    "-bff",
    "-scheduler",
    "-admin",
    "-events",
    "-v2",
    "-batch",
]
EDGE_NAMES = [
    "api-gateway",
    "web-gateway",
    "mobile-bff",
    "partner-api",
    "admin-gateway",
    "public-graphql",
    "ingress-proxy",
    "cdn-origin",
]
PLATFORM_APP = [
    "payments-core",
    "search",
    "auth",
    "notification-hub",
    "pricing-engine",
    "identity-core",
    "config-service",
    "feature-flags",
    "ledger-core",
    "media-service",
    "geo-service",
    "audit-log",
    "rate-limiter",
    "tax-engine",
    "scheduler-core",
    "metrics-gateway",
]
PLATFORM_APP_VARIANTS = ["", "-internal", "-edge-cache", "-replica"]
INFRA_KINDS = [
    "postgres",
    "redis",
    "kafka",
    "object-store",
    "elasticsearch",
    "memcached",
    "etcd",
    "vault",
    "dns",
    "mesh-proxy",
]
INFRA_TAGS = ["core", "orders", "users", "payments", "catalog", "events", "sessions", "media"]
TEAM_WORDS = [
    "Core",
    "Platform",
    "Reliability",
    "Flow",
    "Growth",
    "Foundations",
    "Delivery",
    "Insights",
]
FIRST = [
    "Asha",
    "Ben",
    "Chen",
    "Dara",
    "Eli",
    "Farah",
    "Gus",
    "Hana",
    "Ivan",
    "Jia",
    "Kofi",
    "Lena",
    "Mateo",
    "Nia",
    "Omar",
    "Priya",
    "Quinn",
    "Ravi",
    "Sofia",
    "Tariq",
    "Uma",
    "Viktor",
    "Wen",
    "Xavier",
    "Yara",
    "Zane",
]
LAST = [
    "Abara",
    "Bauer",
    "Cruz",
    "Dang",
    "Evans",
    "Fischer",
    "Garcia",
    "Haddad",
    "Ito",
    "Jensen",
    "Khan",
    "Lopez",
    "Moreau",
    "Novak",
    "Okafor",
    "Patel",
    "Quist",
    "Rossi",
    "Singh",
    "Tanaka",
    "Ueda",
    "Vega",
    "Wu",
    "Yilmaz",
]
CONFIG_KEYS = {  # key -> (low, high) for generated integer values
    "timeout_ms": (200, 5000),
    "pool_size": (5, 200),
    "retry_limit": (0, 8),
    "cache_ttl_s": (30, 3600),
    "rate_limit_rps": (50, 5000),
    "breaker_threshold": (3, 50),
    "batch_size": (10, 1000),
}
FLAG_NAMES = [
    "new_checkout_flow",
    "async_pricing",
    "strict_validation",
    "batch_writes",
    "fast_path_cache",
    "dark_launch_search",
    "v2_serializer",
    "retry_backoff",
    "shadow_traffic",
    "bulk_export",
]
DEPLOY_NOTES = [
    "dependency bump",
    "bugfix for edge case",
    "new endpoint",
    "perf tuning",
    "schema migration",
    "logging cleanup",
    "feature work",
    "security patch",
]
CAUSE_COUNTS = {"deploy": 14, "config": 8, "flag": 8, "expiry": 5, "coupling": 5}
ALL_KINDS = [*ChangeKind, *ExpiryKind]


@dataclass
class _Logical:
    name: str
    layer: int  # 0 edge, 1-3 business, 4 platform app, 5 infra
    domain: str
    regions: list[str]
    tier: int


@dataclass
class _Plan:
    cause: Change | Expiry
    coupling: list[str]
    symptoms: list[Symptom]
    decoys: list[Change | Expiry]
    remediation: Change
    opened_at: int
    resolved_at: int


class _Gen:
    def __init__(self, seed: int, n_services: int):
        self.seed = seed
        self.n = n_services
        self.rng = random.Random(seed)
        self.w = World(seed=seed, n_services=n_services)
        self.logical: list[_Logical] = []
        self.nodes: dict[str, Service] = {}
        self.owner_team: dict[str, str] = {}  # service id -> team id
        self.members: dict[str, list[Person]] = {}
        self.cfgs: dict[str, list[Config]] = {}
        self.flags: dict[str, list[Flag]] = {}
        self.versions: dict[str, list[int]] = {}
        self.used_times: set[tuple[str, str, int]] = set()
        self.baseline: list[Change] = []
        self.undeclared: list[tuple[str, str]] = []
        self.history_deps: list[Dependency] = []
        self.temp = 0

    # ---------------------------------------------------------------- helpers
    def _tmp(self, prefix: str) -> str:
        self.temp += 1
        return f"{prefix}-tmp{self.temp}"

    def _take(self, pool: list[str], k: int) -> list[str]:
        pool = sorted(set(pool))
        picked = self.rng.sample(pool, min(k, len(pool)))
        i = 2
        while len(picked) < k:  # pool exhausted: numbered variants
            picked.append(f"{self.rng.choice(pool)}-{i}")
            i += 1
        return picked

    def _delay(self) -> int:
        r = self.rng.random()
        if r < 0.85:
            return self.rng.randint(5, 300)
        return self.rng.randint(300, 3600) if r < 0.95 else self.rng.randint(3600, 2 * DAY)

    # ---------------------------------------------------------------- topology
    def topology(self) -> None:
        rng, n = self.rng, self.n
        n_edge = max(2, round(n * 0.05))
        n_plat = max(4, round(n * 0.25))
        n_biz = n - n_edge - n_plat
        n_infra = round(n_plat * 0.6)
        n_app = n_plat - n_infra
        domains = list(BUSINESS_NOUNS)
        biz_pool = [
            f"{d}|{noun}{v}"
            for d, nouns in BUSINESS_NOUNS.items()
            for noun in nouns
            for v in BUSINESS_VARIANTS
        ]
        edge_pool = EDGE_NAMES + [f"{d}-gateway" for d in domains]
        app_pool = [b + v for b in PLATFORM_APP for v in PLATFORM_APP_VARIANTS]
        infra_pool = [f"{k}-{t}" for k in INFRA_KINDS for t in INFRA_TAGS]

        def regions(weights: list[float]) -> list[str]:
            k = rng.choices([1, 2, 3], weights)[0]
            return sorted(rng.sample(REGIONS, k), key=REGIONS.index)

        for name in self._take(edge_pool, n_edge):
            self.logical.append(_Logical(name, 0, "edge", list(REGIONS), 1))
        for item in self._take(biz_pool, n_biz):
            domain, name = item.split("|", 1) if "|" in item else (rng.choice(domains), item)
            layer = rng.choices([1, 2, 3], [0.35, 0.35, 0.3])[0]
            self.logical.append(
                _Logical(name, layer, domain, regions([0.4, 0.35, 0.25]), rng.choice([1, 2, 2, 3]))
            )
        for name in self._take(app_pool, n_app):
            self.logical.append(_Logical(name, 4, "platform", regions([0, 0.5, 0.5]), 1))
        for name in self._take(infra_pool, n_infra):
            self.logical.append(_Logical(name, 5, "platform", regions([0, 0.4, 0.6]), 0))
        # logical names must be unique across categories
        seen: set[str] = set()
        for lg in self.logical:
            while lg.name in seen:
                lg.name += "-x"
            seen.add(lg.name)
        for lg in self.logical:
            for r in lg.regions:
                s = Service(f"svc-{lg.name}@{r}", lg.name, r, lg.tier, lg.domain)
                self.w.services.append(s)
                self.nodes[s.id] = s
        self._declared_edges()

    def _declared_edges(self) -> None:
        """Layered DAG with truncated-Zipf out-degree and preferential-attachment callees."""
        rng = self.rng
        zipf = [k**-2.0 for k in range(1, 13)]
        indeg = {lg.name: 0 for lg in self.logical}
        edges: list[tuple[_Logical, _Logical]] = []
        for caller in sorted(self.logical, key=lambda lg: lg.layer):
            if caller.layer == 5:
                continue
            deg = rng.choices(range(1, 13), zipf)[0]
            if caller.layer == 0:
                deg = min(12, 2 * deg + 1)
            cands = [c for c in self.logical if c.layer > caller.layer]
            for _ in range(min(deg, len(cands))):
                weights = [
                    (1 + indeg[c.name])
                    * (3 if c.layer >= 4 else 1)
                    * (1.0 if c.layer == caller.layer + 1 else 0.5)
                    for c in cands
                ]
                callee = rng.choices(cands, weights)[0]
                cands.remove(callee)
                indeg[callee.name] += 1
                edges.append((caller, callee))
        same = [lg for lg in self.logical if 1 <= lg.layer <= 3]
        for i, caller in enumerate(same):  # a few peer calls, still acyclic via list order
            if rng.random() < 0.05:
                peers = [c for c in same[i + 1 :] if c.layer == caller.layer]
                if peers:
                    edges.append((caller, rng.choice(peers)))
        by_name = {lg.name: lg for lg in self.logical}
        pairs: set[tuple[str, str]] = set()
        for a, b in edges:
            for r in a.regions:
                if r in b.regions and rng.random() > 0.03:
                    pairs.add((f"svc-{a.name}@{r}", f"svc-{b.name}@{r}"))
                else:  # rare (or forced) cross-region call
                    pairs.add((f"svc-{a.name}@{r}", f"svc-{b.name}@{rng.choice(b.regions)}"))
        del by_name
        for caller_id, callee_id in sorted(pairs):
            vf = START - rng.randint(30, 300) * DAY
            self.w.dependencies.append(
                Dependency(caller_id, callee_id, vf, OPEN_END, vf + rng.randint(0, 3600))
            )
        # ~2% of dependencies were added during the history window (bitemporal churn)
        for d in rng.sample(self.w.dependencies, max(1, len(self.w.dependencies) // 50)):
            d.valid_from = rng.randint(START + 3 * DAY, WORLD_NOW - 3 * DAY)
            d.recorded_at = d.valid_from + rng.randint(0, 2 * 3600)
            self.history_deps.append(d)

    # ---------------------------------------------------------------- org
    def organisation(self) -> None:
        rng, w = self.rng, self.w
        by_domain: dict[str, list[str]] = {}
        for lg in self.logical:
            by_domain.setdefault(lg.domain, []).append(lg.name)
        names = [f"{f} {last}" for f in FIRST for last in LAST]
        rng.shuffle(names)
        team_no = person_no = 0
        for domain in sorted(by_domain):
            todo = by_domain[domain][:]
            groups: list[list[str]] = []
            while todo:
                size = rng.randint(3, 8)
                if len(todo) <= 8 or len(todo) - size < 3:
                    size = len(todo) if len(todo) <= 8 else len(todo) // 2
                groups.append(todo[:size])
                todo = todo[size:]
            if len(groups) > 1 and len(groups[-1]) < 3:
                groups[-2].extend(groups.pop())
            for group in groups:
                team_no += 1
                team = Team(
                    f"team-{team_no:03d}",
                    f"{domain.title()} {TEAM_WORDS[team_no % len(TEAM_WORDS)]}",
                    domain,
                )
                w.teams.append(team)
                people = []
                for _ in range(rng.randint(3, 6)):
                    person_no += 1
                    people.append(
                        Person(f"per-{person_no:04d}", names[person_no % len(names)], team.id)
                    )
                w.people.extend(people)
                self.members[team.id] = people
                for name in group:
                    for s in w.services:
                        if s.name == name:
                            self.owner_team[s.id] = team.id
                            vf = START - 400 * DAY
                            w.ownerships.append(Ownership(team.id, s.id, vf, OPEN_END, vf))
                self._rotation(team, people)

    def _rotation(self, team: Team, people: list[Person]) -> None:
        first = START - 2 * WEEK
        offset = self.rng.randrange(len(people))
        k = 0
        while first + k * WEEK < WORLD_NOW + 5 * WEEK:
            vf = first + k * WEEK
            p = people[(k + offset) % len(people)]
            self.w.oncall.append(OnCallSlot(p.id, team.id, vf, vf + WEEK, vf - 2 * WEEK))
            k += 1

    # ---------------------------------------------------------------- configs, flags
    def configs_and_flags(self) -> None:
        rng, w = self.rng, self.w
        for s in w.services:
            keys = rng.sample(sorted(CONFIG_KEYS), rng.randint(1, 3))
            self.cfgs[s.id] = [
                Config(
                    f"cfg-{len(w.configs) + i + 1:05d}", s.id, k, str(rng.randint(*CONFIG_KEYS[k]))
                )
                for i, k in enumerate(keys)
            ]
            w.configs.extend(self.cfgs[s.id])
            self.flags[s.id] = []
            for name in rng.sample(FLAG_NAMES, rng.randint(0, 3)):
                f = Flag(
                    f"flg-{len(w.flags) + 1:05d}",
                    s.id,
                    name,
                    rng.choice(self.cfgs[s.id]).id,
                    rng.random() < 0.5,
                )
                w.flags.append(f)
                self.flags[s.id].append(f)
            self.versions[s.id] = [rng.randint(1, 4), rng.randint(0, 20), rng.randint(0, 30)]

    # ---------------------------------------------------------------- changes
    def _free(self, service_id: str, target: str, t: int) -> int:
        while (service_id, target, t) in self.used_times:
            t += 1
        self.used_times.add((service_id, target, t))
        return t

    def _author(self, service_id: str) -> str:
        return self.rng.choice(self.members[self.owner_team[service_id]]).id

    def _change(
        self, service_id: str, kind: str, t: int, target: str, value: str, prev: str, summary: str
    ) -> Change:
        touches = None if kind == ChangeKind.DEPLOY else target
        t = self._free(service_id, target, t)
        return Change(
            self._tmp("chg"),
            kind,
            service_id,
            summary,
            t,
            OPEN_END,
            t + self._delay(),
            self._author(service_id),
            target,
            touches,
            value,
            prev,
        )

    def _version(self, service_id: str, bump: int = 1) -> tuple[str, str]:
        v = self.versions[service_id]
        before = "v{}.{}.{}".format(*v)
        v[2] += self.rng.randint(1, 2) * bump
        return before, "v{}.{}.{}".format(*v)

    def random_change(self, service_id: str, kind: str, t: int) -> Change:
        """A new change of ``kind`` (falls back to a deploy if the service has no flag)."""
        rng = self.rng
        if kind == ChangeKind.FLAG and self.flags[service_id]:
            f = rng.choice(self.flags[service_id])
            value = rng.choice(["on", "off"])
            return self._change(
                service_id,
                kind,
                t,
                f.id,
                value,
                "off" if value == "on" else "on",
                f"Set flag {f.name} {value}",
            )
        if kind == ChangeKind.CONFIG:
            c = rng.choice(self.cfgs[service_id])
            value = str(rng.randint(*CONFIG_KEYS[c.key]))
            return self._change(
                service_id, kind, t, c.id, value, c.value, f"Set {c.key} to {value}"
            )
        before, after = self._version(service_id)
        note = rng.choice(DEPLOY_NOTES)
        return self._change(
            service_id, ChangeKind.DEPLOY, t, "deploy", after, before, f"Deploy {after}: {note}"
        )

    def baseline_changes(self) -> None:
        """Chains of deploys, config changes and flag flips, generated in time order per target."""
        rng = self.rng

        def times(k: int) -> list[int]:
            return sorted(rng.randint(START + 3 * 3600, WORLD_NOW - 2 * 3600) for _ in range(k))

        for s in self.w.services:
            sid = s.id
            for t in times(rng.randint(1, 6)):
                before, after = self._version(sid)
                self.baseline.append(
                    self._change(
                        sid,
                        ChangeKind.DEPLOY,
                        t,
                        "deploy",
                        after,
                        before,
                        f"Deploy {after}: {rng.choice(DEPLOY_NOTES)}",
                    )
                )
            for c in self.cfgs[sid]:
                cur = c.value
                for t in times(rng.choice([0, 0, 1, 2])):
                    value = str(rng.randint(*CONFIG_KEYS[c.key]))
                    self.baseline.append(
                        self._change(
                            sid, ChangeKind.CONFIG, t, c.id, value, cur, f"Set {c.key} to {value}"
                        )
                    )
                    cur = value
            for f in self.flags[sid]:
                cur = "on" if f.enabled else "off"
                for t in times(rng.choice([0, 1, 1, 2])):
                    value = "off" if cur == "on" else "on"
                    self.baseline.append(
                        self._change(
                            sid,
                            ChangeKind.FLAG,
                            t,
                            f.id,
                            value,
                            cur,
                            f"Flip flag {f.name} {cur}->{value}",
                        )
                    )
                    cur = value
        self.w.changes.extend(self.baseline)
        link_chains(self.w.changes)

    # ---------------------------------------------------------------- calls, expiries
    def observed_calls(self) -> None:
        rng, w = self.rng, self.w
        declared = {(d.caller_id, d.callee_id): d for d in w.dependencies}
        for d in w.dependencies:
            first = d.valid_from if d in self.history_deps else START
            w.observed_calls.append(
                ObservedCall(d.caller_id, d.callee_id, first, WORLD_NOW, rng.randint(200, 50_000))
            )
        layer = {f"svc-{lg.name}@{r}": lg.layer for lg in self.logical for r in lg.regions}
        ids = sorted(layer)
        want = max(2, round(0.03 * len(w.dependencies)))
        weak = max(1, round(0.01 * len(w.dependencies)))
        guard = 0
        while len(self.undeclared) < want + weak and guard < 20_000:
            guard += 1
            a, b = rng.choice(ids), rng.choice(ids)
            if a.split("@")[1] != b.split("@")[1] or layer[a] >= layer[b] or (a, b) in declared:
                continue
            if (a, b) in self.undeclared:
                continue
            strong = len(self.undeclared) < want
            count = (
                rng.randint(80, 3000) if strong else rng.randint(5, OBSERVED_CALL_THRESHOLD - 10)
            )
            self.undeclared.append((a, b))
            w.observed_calls.append(ObservedCall(a, b, START, WORLD_NOW, count))
        self.undeclared = self.undeclared[:want]  # strong couplings only; weak ones stay as noise

    def background_expiries(self) -> None:
        rng = self.rng
        for s in rng.sample(self.w.services, round(0.08 * len(self.w.services))):
            kind = rng.choices(list(ExpiryKind), [0.6, 0.25, 0.15])[0]
            self.w.expiries.append(
                self._expiry(s.id, kind, rng.randint(WORLD_NOW + 3600, WORLD_NOW + 60 * DAY))
            )

    def _expiry(self, service_id: str, kind: str, valid_to: int) -> Expiry:
        vf = valid_to - self.rng.randint(30, 365) * DAY
        name = self.nodes[service_id].name
        return Expiry(
            self._tmp("exp"), kind, service_id, f"{kind} for {name} expires", vf, valid_to, vf
        )

    # ---------------------------------------------------------------- incidents
    def incidents(self) -> list[_Plan]:
        rng, w = self.rng, self.w
        structural = World(self.seed, self.n)
        structural.services, structural.dependencies = w.services, w.dependencies
        structural.observed_calls = w.observed_calls
        self.structural = structural
        kinds = [k for k, c in CAUSE_COUNTS.items() for _ in range(c)]
        rng.shuffle(kinds)
        plans: list[_Plan] = []
        windows: list[tuple[int, int]] = []
        used: set[str] = set()
        for kind in kinds:
            for _ in range(40):
                plan = self._plan(kind, used)
                if plan is None:
                    continue
                lo, hi = plan.cause.event_time - 4 * 3600, plan.resolved_at + 4 * 3600
                if any(lo < b and a < hi for a, b in windows):
                    continue
                if any(
                    lo - DAY <= t <= hi + DAY
                    for d in self.history_deps
                    for t in (d.valid_from, d.recorded_at)
                ):
                    continue  # keep the IMPACTS graph identical in true and known views
                windows.append((lo, hi))
                used.add(plan.cause.id)
                plans.append(plan)
                break
        return plans

    def _candidate_change(self, kinds: set[str]) -> Change | None:
        pool = [
            c
            for c in self.baseline
            if c.kind in kinds
            and START + 3 * DAY < c.valid_from < WORLD_NOW - 2 * DAY
            and c.recorded_at - c.valid_from <= 900
            and c.valid_to - c.valid_from > 3 * 3600
        ]
        return self.rng.choice(pool) if pool else None

    def _plan(self, kind: str, used: set[str]) -> _Plan | None:
        rng = self.rng
        coupling: list[str] = []
        cause: Change | Expiry | None
        if kind == "expiry":
            sid = rng.choice(self.w.services).id
            cause = self._expiry(
                sid, rng.choice(list(ExpiryKind)), rng.randint(START + 3 * DAY, WORLD_NOW - 3 * DAY)
            )
        elif kind == "coupling":
            caller, callee = rng.choice(self.undeclared)
            pool = [
                c
                for c in self.baseline
                if c.service_id == callee
                and c.recorded_at - c.valid_from <= 900
                and START + 3 * DAY < c.valid_from < WORLD_NOW - 2 * DAY
                and c.valid_to - c.valid_from > 3 * 3600
            ]
            if not pool:
                return None
            cause, coupling = rng.choice(pool), [caller, callee]
        else:
            cause = self._candidate_change({kind})
        if cause is None or cause.id in used:
            return None

        g = impact_graph(self.structural, cause.event_time, as_known=False)
        if coupling:
            declared = impact_graph(
                self.structural, cause.event_time, as_known=False, include_observed=False
            )
            if nx.has_path(declared, coupling[1], coupling[0]):
                return None
        add_entity_edge(g, cause)
        hops = nx.single_source_shortest_path_length(g, cause.id)
        cone = sorted(s for s, d in hops.items() if s.startswith("svc-") and 2 <= d <= 4)
        if coupling and coupling[0] not in cone:
            return None
        chosen = rng.sample(cone, rng.randint(0, min(5, len(cone))))
        if coupling and coupling[0] not in chosen:
            chosen.append(coupling[0])
        services = [cause.service_id, *chosen]

        allowed = sorted(str(s) for s in COMPATIBLE_SIGNATURES[cause.sig_kind])
        first_sig = rng.choice(allowed)
        step = rng.randint(90, 240)
        base = cause.event_time + rng.randint(60, 600)
        symptoms = [
            Symptom(
                "",
                "",
                s,
                base + (hops[s] - 1) * step + rng.randint(0, step // 3),
                first_sig if rng.random() < 0.7 else rng.choice(allowed),
            )
            for s in services
        ]
        earliest = min(s.onset for s in symptoms)
        opened = earliest + rng.randint(60, 300)
        resolved = opened + rng.randint(900, 7200)
        if not qualifies(cause, g, symptoms)[0]:
            return None
        remediation = self._remediation(cause, resolved)
        add_entity_edge(g, remediation)
        decoys = self._decoys(cause, g, symptoms, cone, earliest)
        return _Plan(cause, coupling, symptoms, decoys, remediation, opened, resolved)

    def _remediation(self, cause: Change | Expiry, resolved: int) -> Change:
        rng = self.rng
        t = resolved - rng.randint(60, 300)
        if isinstance(cause, Change):  # rollback restoring the previous value on the same target
            return self._change(
                cause.service_id,
                cause.kind,
                t,
                cause.target_key,
                cause.prev_value,
                cause.value,
                f"Rollback {cause.summary} (restore {cause.prev_value})",
            )
        c = self.cfgs[cause.service_id][0]
        value = str(rng.randint(*CONFIG_KEYS[c.key]))
        return self._change(
            cause.service_id,
            ChangeKind.CONFIG,
            t,
            c.id,
            value,
            c.value,
            f"Rotate {cause.kind} and set {c.key} to {value}",
        )

    def _decoys(
        self, cause, g: nx.DiGraph, symptoms: list[Symptom], cone: list[str], earliest: int
    ):
        """Timing-matched entities that are reachable yet each fail >= 1 clause (verified)."""
        rng = self.rng
        sigs = {s.signature for s in symptoms}
        compatible = [k for k in ALL_KINDS if sigs <= {str(x) for x in COMPATIBLE_SIGNATURES[k]}]
        incompatible = [k for k in ALL_KINDS if k not in compatible]
        root = cause.service_id
        below = sorted(s for s in nx.ancestors(g, root) if s.startswith("svc-"))  # root's callees
        before = lambda: earliest - rng.randint(300, 3600)  # noqa: E731
        wanted: list[tuple[str, str, int]] = [
            (root, rng.choice(ALL_KINDS), earliest + rng.randint(30, 300))
        ]
        if cone and compatible:
            wanted.append((rng.choice(cone), rng.choice(compatible), before()))
        if incompatible:
            wanted.append((root, rng.choice(incompatible), before()))
            if below:
                wanted.append((rng.choice(below), rng.choice(incompatible), before()))
                wanted.append((rng.choice(below), rng.choice(incompatible), before()))
        decoys = []
        for sid, kind, t in wanted:
            if kind in ExpiryKind:
                d = self._expiry(sid, kind, t)
            else:
                d = self.random_change(sid, kind, t)
            add_entity_edge(g, d)
            if qualifies(d, g, symptoms)[0]:
                g.remove_node(d.id)  # accidentally a valid explanation: drop it
                continue
            decoys.append(d)
        return decoys

    # ---------------------------------------------------------------- finalize
    def commit(self, plans: list[_Plan]) -> None:
        """Insert incident entities, assign final time-ordered ids, link version chains."""
        rng, w = self.rng, self.w
        for p in plans:
            for e in (p.cause, *p.decoys, p.remediation):
                bucket = w.changes if isinstance(e, Change) else w.expiries
                if e not in bucket:
                    bucket.append(e)
        for items, prefix, width in ((w.changes, "chg", 5), (w.expiries, "exp", 4)):
            order = sorted(items, key=lambda e: (e.event_time, rng.random()))
            for i, e in enumerate(order, 1):
                e.id = f"{prefix}-{i:0{width}d}"
            items[:] = order
        link_chains(w.changes)
        last: dict[tuple[str, str], Change] = {}
        for c in w.changes:
            last[(c.service_id, c.target_key)] = c
        for cfg in w.configs:
            if (cfg.service_id, cfg.id) in last:
                cfg.value = last[(cfg.service_id, cfg.id)].value
        for f in w.flags:
            if (f.service_id, f.id) in last:
                f.enabled = last[(f.service_id, f.id)].value == "on"

        plans.sort(key=lambda p: p.opened_at)
        for n, p in enumerate(plans, 1):
            self._incident_rows(f"inc-{n:03d}", p)

    def _incident_rows(self, inc_id: str, p: _Plan) -> None:
        rng, w = self.rng, self.w
        names = {s.id: s.name for s in w.services}
        symptoms = []
        for s in p.symptoms:
            s.id, s.incident_id = f"sym-{len(w.symptoms) + len(symptoms) + 1:04d}", inc_id
            symptoms.append(s)
        w.symptoms.extend(symptoms)
        root = p.cause.service_id
        facts = text.PostmortemFacts(
            root=names[root],
            others=[names[s.service_id] for s in symptoms[1:]],
            signatures=[s.signature for s in symptoms],
            cause_kind=p.cause.kind,
            cause_service=names[root],
            detail=p.cause.summary if isinstance(p.cause, Change) else p.cause.kind,
            remediation=p.remediation.summary,
            minutes=(p.resolved_at - p.opened_at) // 60,
            coupling=(names[p.coupling[0]], names[p.coupling[1]]) if p.coupling else None,
        )
        title, body, sym_text = text.postmortem(rng, facts)
        w.incidents.append(
            Incident(
                inc_id,
                title,
                p.opened_at,
                p.resolved_at,
                p.cause.id,
                "change" if isinstance(p.cause, Change) else "expiry",
                p.remediation.id,
                p.coupling,
                sorted(d.id for d in p.decoys),
            )
        )
        pm_id = f"pm-{len(w.postmortems) + 1:03d}"
        w.postmortems.append(Postmortem(pm_id, inc_id, title, body, sym_text))
        path = list(dict.fromkeys([root, *(s.service_id for s in symptoms)]))
        for _ in range(rng.randint(1, 3)):
            targets = rng.sample(path, min(len(path), rng.randint(1, 2)))
            w.recommendations.append(
                Recommendation(
                    f"rec-{len(w.recommendations) + 1:03d}",
                    pm_id,
                    text.recommendation_text(rng, p.cause.kind, names[targets[0]]),
                    "open" if rng.random() < 0.5 else "closed",
                    targets,
                )
            )

    def adrs(self) -> None:
        rng, w = self.rng, self.w
        for i in range(1, 21):
            lg = rng.choice(self.logical)
            title, body = text.adr(rng, lg.name)
            ids = [f"svc-{lg.name}@{r}" for r in lg.regions]
            w.adrs.append(
                ADR(
                    f"adr-{i:03d}",
                    f"ADR-{i:03d}: {title}",
                    body,
                    rng.randint(START - 700 * DAY, START),
                    ids,
                )
            )


def link_chains(changes: list[Change]) -> None:
    """Set previous_id / valid_to / prev_value along each (service, target) version chain."""
    chains: dict[tuple[str, str], list[Change]] = {}
    for c in sorted(changes, key=lambda c: c.valid_from):
        chains.setdefault((c.service_id, c.target_key), []).append(c)
    for chain in chains.values():
        chain[0].previous_id = None
        for prev, nxt in zip(chain, chain[1:], strict=False):
            prev.valid_to = nxt.valid_from
            nxt.previous_id, nxt.prev_value = prev.id, prev.value
        chain[-1].valid_to = OPEN_END


def verify_incidents(world: World) -> None:
    """Assert every incident cause qualifies (true and known views) and every decoy fails."""
    for inc in world.incidents:
        symptoms = world.symptoms_of(inc.id)
        for as_known in (False, True):
            g = impact_graph(world, inc.resolved_at, as_known=as_known)
            ok, clause, detail = qualifies(world.entity(inc.cause_id), g, symptoms)
            assert ok, f"{inc.id} cause fails {clause}: {detail}"
        g = impact_graph(world, inc.resolved_at, as_known=False)
        for d in inc.decoy_ids:
            assert not qualifies(world.entity(d), g, symptoms)[0], f"{inc.id} decoy {d} qualifies"


def generate_world(seed: int, n_services: int = 400) -> World:
    gen = _Gen(seed, n_services)
    gen.topology()
    gen.organisation()
    gen.configs_and_flags()
    gen.baseline_changes()
    gen.observed_calls()
    gen.background_expiries()
    plans = gen.incidents()
    gen.commit(plans)
    gen.adrs()
    apply_layout(gen.w, seed)
    verify_incidents(gen.w)
    return gen.w
