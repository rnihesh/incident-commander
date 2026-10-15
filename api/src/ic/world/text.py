"""Templates for synthetic SRE prose: incident titles, postmortems, recommendations, ADRs."""

from __future__ import annotations

import random
from dataclasses import dataclass

SIGNATURE_PHRASES = {
    "latency": ["elevated p99 latency", "slow responses", "latency regression"],
    "error_5xx": ["a spike in HTTP 5xx errors", "elevated error rates", "failing requests (5xx)"],
    "tls_error": [
        "TLS handshake failures",
        "certificate validation errors",
        "expired-cert rejections",
    ],
    "saturation": [
        "resource saturation",
        "exhausted connection pools",
        "throttling and queue build-up",
    ],
    "timeout": ["upstream timeouts", "request timeouts", "deadline-exceeded errors"],
}

CAUSE_PHRASES = {
    "deploy": ["a deploy of {cause_service} ({detail})", "release {detail} of {cause_service}"],
    "config": [
        "a configuration change on {cause_service} ({detail})",
        "a config push to {cause_service}: {detail}",
    ],
    "flag": [
        "a feature flag flip on {cause_service} ({detail})",
        "enabling a flag on {cause_service}: {detail}",
    ],
    "cert": [
        "an expired TLS certificate on {cause_service}",
        "certificate expiry at {cause_service}",
    ],
    "token": ["an expired service token for {cause_service}", "token expiry at {cause_service}"],
    "quota": ["an exhausted quota on {cause_service}", "quota expiry at {cause_service}"],
}

OPENERS = [
    "On-call was paged for {sig_phrase} on {root}.",
    "Alerts fired for {sig_phrase} on {root}, shortly followed by its callers.",
    "Customer-facing impact began when {root} started showing {sig_phrase}.",
]
SPREAD = [
    "The problem propagated upstream to {others}.",
    "Callers including {others} were affected within minutes.",
    "Downstream effects were visible on {others}.",
]
NO_SPREAD = ["Impact stayed contained to {root}.", "No other services showed symptoms."]
ROOT_CAUSE = [
    "Root cause: {cause}. The timing and the dependency path from {cause_service} to {root} matched.",
    "We traced the incident to {cause}; everything affected sits upstream of {cause_service}.",
    "Investigation concluded the trigger was {cause}.",
]
COUPLING = [
    "The path ran through an undeclared dependency: {caller} calls {callee} but no DEPENDS_ON edge existed.",
    "Observed traffic showed {caller} depending on {callee}, which was missing from the declared topology.",
]
REMEDIATION = [
    "Remediation: {remediation}. Error rates recovered within {minutes} minutes.",
    "Mitigated by {remediation}; full recovery took {minutes} minutes.",
]
LESSONS = [
    "Detection was slower than it should have been; alerting on {sig_phrase} needs tighter thresholds.",
    "Rollout of {cause_service} lacked a canary stage.",
    "The runbook for {root} did not mention this failure mode.",
    "Ownership of {cause_service} was unclear during the first ten minutes.",
]

REC_TEMPLATES = {
    "deploy": [
        "Add a canary stage with automated rollback to {svc} deploys",
        "Require load-test sign-off before {svc} releases",
    ],
    "config": [
        "Validate {svc} config changes against a schema before rollout",
        "Stage config pushes to {svc} one region at a time",
    ],
    "flag": [
        "Add a kill-switch dashboard for {svc} feature flags",
        "Require a gating config review for flags on {svc}",
    ],
    "cert": [
        "Alert on certificate expiry 21 days ahead for {svc}",
        "Automate certificate renewal for {svc}",
    ],
    "token": [
        "Rotate {svc} service tokens automatically",
        "Alert on token expiry 14 days ahead for {svc}",
    ],
    "quota": [
        "Add quota headroom alerts for {svc}",
        "Raise and monitor the quota ceiling for {svc}",
    ],
}
REC_COMMON = [
    "Add a circuit breaker between {svc} and its callers",
    "Write a runbook for failure modes of {svc}",
    "Declare the missing dependency edge for {svc} in the service catalog",
]

ADR_TOPICS = [
    "Adopt gRPC with deadline propagation for {svc}",
    "Use a read-through cache in front of {svc}",
    "Move {svc} to a regional active-active layout",
    "Standardise retries with jitter and budgets on {svc}",
    "Gate risky behaviour in {svc} behind feature flags",
    "Store {svc} secrets in the central vault",
    "Adopt blue/green deploys for {svc}",
    "Rate-limit external callers of {svc}",
]
ADR_BODY = [
    "Context: {svc} had grown ad hoc operational practices. Decision: {decision}. "
    "Consequences: a small latency cost, much simpler on-call reasoning.",
    "We considered three alternatives for {svc}. The chosen approach trades implementation effort "
    "for predictable failure behaviour: {decision}.",
]


@dataclass
class PostmortemFacts:
    root: str  # root symptomatic service name
    others: list[str]  # other symptomatic service names
    signatures: list[str]
    cause_kind: str  # deploy | config | flag | cert | token | quota
    cause_service: str
    detail: str  # version / config change / flag
    remediation: str
    minutes: int
    coupling: tuple[str, str] | None


def _sig_phrase(rng: random.Random, signatures: list[str]) -> str:
    return " and ".join(rng.choice(SIGNATURE_PHRASES[s]) for s in dict.fromkeys(signatures))


def incident_title(rng: random.Random, f: PostmortemFacts) -> str:
    sig = rng.choice(SIGNATURE_PHRASES[f.signatures[0]])
    return rng.choice(
        [
            f"{sig.capitalize()} on {f.root}",
            f"{f.root}: {sig}",
            f"Incident: {sig} affecting {f.root}",
        ]
    )


def postmortem(rng: random.Random, f: PostmortemFacts) -> tuple[str, str, str]:
    """(title, body, symptoms_text) for one incident."""
    sig = _sig_phrase(rng, f.signatures)
    cause = rng.choice(CAUSE_PHRASES[f.cause_kind]).format(
        cause_service=f.cause_service, detail=f.detail
    )
    others = ", ".join(f.others[:4])
    ctx = dict(root=f.root, sig_phrase=sig, cause=cause, cause_service=f.cause_service)
    parts = [rng.choice(OPENERS).format(**ctx)]
    parts.append(
        rng.choice(SPREAD).format(others=others, **ctx)
        if f.others
        else rng.choice(NO_SPREAD).format(**ctx)
    )
    parts.append(rng.choice(ROOT_CAUSE).format(**ctx))
    if f.coupling:
        parts.append(rng.choice(COUPLING).format(caller=f.coupling[0], callee=f.coupling[1]))
    parts.append(rng.choice(REMEDIATION).format(remediation=f.remediation, minutes=f.minutes))
    parts.append(rng.choice(LESSONS).format(**ctx))
    symptoms = f"{sig} on {f.root}" + (f"; also {others}" if f.others else "")
    return incident_title(rng, f), "\n\n".join(parts), symptoms


def recommendation_text(rng: random.Random, cause_kind: str, svc: str) -> str:
    pool = REC_TEMPLATES[cause_kind] + REC_COMMON
    return rng.choice(pool).format(svc=svc)


def adr(rng: random.Random, svc: str) -> tuple[str, str]:
    topic = rng.choice(ADR_TOPICS).format(svc=svc)
    return topic, rng.choice(ADR_BODY).format(svc=svc, decision=topic.lower())
