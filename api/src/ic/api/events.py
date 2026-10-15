"""Server-sent event envelope streamed to the UI for every run (R32).

The TypeScript mirror lives in web/lib/events.ts; keep both in sync.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

SCHEMA_VERSION = 1


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    REFLEX = "reflex"  # pre-snapshot answer (R30)
    SNAPSHOT = "snapshot"  # as-of graphs built
    TOOL_CALL = "tool_call"  # ToolResult payload (R15)
    HYPOTHESIS = "hypothesis"  # registered before execution (R12)
    PROOF = "proof"  # executed result + hash (R13)
    FUNNEL = "funnel"  # elimination step (R6)
    CERTIFICATE = "certificate"  # absence certificate (R7)
    VERDICT = "verdict"  # DIAGNOSED | INCONCLUSIVE | FAILED (+ DEGRADED flag)
    STATE = "state"  # incident state transition (KTD7)
    HANDOFF = "handoff"  # what remediation read from the graph (R22)
    REHEARSAL = "rehearsal"  # cloned-world result (R16)
    ACTION = "action"  # PR / status / page (R17-R19)
    MEMORY = "memory"  # write-back (R20)
    NARRATION = "narration"  # agent's short reasoning text
    STORM = "storm"  # alert-storm collapse progress (U17): intake, components, incidents
    ERROR = "error"
    DONE = "done"


class Agent(StrEnum):
    SYSTEM = "system"
    TRIAGE = "triage"
    REMEDIATION = "remediation"


class Event(BaseModel):
    id: int
    run_id: str
    ts: float  # epoch milliseconds (run layer and EventLog alike)
    type: EventType
    agent: Agent
    data: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
