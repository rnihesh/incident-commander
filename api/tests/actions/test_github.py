import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from ic.actions import ActionError
from ic.actions.github import GitHubClient, parse_manifest
from ic.actions.text import ZWSP

REPO = "acme/sandbox"


def change(kind="deploy", **kw):
    base = dict(
        id="chg-00042",
        kind=kind,
        service_id="svc-payments@us-east",
        summary="bump",
        value="2.0.0",
        prev_value="1.9.0",
        touches_id=None,
        target_key="deploy",
    )
    return SimpleNamespace(**{**base, **kw})


class FakeGitHub:
    """Just enough of GitHub REST v3 to drive the PR flow; records every request."""

    def __init__(self, manifest: str | None = None, prs: list | None = None):
        self.requests: list[httpx.Request] = []
        self.manifest = manifest
        self.prs = prs or []
        self.put_body: dict | None = None
        self.pr_body: dict | None = None
        self.patched: list[int] = []
        self.deleted: list[str] = []
        self.fail_patch: set[int] = set()

    @property
    def sequence(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p, m = req.url.path, req.method
        assert req.headers["authorization"] == "Bearer tok"
        if m == "GET" and p == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if m == "GET" and p.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "b" * 40}})
        if m == "POST" and p.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if p.endswith(".yaml") and m == "GET":
            if self.manifest is None:
                return httpx.Response(404, json={"message": "Not Found"})
            enc = base64.b64encode(self.manifest.encode()).decode()
            return httpx.Response(200, json={"sha": "f" * 40, "content": enc})
        if p.endswith(".yaml") and m == "PUT":
            self.put_body = json.loads(req.content)
            return httpx.Response(200, json={"commit": {"sha": "c" * 40}})
        if m == "POST" and p.endswith("/pulls"):
            self.pr_body = json.loads(req.content)
            return httpx.Response(
                201, json={"number": 7, "html_url": f"https://github.com/{REPO}/pull/7"}
            )
        if m == "GET" and p.endswith("/pulls"):
            q = req.url.params
            page, per = int(q.get("page", 1)), int(q.get("per_page", 100))
            return httpx.Response(200, json=self.prs[(page - 1) * per : page * per])
        if m == "PATCH" and "/pulls/" in p:
            n = int(p.rsplit("/", 1)[1])
            if n in self.fail_patch:
                return httpx.Response(422, json={"message": "nope"})
            self.patched.append(n)
            return httpx.Response(200, json={})
        if m == "DELETE" and "/git/refs/heads/" in p:
            self.deleted.append(p.split("/git/refs/heads/")[1])
            return httpx.Response(204)
        if m == "POST" and "/statuses/" in p:
            return httpx.Response(201, json={"id": 1, **json.loads(req.content)})
        raise AssertionError(f"unexpected {m} {p}")


def client(fake, **kw):
    return GitHubClient("tok", REPO, transport=httpx.MockTransport(fake), **kw)


PATH = ["chg-00042", "svc-payments@us-east", "svc-web@us-east"]


async def open_pr(fake, ch=None, previous=None, **kw):
    return await client(fake).open_rollback_pr(
        "inc-007",
        ch or change(),
        previous,
        PATH,
        "ab" * 32,
        "https://app.example/proof/inc-007",
        **kw,
    )


async def test_pr_call_sequence_and_result():
    fake = FakeGitHub(manifest="service: payments\nversion: 2.0.0\n")
    res = await open_pr(fake)
    assert fake.sequence == [
        f"GET /repos/{REPO}",
        f"GET /repos/{REPO}/git/ref/heads/main",
        f"POST /repos/{REPO}/git/refs",
        f"GET /repos/{REPO}/contents/services/payments.yaml",
        f"PUT /repos/{REPO}/contents/services/payments.yaml",
        f"POST /repos/{REPO}/pulls",
    ]
    ref_req = json.loads(fake.requests[2].content)
    assert ref_req == {"ref": "refs/heads/ic/rollback-inc-007-chg-00042", "sha": "b" * 40}
    assert fake.put_body["branch"] == "ic/rollback-inc-007-chg-00042"
    assert fake.put_body["sha"] == "f" * 40  # updating the existing file
    assert (
        fake.pr_body["head"] == "ic/rollback-inc-007-chg-00042" and fake.pr_body["base"] == "main"
    )
    assert (res.number, res.head_sha, res.simulated) == (7, "c" * 40, False)
    assert res.url.endswith("/pull/7")


def written(fake) -> dict:
    return parse_manifest(base64.b64decode(fake.put_body["content"]).decode())


async def test_deploy_restores_previous_version():
    fake = FakeGitHub(manifest="service: payments\nversion: 2.0.0\nconfig:\n  a: '1'\n")
    await open_pr(fake)
    m = written(fake)
    assert m["version"] == "1.9.0" and "a" in m["config"]


async def test_previous_change_value_wins_over_prev_value():
    fake = FakeGitHub(manifest="version: 2.0.0\n")
    await open_pr(fake, previous=SimpleNamespace(value="1.8.5"))
    assert written(fake)["version"] == "1.8.5"


async def test_config_restores_prev_value_under_config():
    fake = FakeGitHub(manifest="service: payments\nversion: 2.0.0\nconfig:\n  timeout: 30\n")
    ch = change("config", value="60", prev_value="30", touches_id="cfg-00001")
    await open_pr(fake, ch, target_name="timeout")
    m = written(fake)
    assert m["config"] == {"timeout": "30"} and m["version"] == "2.0.0"


async def test_flag_restores_prev_value_under_flags():
    fake = FakeGitHub(manifest=None)
    ch = change("flag", value="on", prev_value="off", touches_id="flg-1")
    await open_pr(fake, ch, target_name="new_checkout")
    assert "sha" not in fake.put_body  # file created
    m = written(fake)
    assert m["flags"] == {"new_checkout": "off"} and m["service"] == "payments"


async def test_missing_manifest_gets_minimal_one():
    fake = FakeGitHub(manifest=None)
    await open_pr(fake)
    assert written(fake) == {"service": "payments", "version": "1.9.0"}


async def test_pr_body_has_causal_path_chain_head_and_proof_link():
    fake = FakeGitHub(manifest=None)
    await open_pr(fake, extra_sections={"Recommendation": "see ADR-3"})
    body = fake.pr_body["body"]
    for hop in PATH:
        assert f"`{hop}`" in body
    assert "ab" * 32 in body and "https://app.example/proof/inc-007" in body
    assert "## Root cause" in body and "## Recommendation" in body
    assert body.index("`chg-00042`") < body.index("`svc-web@us-east`")


async def test_hostile_text_is_inert_in_pr_body():
    fake = FakeGitHub(manifest=None)
    evil = "@someone @everyone [x](http://evil) `break` ![i](http://e/i.png) \x00"
    await open_pr(fake, change(summary=evil), extra_sections={"LLM summary": evil})
    body = fake.pr_body["body"]
    assert "@someone" not in body and "@everyone" not in body
    assert f"@{ZWSP}someone" in body
    assert "[x](http://evil)" not in body and "![i](" not in body and "\x00" not in body
    # backticks cannot close the code span early: the fence is longer than any run inside
    assert "``" in body


async def test_status_payloads():
    fake = FakeGitHub()
    c = client(fake)
    for state in ("success", "failure", "pending"):
        out = await c.post_proof_status("c" * 40, state, f"proofs {state}", "https://app/p")
        assert out["state"] == state and not out["simulated"]
        req = fake.requests[-1]
        assert req.url.path == f"/repos/{REPO}/statuses/{'c' * 40}"
        body = json.loads(req.content)
        assert body["context"] == "incident-commander/proofs"
        assert body["target_url"] == "https://app/p" and body["state"] == state
    with pytest.raises(ValueError):
        await c.post_proof_status("c" * 40, "error", "x", "u")


async def test_status_description_truncated():
    fake = FakeGitHub()
    await client(fake).post_proof_status("c" * 40, "success", "z" * 500, "u")
    assert len(json.loads(fake.requests[-1].content)["description"]) <= 140


def pr(number, ref, created, repo=REPO):
    return {
        "number": number,
        "head": {"ref": ref, "repo": {"full_name": repo}},
        "created_at": created,
    }


async def test_close_stale_prs_only_old_rollback_prs():
    now = 1_800_000_000

    def iso(t):
        return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    fake = FakeGitHub(
        prs=[
            pr(1, "ic/rollback-inc-1-chg-1", iso(now - 7200)),  # old, ours -> close
            pr(2, "ic/rollback-inc-2-chg-2", iso(now - 600)),  # fresh -> keep
            pr(3, "feature/old", iso(now - 99999)),  # old but not ours -> keep
        ]
    )
    closed = await client(fake).close_stale_prs(3600, now=now)
    assert closed == [1] and fake.patched == [1]
    assert fake.deleted == ["ic/rollback-inc-1-chg-1"]


async def test_mock_mode_no_network_and_deterministic():
    def boom(req):
        raise AssertionError("network used")

    for c in (
        GitHubClient("tok", REPO, transport=httpx.MockTransport(boom), mock=True),
        GitHubClient(None, REPO, transport=httpx.MockTransport(boom)),
        GitHubClient("tok", None, transport=httpx.MockTransport(boom)),
    ):
        a = await c.open_rollback_pr("inc-1", change(), None, PATH, "h", "https://p")
        b = await c.open_rollback_pr("inc-1", change(), None, PATH, "h", "https://p")
        assert a.simulated and a == b and len(a.head_sha) == 40
        s = await c.post_proof_status(a.head_sha, "success", "ok", "https://p")
        assert s["simulated"] and await c.close_stale_prs() == []
        assert [x["op"] for x in c.calls] == [
            "open_rollback_pr",
            "open_rollback_pr",
            "post_proof_status",
            "close_stale_prs",
        ]
        assert c.calls[0]["branch"] == "ic/rollback-inc-1-chg-00042"


async def test_exhausted_budget_simulates():
    class Refuse:
        def allow(self, kind):
            assert kind == "pr"
            return False

    def boom(req):
        raise AssertionError("network used")

    c = GitHubClient("tok", REPO, transport=httpx.MockTransport(boom), budget=Refuse())
    res = await c.open_rollback_pr("inc-1", change(), None, PATH, "h", "https://p")
    assert res.simulated


@pytest.mark.parametrize(
    ("resp", "retryable"),
    [
        (httpx.Response(500, json={"message": "boom"}), True),
        (httpx.Response(502), True),
        (httpx.Response(429, json={"message": "slow down"}), True),
        (httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}), True),
        (httpx.Response(403, headers={"retry-after": "30"}), True),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), True),
        (httpx.Response(403, json={"message": "Resource not accessible"}), False),
        (httpx.Response(401, json={"message": "Bad credentials"}), False),
        (httpx.Response(404), False),
        (httpx.Response(422, json={"message": "Validation Failed"}), False),
    ],
)
async def test_error_classification(resp, retryable):
    c = GitHubClient("tok", REPO, transport=httpx.MockTransport(lambda r: resp))
    with pytest.raises(ActionError) as ei:
        await c.open_rollback_pr("inc-1", change(), None, PATH, "h", "https://p")
    assert ei.value.retryable is retryable and ei.value.status == resp.status_code
    assert "tok" not in str(ei.value)


async def test_network_error_retryable():
    def h(req):
        raise httpx.ConnectError("down")

    c = GitHubClient("tok", REPO, transport=httpx.MockTransport(h))
    with pytest.raises(ActionError) as ei:
        await c.post_proof_status("c" * 40, "success", "x", "u")
    assert ei.value.retryable


async def test_branch_already_exists_is_reused():
    fake = FakeGitHub(manifest=None)
    orig = fake.__call__

    def h(req):
        if req.method == "POST" and req.url.path.endswith("/git/refs"):
            fake.requests.append(req)
            return httpx.Response(422, json={"message": "Reference already exists"})
        return orig(req)

    res = await client(h).open_rollback_pr("inc-1", change(), None, PATH, "h", "https://p")
    assert res.number == 7


@pytest.mark.parametrize(
    "overrides",
    [
        {"service_id": "svc-../.github/workflows/pwn@us-east"},
        {"service_id": "svc-a/b@us-east"},
        {"id": "chg-1/../../main"},
        {"id": "chg 1"},
    ],
)
async def test_graph_derived_names_are_validated_before_any_write(overrides):
    """Service and change ids come from a judge-editable graph; they must never shape
    a repository path or ref (path traversal could write .github/workflows/*)."""
    fake = FakeGitHub()
    with pytest.raises(ActionError) as e:
        await open_pr(fake, change(**overrides))
    assert e.value.retryable is False
    assert not [r for r in fake.requests if r.method in ("PUT", "POST", "PATCH", "DELETE")]


async def test_incident_id_is_validated():
    fake = FakeGitHub()
    with pytest.raises(ActionError):
        await client(fake).open_rollback_pr(
            "inc-7/../x", change(), None, PATH, "ab" * 32, "https://app.example/p"
        )
    assert not [r for r in fake.requests if r.method != "GET"]


@pytest.mark.parametrize("target", ["evil\nversion: 0.0.1", "a:b", "x y"])
async def test_manifest_key_names_are_validated(target):
    fake = FakeGitHub()
    with pytest.raises(ActionError):
        await open_pr(fake, change(kind="config"), target_name=target)
    assert not [r for r in fake.requests if r.method in ("PUT", "POST")]


async def test_close_stale_prs_ignores_fork_prs():
    now = 1_800_000_000
    old = datetime.fromtimestamp(now - 7200, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = FakeGitHub(prs=[pr(9, "ic/rollback-inc-9-chg-9", old, repo="mallory/sandbox")])
    assert await client(fake).close_stale_prs(3600, now=now) == []
    assert fake.patched == [] and fake.deleted == []


async def test_branch_suffix_makes_rollback_branches_unique_per_session():
    fake = FakeGitHub()
    res = await open_pr(fake, branch_suffix="a1b2c3d4e5f6")
    assert res.url
    refs = [json.loads(r.content)["ref"] for r in fake.requests if r.url.path.endswith("/git/refs")]
    assert refs == ["refs/heads/ic/rollback-inc-007-chg-00042-a1b2c3d4e5f6"]


async def test_branch_suffix_is_validated():
    fake = FakeGitHub()
    with pytest.raises(ActionError):
        await open_pr(fake, branch_suffix="../main")
    assert not [r for r in fake.requests if r.method != "GET"]


async def test_retryable_502_then_success_retries():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(502)
        return httpx.Response(201, json={"id": 1})

    c = GitHubClient("tok", REPO, transport=httpx.MockTransport(h))
    out = await c.post_proof_status("c" * 40, "success", "x", "u")
    assert n["c"] == 2 and out["simulated"] is False


async def test_retry_gives_up_after_three_attempts():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(503)

    c = GitHubClient("tok", REPO, transport=httpx.MockTransport(h))
    with pytest.raises(ActionError) as ei:
        await c.post_proof_status("c" * 40, "success", "x", "u")
    assert n["c"] == 3 and ei.value.retryable


async def test_422_is_not_retried():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(422, json={"message": "Validation Failed"})

    c = GitHubClient("tok", REPO, transport=httpx.MockTransport(h))
    with pytest.raises(ActionError):
        await c.post_proof_status("c" * 40, "success", "x", "u")
    assert n["c"] == 1


def _iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_close_stale_prs_continues_past_failing_pr():
    now = 1_800_000_000
    fake = FakeGitHub(
        prs=[
            pr(1, "ic/rollback-inc-1-chg-1", _iso(now - 7200)),
            pr(2, "ic/rollback-inc-2-chg-2", _iso(now - 7200)),
        ]
    )
    fake.fail_patch = {1}
    closed = await client(fake).close_stale_prs(3600, now=now)
    assert closed == [2] and fake.patched == [2]


async def test_close_stale_prs_paginates_until_empty_page():
    now = 1_800_000_000
    prs = [pr(i, f"ic/rollback-inc-{i}-chg-{i}", _iso(now - 7200)) for i in range(1, 251)]
    fake = FakeGitHub(prs=prs)
    closed = await client(fake).close_stale_prs(3600, now=now)
    assert closed == list(range(1, 251))
