"""Company tools (Ask the graph, KTD1): read-only world-graph lookups over people, teams,
services, changes, incidents and decisions, each with its Cypher (see company_world)."""

import pytest
from company_world import (
    CHECKOUT,
    CHECKOUT_API,
    DAY,
    LIMITER,
    NOW,
    SEARCH,
    all_ids,
    counts,
    write_company,
)

from ic.graph import naming
from ic.tools.registry import ToolContext, call_tool, get_tool
from ic.tools.result import ToolError, ToolResult

COMPANY_TOOLS = (
    "find_entities",
    "service_profile",
    "team_profile",
    "person_profile",
    "decisions_for",
    "incident_history",
)


@pytest.fixture(scope="module")
async def world(module_sid):
    g = naming.session_world(module_sid)
    await write_company(g)
    return module_sid, g


async def call(world, name: str, **args) -> ToolResult:
    sid, g = world
    return await call_tool(name, args, ToolContext(sid=sid, world_graph=g))


def ids(rows: list[dict], key: str = "id") -> list[str]:
    return [r[key] for r in rows]


async def test_company_tools_are_read_only_world_tools():
    for name in COMPANY_TOOLS:
        spec = get_tool(name)
        assert spec.read_only and spec.target == "world", name


async def test_find_entities_is_fuzzy_and_ranks_exact_names_first(world):
    r = await call(world, "find_entities", search="checkout")
    found = ids(r.result["matches"])
    assert found[0] == CHECKOUT  # name == "checkout" beats name contains "checkout"
    assert CHECKOUT_API in found
    assert r.result["matches"][0] == {
        "id": CHECKOUT,
        "kind": "Service",
        "label": "checkout",
        "hits": 1,
        "exact": True,
        "at": None,
    }

    r = await call(world, "find_entities", search="Who changed the rate limiter recently?")
    assert ids(r.result["matches"])[0] == LIMITER  # stop words dropped, words joined
    assert "rate-limiter" in r.result["terms"]

    r = await call(world, "find_entities", search="ada")
    assert ids(r.result["matches"]) == ["per-0001"]

    r = await call(world, "find_entities", search="search", kinds=["Incident"])
    assert ids(r.result["matches"]) == ["inc-002"]
    assert r.result["matches"][0]["at"] == NOW - 5 * DAY + 3600  # incidents carry a date

    r = await call(world, "find_entities", search="zebra unicorn")
    assert r.result["matches"] == []


async def test_service_profile_owner_on_call_changes_incidents(world):
    r = await call(world, "service_profile", service_id=CHECKOUT_API)
    p = r.result
    assert p["service"]["name"] == "checkout-api"
    assert p["as_of"] == NOW
    assert p["owner"] == {"team_id": "team-pay", "team_name": "Payments Core"}  # valid now
    assert ids(p["on_call"], "person_id") == ["per-0001"]
    assert ids(p["recent_changes"]) == ["chg-001", "chg-002"]  # newest first
    assert p["recent_changes"][1]["author"] == {"person_id": "per-0002", "name": "Ben Ito"}
    assert p["incidents"] == [
        {
            "id": "inc-001",
            "title": "Failing requests (5xx) on checkout-api",
            "opened_at": NOW - 20 * DAY + 3600,
            "cause_id": "chg-002",
        }
    ]
    assert {
        CHECKOUT_API,
        "team-pay",
        "per-0001",
        "chg-001",
        "chg-002",
        "per-0002",
        "inc-001",
    } <= set(r.nodes_touched)
    assert "team-search" not in r.nodes_touched  # the old owner is not touched

    # asking about the past uses the owner and rotation valid then
    r = await call(world, "service_profile", service_id=CHECKOUT_API, at=NOW - 200 * DAY)
    assert r.result["owner"]["team_id"] == "team-search"
    assert r.result["recent_changes"] == []

    with pytest.raises(ToolError, match="find_entities"):
        await call(world, "service_profile", service_id="svc-nope@us-east")


async def test_team_profile(world):
    r = await call(world, "team_profile", team_id="team-pay")
    p = r.result
    assert p["team"] == {"id": "team-pay", "name": "Payments Core", "domain": "commerce"}
    assert ids(p["members"], "person_id") == ["per-0001", "per-0002"]
    assert ids(p["on_call"], "person_id") == ["per-0001"]
    assert p["services"] == sorted([CHECKOUT_API, CHECKOUT]) and p["service_count"] == 2
    with pytest.raises(ToolError):
        await call(world, "team_profile", team_id="team-nope")


async def test_person_profile(world):
    r = await call(world, "person_profile", person_id="per-0003")
    p = r.result
    assert p["person"] == {"id": "per-0003", "name": "Cy Moss"}
    assert p["team"] == {"team_id": "team-search", "team_name": "Search Platform"}
    assert p["on_call_now"] is True
    assert ids(p["recent_changes"]) == ["chg-003", "chg-004"]
    assert p["recent_changes"][0]["service_id"] == LIMITER
    assert p["incidents_caused"] == [
        {"incident_id": "inc-002", "title": "Latency on search-api", "change_id": "chg-004"}
    ]
    r = await call(world, "person_profile", person_id="per-0002")
    assert r.result["on_call_now"] is False


async def test_decisions_for_service_team_and_incident(world):
    r = await call(world, "decisions_for", entity_id=CHECKOUT_API)
    assert ids(r.result["adrs"]) == ["adr-001"]
    recs = {x["id"]: x for x in r.result["recommendations"]}
    assert set(recs) == {"rec-001", "rec-002"}
    assert recs["rec-001"]["status"] == "open" and recs["rec-001"]["incident_id"] == "inc-001"
    assert recs["rec-002"]["status"] == "closed"

    r = await call(world, "decisions_for", entity_id="team-search")
    assert r.result["kind"] == "Team"
    assert ids(r.result["recommendations"]) == ["rec-003"] and r.result["adrs"] == []

    r = await call(world, "decisions_for", entity_id="inc-002")
    assert r.result["kind"] == "Incident"
    assert r.result["postmortems"] == [{"id": "pm-002", "title": "Latency on search-api"}]
    assert ids(r.result["recommendations"]) == ["rec-003"]
    assert r.result["recommendations"][0]["targets"] == [SEARCH]

    with pytest.raises(ToolError):
        await call(world, "decisions_for", entity_id="chg-001")


async def test_incident_history(world):
    r = await call(world, "incident_history", days=7)
    assert ids(r.result["incidents"]) == ["inc-002"]
    inc = r.result["incidents"][0]
    assert inc["cause"] == {
        "id": "chg-004",
        "kind": "deploy",
        "summary": "Deploy v2.1.0: query planner",
        "author": {"person_id": "per-0003", "name": "Cy Moss"},
    }
    assert inc["services"] == [SEARCH] and inc["postmortem_id"] == "pm-002"

    r = await call(world, "incident_history")
    assert ids(r.result["incidents"]) == ["inc-002", "inc-001"]  # newest first

    r = await call(world, "incident_history", entity_id=CHECKOUT_API)
    assert ids(r.result["incidents"]) == ["inc-001"]
    r = await call(world, "incident_history", entity_id="team-search")
    assert ids(r.result["incidents"]) == ["inc-002"]
    r = await call(world, "incident_history", entity_id="per-0002")  # caused by their change
    assert ids(r.result["incidents"]) == ["inc-001"]


SAMPLES = {
    "find_entities": {"search": "checkout"},
    "service_profile": {"service_id": CHECKOUT_API},
    "team_profile": {"team_id": "team-pay"},
    "person_profile": {"person_id": "per-0003"},
    "decisions_for": {"entity_id": CHECKOUT_API},
    "incident_history": {"entity_id": SEARCH},
}


async def test_every_company_tool_has_evidence_touches_world_ids_and_writes_nothing(world):
    assert set(SAMPLES) == set(COMPANY_TOOLS)
    _, g = world
    before = await counts(g)
    known = await all_ids(g)
    for name, args in SAMPLES.items():
        r = await call(world, name, **args)
        assert r.tool == name and r.graph == g
        assert r.cypher and len(r.params) == len(r.cypher), name
        assert r.nodes_touched and set(r.nodes_touched) <= known, name
        assert "embedding" not in str(r.result), name
    assert await counts(g) == before
