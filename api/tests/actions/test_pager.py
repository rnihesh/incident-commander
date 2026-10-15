import json

import httpx
import pytest

from ic.actions import ActionError, pager, retry
from ic.actions.pager import DiscordPager

URL = "https://discord.com/api/webhooks/1/secret"


def make(handler, **kw):
    return DiscordPager(URL, transport=httpx.MockTransport(handler), **kw)


ARGS = dict(
    incident_id="inc-1",
    title="DB down",
    root_cause="deploy c-1",
    paged_person="Ann",
    reason="on-call at T",
    pr_url="https://github.com/o/r/pull/1",
    proof_status="VERIFIED",
)


async def test_embed_shape_and_wait_param():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "42"})

    res = await make(h).page(**ARGS)
    assert not res.simulated and res.message_id == "42"
    req = seen[0]
    assert req.url.params["wait"] == "true"
    body = json.loads(req.content)
    assert body["allowed_mentions"] == {"parse": []}
    e = body["embeds"][0]
    assert e["title"].startswith("Incident inc-1")
    assert [f["name"] for f in e["fields"]] == ["Root cause", "Paged", "PR", "Proof status"]
    assert "on-call at T" in e["fields"][1]["value"]
    assert e["color"] == pager.GREEN and e["timestamp"]


def test_color_by_status():
    assert pager.color_for("FALSIFIED") == pager.RED
    assert pager.color_for("failure") == pager.RED
    assert pager.color_for("pending") == pager.AMBER


async def test_text_inert_and_limits():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(204)

    long = "@everyone [x](http://evil) " + "y" * 5000
    await make(h).page(
        **{**ARGS, "title": long, "root_cause": long},
        extra_fields={f"f{i}": long for i in range(10)},
    )
    e = seen[0]["embeds"][0]
    assert len(e["title"]) <= 256
    assert all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in e["fields"])
    assert len(e["fields"]) <= 25
    total = len(e["title"]) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"])
    assert total <= 6000
    text = json.dumps(e, ensure_ascii=False)
    assert "@everyone" not in text and "[x](" not in text
    assert [f["name"] for f in e["fields"][:4]] == ["Root cause", "Paged", "PR", "Proof status"]


async def test_429_retries_once_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, json={"retry_after": 0.5})
        return httpx.Response(200, json={"id": "1"})

    res = await make(h).page(**ARGS)
    assert n["c"] == 2 and sleeps == [0.5] and not res.simulated


async def test_429_always_raises_retryable_after_three_attempts(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429, json={"retry_after": 0})

    with pytest.raises(ActionError) as ei:
        await make(h).page(**ARGS)
    assert n["c"] == 3 and ei.value.retryable and "secret" not in str(ei.value)


async def test_400_not_retryable_502_retryable():
    with pytest.raises(ActionError) as e1:
        await make(lambda r: httpx.Response(400)).page(**ARGS)
    assert not e1.value.retryable
    with pytest.raises(ActionError) as e2:
        await make(lambda r: httpx.Response(502)).page(**ARGS)
    assert e2.value.retryable


async def test_502_then_200_retries():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(502) if n["c"] == 1 else httpx.Response(200, json={"id": "9"})

    res = await make(h).page(**ARGS)
    assert n["c"] == 2 and res.message_id == "9"


async def test_400_not_retried():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(400)

    with pytest.raises(ActionError):
        await make(h).page(**ARGS)
    assert n["c"] == 1


async def test_mock_mode_makes_no_requests():
    def boom(req):
        raise AssertionError("network used")

    for p in (make(boom, mock=True), DiscordPager(None, transport=httpx.MockTransport(boom))):
        res = await p.page(**ARGS)
        assert res.simulated and p.calls[0]["payload"]["allowed_mentions"] == {"parse": []}


async def test_page_defangs_urls_but_keeps_our_pr_link():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(204)

    await make(h).page(**{**ARGS, "root_cause": "see https://evil.example/x www.evil.example"})
    f = {x["name"]: x["value"] for x in seen[0]["embeds"][0]["fields"]}
    assert "://" not in f["Root cause"] and "www." not in f["Root cause"]
    assert f["PR"] == "https://github.com/o/r/pull/1"
    await make(h).page(**{**ARGS, "pr_url": "https://evil.example/pull/1"})
    pr = {x["name"]: x["value"] for x in seen[1]["embeds"][0]["fields"]}["PR"]
    assert "://" not in pr
