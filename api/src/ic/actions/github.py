"""GitHub rollback PR and proof commit status over REST v3 with httpx (KTD19, R17, R18).

Error policy (``ActionError.retryable``): network failures, 5xx, 429 and 403 responses that
look like a (secondary) rate limit are retryable; every other non-2xx (401, 403, 404, 422,
...) is not, because repeating the same request cannot succeed.

Mock mode (no token, ``mock=True``, or the action budget refuses) makes no network call,
returns deterministic fake results with ``simulated=True`` and records the call in
``self.calls`` for the UI.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ic.actions.errors import ActionError
from ic.actions.retry import retry_action
from ic.actions.text import code_span, inert
from ic.sessions.action_budget import ActionBudget

log = logging.getLogger(__name__)

API = "https://api.github.com"
BRANCH_PREFIX = "ic/rollback-"
STATUS_CONTEXT = "incident-commander/proofs"
STATES = ("success", "failure", "pending")
TEXT_MAX = 300


@dataclass
class PrResult:
    number: int
    url: str
    head_sha: str
    simulated: bool


# --- manifest (a tiny two-level YAML subset: scalars plus config:/flags: maps) -----------

_PLAIN = re.compile(r"[A-Za-z0-9._+/-]+")
# Graph-derived names (judge-editable) that shape repo paths, refs or manifest keys must
# match these; anything else is refused before any write (no traversal, no YAML keys).
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _require(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise ActionError(f"refusing unsafe {what}: {value[:80]!r}", retryable=False)
    return value


def _scalar(v: str) -> str:
    return v if _PLAIN.fullmatch(v) else json.dumps(v)


def _unscalar(v: str) -> str:
    v = v.strip()
    return json.loads(v) if v.startswith('"') else v


def parse_manifest(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    section: dict[str, str] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, _, val = raw.strip().partition(":")
        if raw[0] in " \t" and section is not None:
            section[key.strip()] = _unscalar(val)
        elif val.strip() == "":
            section = data.setdefault(key, {})
            if not isinstance(section, dict):
                section = data[key] = {}
        else:
            section = None
            data[key] = _unscalar(val)
    return data


def render_manifest(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, val in data.items():
        if isinstance(val, dict):
            lines.append(f"{key}:")
            lines += [f"  {k}: {_scalar(str(v))}" for k, v in val.items()]
        else:
            lines.append(f"{key}: {_scalar(str(val))}")
    return "\n".join(lines) + "\n"


def service_name_of(change: Any, service_name: str | None = None) -> str:
    """Manifest name: explicit, else the generator id ``svc-<name>@<region>`` reduced to name."""
    if service_name:
        return service_name
    sid = str(change.service_id)
    sid = sid.removeprefix("svc-")
    return sid.split("@", 1)[0]


def apply_rollback(
    manifest: dict[str, Any], change: Any, prev_value: str, target_name: str | None
) -> dict[str, Any]:
    """Return ``manifest`` with the change's target restored to its pre-change value."""
    out = dict(manifest)
    kind = str(change.kind)
    if kind == "deploy":
        out["version"] = prev_value
        return out
    section = {"config": "config", "flag": "flags"}.get(kind)
    if section is None:
        raise ActionError(f"cannot roll back a {kind!r} change with a manifest", retryable=False)
    name = _require(
        _SAFE_KEY,
        str(target_name or getattr(change, "touches_id", None) or change.target_key),
        "manifest key",
    )
    sec = dict(out.get(section) or {})
    sec[str(name)] = prev_value
    out[section] = sec
    return out


# --- client -----------------------------------------------------------------------------


def _fake_sha(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()  # noqa: S324 - fake id


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        repo: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        mock: bool = False,
        budget: ActionBudget | None = None,
    ):
        self.token = token
        self.repo = repo
        self.transport = transport
        self.mock = mock or not token or not repo
        self.budget = budget
        self.calls: list[dict[str, Any]] = []  # mock-mode record, for the UI

    # transport -----------------------------------------------------------------------
    async def _req(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        """One request, retried (3 attempts, backoff + jitter) on retryable failures only."""
        return await retry_action(lambda: self._req_once(method, path, ok=ok, **kw))

    async def _req_once(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "incident-commander",
        }
        try:
            async with httpx.AsyncClient(
                base_url=API, headers=headers, transport=self.transport, timeout=15.0
            ) as c:
                r = await c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ActionError(f"GitHub {method} {path}: {type(e).__name__}", retryable=True) from e
        if r.status_code in ok:
            return r
        raise ActionError(
            f"GitHub {method} {path} -> {r.status_code}: {self._detail(r)}",
            retryable=self._retryable(r),
            status=r.status_code,
        )

    @staticmethod
    def _detail(r: httpx.Response) -> str:
        try:
            msg = r.json().get("message", "")
        except Exception:
            msg = ""
        return inert(str(msg), 200)

    @staticmethod
    def _retryable(r: httpx.Response) -> bool:
        if r.status_code >= 500 or r.status_code == 429:
            return True
        if r.status_code == 403:
            body = r.text.lower()
            return (
                "retry-after" in r.headers
                or r.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in body
                or "abuse" in body
            )
        return False

    def _record(self, op: str, **kw: Any) -> None:
        self.calls.append({"op": op, "at": time.time(), **kw})

    # operations ----------------------------------------------------------------------
    async def open_rollback_pr(
        self,
        incident_id: str,
        change: Any,
        previous: Any | None,
        causal_path: Sequence[str],
        chain_head: str,
        proof_url: str,
        extra_sections: dict[str, str] | None = None,
        *,
        service_name: str | None = None,
        target_name: str | None = None,
        branch_suffix: str | None = None,
    ) -> PrResult:
        _require(_SAFE_ID, str(incident_id), "incident id")
        _require(_SAFE_ID, str(change.id), "change id")
        branch = f"{BRANCH_PREFIX}{incident_id}-{change.id}"
        if branch_suffix is not None:
            # Incident and change ids repeat across sessions; the suffix (a per-incident
            # random proof ref) keeps one session from reusing another's branch and PR.
            branch += "-" + _require(_SAFE_ID, branch_suffix, "branch suffix")
        prev_value = str(previous.value if previous is not None else change.prev_value)
        name = _require(_SAFE_NAME, service_name_of(change, service_name), "service name")
        if str(change.kind) != "deploy":  # validate the manifest key before any network call
            apply_rollback({}, change, prev_value, target_name)
        body = self._pr_body(change, causal_path, chain_head, proof_url, extra_sections)
        title = f"Rollback {change.id} for {incident_id}"

        simulate = self.mock or (self.budget is not None and not self.budget.allow("pr"))
        if simulate:
            n = 1000 + int(_fake_sha("pr", incident_id, change.id)[:6], 16) % 9000
            res = PrResult(
                number=n,
                url=f"https://github.com/{self.repo or 'sandbox/demo'}/pull/{n}",
                head_sha=_fake_sha("head", incident_id, change.id, prev_value),
                simulated=True,
            )
            self._record(
                "open_rollback_pr",
                branch=branch,
                title=title,
                body=body,
                path=f"services/{name}.yaml",
                prev_value=prev_value,
                result=res,
            )
            return res

        repo = self.repo
        info = (await self._req("GET", f"/repos/{repo}")).json()
        base = info["default_branch"]
        ref = (await self._req("GET", f"/repos/{repo}/git/ref/heads/{base}")).json()
        base_sha = ref["object"]["sha"]
        try:
            await self._req(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        except ActionError as e:
            if e.status != 422:  # 422: branch already exists (a retry); reuse it
                raise
        path = f"services/{name}.yaml"
        existing = await self._req(
            "GET", f"/repos/{repo}/contents/{path}", ok=(200, 404), params={"ref": branch}
        )
        sha = None
        manifest: dict[str, Any] = {"service": name}
        if existing.status_code == 200:
            j = existing.json()
            sha = j["sha"]
            text = base64.b64decode(j["content"]).decode()
            manifest = parse_manifest(text) or manifest
        manifest = apply_rollback(manifest, change, prev_value, target_name)
        put: dict[str, Any] = {
            "message": f"Rollback {change.id}: restore pre-change value ({incident_id})",
            "content": base64.b64encode(render_manifest(manifest).encode()).decode(),
            "branch": branch,
        }
        if sha:
            put["sha"] = sha
        commit = (await self._req("PUT", f"/repos/{repo}/contents/{path}", json=put)).json()
        head_sha = commit["commit"]["sha"]
        pr = (
            await self._req(
                "POST",
                f"/repos/{repo}/pulls",
                json={"title": title, "head": branch, "base": base, "body": body},
            )
        ).json()
        return PrResult(
            number=pr["number"],
            url=pr["html_url"],
            head_sha=pr.get("head", {}).get("sha") or head_sha,
            simulated=False,
        )

    @staticmethod
    def _pr_body(
        change: Any,
        causal_path: Sequence[str],
        chain_head: str,
        proof_url: str,
        extra_sections: dict[str, str] | None,
    ) -> str:
        def cs(s: Any) -> str:
            return code_span(inert(str(s), TEXT_MAX))

        def ident(x: Any) -> str:  # server-generated ids: a code span already blocks mentions
            return code_span(str(x)[:TEXT_MAX])

        hops = "\n".join(f"{i}. {ident(h)}" for i, h in enumerate(causal_path, 1)) or "_none_"
        summary = getattr(change, "summary", "")
        parts = [
            "## Root cause",
            f"{ident(change.kind)} change {ident(change.id)} on {ident(change.service_id)}: "
            f"{cs(summary)}",
            "## Causal path",
            hops,
            "## Proof chain head hash",
            ident(chain_head),
            "## Proof page",
            proof_url if re.match(r"https?://\S+$", proof_url) else cs(proof_url),
        ]
        for heading, text in (extra_sections or {}).items():
            parts += [f"## {inert(heading, 80)}", cs(text)]
        return "\n\n".join(parts) + "\n"

    async def post_proof_status(
        self, head_sha: str, state: str, description: str, target_url: str
    ) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"invalid state: {state!r}")
        payload = {
            "state": state,
            "description": inert(description, 140),
            "target_url": target_url,
            "context": STATUS_CONTEXT,
        }
        if self.mock:
            self._record("post_proof_status", head_sha=head_sha, **payload)
            return {"simulated": True, **payload}
        r = await self._req("POST", f"/repos/{self.repo}/statuses/{head_sha}", json=payload)
        return {"simulated": False, **r.json()}

    async def close_stale_prs(
        self, older_than_s: float = 3600, *, now: float | None = None
    ) -> list[int]:
        """Close open PRs on ``ic/rollback-`` branches older than the cutoff and delete them."""
        if self.mock:
            self._record("close_stale_prs", older_than_s=older_than_s)
            return []
        cutoff = (time.time() if now is None else now) - older_than_s
        closed: list[int] = []
        page = 1
        while True:
            r = await self._req(
                "GET",
                f"/repos/{self.repo}/pulls",
                params={"state": "open", "per_page": 100, "page": page},
            )
            prs = r.json()
            if not prs:
                break
            for pr in prs:
                ref = pr["head"]["ref"]
                head_repo = (pr["head"].get("repo") or {}).get("full_name")
                # Only our own branches: a fork can name its branch ic/rollback-* too.
                if not ref.startswith(BRANCH_PREFIX) or head_repo != self.repo:
                    continue
                created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                if created.astimezone(UTC).timestamp() > cutoff:
                    continue
                try:
                    await self._req(
                        "PATCH",
                        f"/repos/{self.repo}/pulls/{pr['number']}",
                        json={"state": "closed"},
                    )
                    await self._req(
                        "DELETE",
                        f"/repos/{self.repo}/git/refs/heads/{ref}",
                        ok=(200, 204, 404, 422),
                    )
                except ActionError:
                    log.exception("closing stale PR #%s failed", pr["number"])
                    continue
                closed.append(pr["number"])
            page += 1
        return closed
