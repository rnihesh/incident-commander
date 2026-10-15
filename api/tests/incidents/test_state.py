import asyncio

import pytest

from ic.graph import client, naming
from ic.incidents.state import ALLOWED, IllegalTransition, State, claim, get_state, transition


@pytest.fixture
async def ledger():
    client.reset_client()
    name = naming.ledger(naming.new_session_id())
    yield name
    await client.delete_graph(name)
    client.reset_client()


async def test_legal_path(ledger):
    for s in (
        "TRIAGING",
        "DIAGNOSED",
        "REHEARSING",
        "ACTING",
        "PR_OPEN",
        "RESOLVED",
        "LEARNED",
    ):
        assert await transition(ledger, "inc-1", s) == State(s)
        assert await get_state(ledger, "inc-1") == State(s)


async def test_new_node_starts_open_and_branches(ledger):
    await transition(ledger, "inc-2", State.TRIAGING)
    await transition(ledger, "inc-2", State.INCONCLUSIVE)
    with pytest.raises(IllegalTransition):
        await transition(ledger, "inc-2", State.DIAGNOSED)
    await transition(ledger, "inc-3", "TRIAGING")
    await transition(ledger, "inc-3", "FAILED")


async def test_illegal_transition_raises(ledger):
    with pytest.raises(IllegalTransition):
        await transition(ledger, "inc-1", State.ACTING)
    assert await get_state(ledger, "inc-1") == State.OPEN
    with pytest.raises(IllegalTransition):
        await claim(ledger, "inc-1", State.OPEN, State.LEARNED)


def test_table_covers_all_states():
    assert set(ALLOWED) == set(State)


async def test_concurrent_claim_exactly_one_wins(ledger):
    await transition(ledger, "inc-1", "TRIAGING")
    await transition(ledger, "inc-1", "DIAGNOSED")
    res = await asyncio.gather(
        *(claim(ledger, "inc-1", State.DIAGNOSED, State.REHEARSING) for _ in range(2))
    )
    assert sorted(res) == [False, True]
    assert await get_state(ledger, "inc-1") == State.REHEARSING
