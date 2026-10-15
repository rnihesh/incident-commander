"""Incident state machine (KTD7). State lives on the ledger's ``LedgerIncident`` node."""

from __future__ import annotations

import time
from enum import StrEnum

from ic.graph.client import query


class State(StrEnum):
    OPEN = "OPEN"
    TRIAGING = "TRIAGING"
    DIAGNOSED = "DIAGNOSED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    REHEARSING = "REHEARSING"
    REFUSED = "REFUSED"
    ACTING = "ACTING"
    PR_OPEN = "PR_OPEN"
    RESOLVED = "RESOLVED"
    LEARNED = "LEARNED"


ALLOWED: dict[State, frozenset[State]] = {
    State.OPEN: frozenset({State.TRIAGING}),
    State.TRIAGING: frozenset({State.DIAGNOSED, State.INCONCLUSIVE, State.FAILED}),
    State.DIAGNOSED: frozenset({State.REHEARSING}),
    State.INCONCLUSIVE: frozenset(),
    State.FAILED: frozenset(),
    # -> DIAGNOSED releases the claim when a run is cancelled mid-rehearsal (nothing acted yet).
    State.REHEARSING: frozenset({State.REFUSED, State.ACTING, State.DIAGNOSED}),
    State.REFUSED: frozenset(),
    State.ACTING: frozenset({State.PR_OPEN}),
    State.PR_OPEN: frozenset({State.RESOLVED}),
    State.RESOLVED: frozenset({State.LEARNED}),
    State.LEARNED: frozenset(),
}
# Completion handler pages the on-call on these (no PR opens).
PAGING = frozenset({State.INCONCLUSIVE, State.FAILED, State.REFUSED})


class IllegalTransition(Exception):
    pass


async def get_state(ledger_graph: str, incident_id: str) -> State | None:
    rs = (
        await query(
            ledger_graph,
            "MATCH (i:LedgerIncident {id: $id}) RETURN i.state",
            {"id": incident_id},
            read_only=True,
        )
    ).result_set
    return State(rs[0][0]) if rs and rs[0][0] else None


async def claim(
    ledger_graph: str, incident_id: str, from_state: State | str, to_state: State | str
) -> bool:
    """Atomic compare-and-set; exactly one concurrent caller gets True."""
    frm, to = State(from_state), State(to_state)
    if to not in ALLOWED[frm]:
        raise IllegalTransition(f"{frm.value} -> {to.value}")
    res = await query(
        ledger_graph,
        "MATCH (i:LedgerIncident {id: $id}) WHERE i.state = $from "
        "SET i.state = $to, i.updated_at = $now RETURN count(i)",
        {"id": incident_id, "from": frm.value, "to": to.value, "now": time.time()},
    )
    return bool(res.result_set and res.result_set[0][0] == 1)


async def transition(ledger_graph: str, incident_id: str, to_state: State | str) -> State:
    """Move to ``to_state`` if legal from the current state (node created as OPEN).

    A ``LedgerIncident`` opened by the proof ledger carries no state yet: it counts as OPEN."""
    to = State(to_state)
    await query(
        ledger_graph,
        "MERGE (i:LedgerIncident {id: $id}) "
        "SET i.state = coalesce(i.state, $open), i.updated_at = coalesce(i.updated_at, $now)",
        {"id": incident_id, "open": State.OPEN.value, "now": time.time()},
    )
    cur = await get_state(ledger_graph, incident_id) or State.OPEN
    if to not in ALLOWED[cur]:
        raise IllegalTransition(f"{cur.value} -> {to.value}")
    if not await claim(ledger_graph, incident_id, cur, to):
        raise IllegalTransition(f"{cur.value} -> {to.value} (state changed concurrently)")
    return to
