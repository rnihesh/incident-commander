"""Graph data contract shared by the generator, tools, and proofs.

Every node carries a generator-assigned string ``id`` (domain id). Internal FalkorDB ids
are never exposed outside the graph layer.

Time is epoch seconds (int). Bitemporal facts carry:
  valid_from   -- when the fact became true in the world
  valid_to     -- when it stopped being true (OPEN_END while still true)
  recorded_at  -- when the system learned about it
Missing valid_from/valid_to means "valid always"; missing recorded_at means "recorded now".
"""

from enum import StrEnum

# Fixed world clock: 2026-10-01T00:00:00Z. The demo world's "now".
WORLD_NOW = 1_790_812_800
HISTORY_DAYS = 90
OPEN_END = 4_102_444_800  # 2100-01-01, sentinel for "still valid"


class Label(StrEnum):
    SERVICE = "Service"  # one node per service-region, id "svc-<name>@<region>"
    TEAM = "Team"
    PERSON = "Person"
    CHANGE = "Change"  # kind: deploy | config | flag
    FLAG = "Flag"
    CONFIG = "Config"
    EXPIRY = "Expiry"  # kind: cert | token | quota
    INCIDENT = "Incident"
    SYMPTOM = "Symptom"
    POSTMORTEM = "Postmortem"
    RECOMMENDATION = "Recommendation"
    ADR = "ADR"


class Rel(StrEnum):
    DEPENDS_ON = "DEPENDS_ON"  # (caller:Service)->(callee:Service), bitemporal
    OBSERVED_CALL = "OBSERVED_CALL"  # (caller)->(callee) {first_seen, last_seen, count}
    LEARNED_DEPENDS_ON = "LEARNED_DEPENDS_ON"  # memory; never a proof premise
    OWNS = "OWNS"  # (Team)->(Service), bitemporal
    MEMBER_OF = "MEMBER_OF"  # (Person)->(Team)
    ON_CALL = "ON_CALL"  # (Person)->(Team), bitemporal rotation slot
    CHANGES = "CHANGES"  # (Change)->(Service)
    TOUCHES = "TOUCHES"  # (Change)->(Flag|Config)
    PREVIOUS_VERSION = "PREVIOUS_VERSION"  # (Change)->(Change it superseded)
    AUTHORED = "AUTHORED"  # (Person)->(Change)
    GATED_BY = "GATED_BY"  # (Flag)->(Config)
    HAS_FLAG = "HAS_FLAG"  # (Service)->(Flag)
    HAS_CONFIG = "HAS_CONFIG"  # (Service)->(Config)
    EXPIRES_ON = "EXPIRES_ON"  # (Expiry)->(Service)
    HAS = "HAS"  # (Incident)->(Symptom)
    ON = "ON"  # (Symptom)->(Service)
    CAUSED_BY = "CAUSED_BY"  # (Incident)->(Change|Expiry)
    REMEDIATED_BY = "REMEDIATED_BY"  # (Incident)->(Change)
    ABOUT = "ABOUT"  # (Postmortem)->(Incident)
    RECOMMENDS = "RECOMMENDS"  # (Postmortem)->(Recommendation)
    TARGETS = "TARGETS"  # (Recommendation)->(Service)
    DECIDED = "DECIDED"  # (ADR)->(Service)
    IMPACTS = "IMPACTS"  # derived, snapshots only: direction failure travels


class ChangeKind(StrEnum):
    DEPLOY = "deploy"
    CONFIG = "config"
    FLAG = "flag"


class ExpiryKind(StrEnum):
    CERT = "cert"
    TOKEN = "token"
    QUOTA = "quota"


class Signature(StrEnum):
    LATENCY = "latency"
    ERROR_5XX = "error_5xx"
    TLS_ERROR = "tls_error"
    SATURATION = "saturation"
    TIMEOUT = "timeout"


# Which symptom signatures each root-cause kind can produce (KTD23 clause d).
COMPATIBLE_SIGNATURES: dict[str, frozenset[Signature]] = {
    ChangeKind.DEPLOY: frozenset({Signature.LATENCY, Signature.ERROR_5XX, Signature.TIMEOUT}),
    ChangeKind.CONFIG: frozenset({Signature.ERROR_5XX, Signature.TIMEOUT, Signature.SATURATION}),
    ChangeKind.FLAG: frozenset({Signature.ERROR_5XX, Signature.LATENCY}),
    ExpiryKind.CERT: frozenset({Signature.TLS_ERROR}),
    ExpiryKind.TOKEN: frozenset({Signature.ERROR_5XX}),
    ExpiryKind.QUOTA: frozenset({Signature.SATURATION, Signature.ERROR_5XX}),
}

# ObservedCall counts at or above this inside the window become IMPACTS edges (KTD9).
OBSERVED_CALL_THRESHOLD = 50

REGIONS = ("us-east", "eu-west", "ap-south")

# Range indexes created on every world graph (id lookups and time filters).
INDEXES: tuple[tuple[str, str], ...] = (
    (Label.SERVICE, "id"),
    (Label.SERVICE, "name"),
    (Label.TEAM, "id"),
    (Label.PERSON, "id"),
    (Label.CHANGE, "id"),
    (Label.CHANGE, "valid_from"),
    (Label.FLAG, "id"),
    (Label.CONFIG, "id"),
    (Label.EXPIRY, "id"),
    (Label.INCIDENT, "id"),
    (Label.SYMPTOM, "id"),
    (Label.POSTMORTEM, "id"),
    (Label.RECOMMENDATION, "id"),
    (Label.ADR, "id"),
)

FULLTEXT_POSTMORTEM_FIELDS = ("title", "body", "symptoms_text")


def index_statements(embedding_dims: int) -> list[str]:
    """Cypher DDL to run on a fresh world graph."""
    stmts = [f"CREATE INDEX FOR (n:{label}) ON (n.{prop})" for label, prop in INDEXES]
    fields = ", ".join(f"n.{f}" for f in FULLTEXT_POSTMORTEM_FIELDS)
    stmts.append(f"CREATE FULLTEXT INDEX FOR (n:{Label.POSTMORTEM}) ON ({fields})")
    stmts.append(
        f"CREATE VECTOR INDEX FOR (n:{Label.POSTMORTEM}) ON (n.embedding) "
        f"OPTIONS {{dimension:{embedding_dims}, similarityFunction:'cosine'}}"
    )
    return stmts
