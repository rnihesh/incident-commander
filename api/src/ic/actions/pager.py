"""Discord webhook page (KTD20, R19). Mentions are disabled and all text is made inert."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ic.actions.errors import ActionError
from ic.actions.retry import retry_action
from ic.actions.text import inert_discord
from ic.sessions.action_budget import ActionBudget

TITLE_MAX = 256
NAME_MAX = 256
VALUE_MAX = 1024
TOTAL_MAX = 6000
FIELDS_MAX = 25

GREEN, RED, AMBER = 0x2ECC71, 0xE74C3C, 0xF1C40F
_GOOD = {"VERIFIED", "DRIFTED", "SUCCESS"}
_BAD = {"FALSIFIED", "TAMPERED", "FAILURE"}


@dataclass
class PageResult:
    simulated: bool
    message_id: str | None = None


def color_for(proof_status: str) -> int:
    s = str(proof_status).upper()
    return GREEN if s in _GOOD else RED if s in _BAD else AMBER


def build_embed(
    incident_id: str,
    title: str,
    root_cause: str,
    paged_person: str,
    reason: str,
    pr_url: str,
    proof_status: str,
    extra_fields: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    fields = [
        ("Root cause", root_cause),
        ("Paged", f"{paged_person}: {reason}"),
        ("PR", pr_url),
        ("Proof status", proof_status),
        *(extra_fields or {}).items(),
    ]
    out_fields = [
        {
            "name": inert_discord(n, NAME_MAX) or "-",
            "value": inert_discord(str(v), VALUE_MAX, allow_pr_link=n == "PR") or "-",
            "inline": False,
        }
        for n, v in fields[:FIELDS_MAX]
    ]
    embed = {
        "title": inert_discord(f"Incident {incident_id}: {title}", TITLE_MAX),
        "color": color_for(proof_status),
        "fields": out_fields,
        "timestamp": (now or datetime.now(UTC)).isoformat(),
    }
    # total budget: drop trailing (extra) fields, then trim the last values, until it fits
    while _size(embed) > TOTAL_MAX and len(out_fields) > 4:
        out_fields.pop()
    for f in reversed(out_fields):
        over = _size(embed) - TOTAL_MAX
        if over <= 0:
            break
        keep = max(1, len(f["value"]) - over)
        f["value"] = f["value"][: keep - 1] + "…" if keep > 1 else f["value"][:1]
    return embed


def _size(embed: dict[str, Any]) -> int:
    return len(embed["title"]) + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])


class DiscordPager:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        mock: bool = False,
        budget: ActionBudget | None = None,
    ):
        self.webhook_url = webhook_url
        self.transport = transport
        self.mock = mock or not webhook_url
        self.budget = budget
        self.calls: list[dict[str, Any]] = []

    async def page(
        self,
        incident_id: str,
        title: str,
        root_cause: str,
        paged_person: str,
        reason: str,
        pr_url: str,
        proof_status: str,
        extra_fields: dict[str, str] | None = None,
    ) -> PageResult:
        embed = build_embed(
            incident_id, title, root_cause, paged_person, reason, pr_url, proof_status, extra_fields
        )
        payload = {
            "username": "Incident Commander",
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }
        if self.mock or (self.budget is not None and not self.budget.allow("page")):
            self.calls.append({"at": time.time(), "payload": payload})
            return PageResult(simulated=True)

        assert self.webhook_url
        return await retry_action(lambda: self._post_once(payload))

    async def _post_once(self, payload: dict[str, Any]) -> PageResult:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=15.0) as c:
                r = await c.post(self.webhook_url, params={"wait": "true"}, json=payload)
        except httpx.HTTPError as e:
            raise ActionError(f"Discord: {type(e).__name__}", retryable=True) from e
        if 200 <= r.status_code < 300:
            try:
                mid = str(r.json().get("id")) if r.content else None
            except ValueError:
                mid = None
            return PageResult(simulated=False, message_id=mid)
        raise ActionError(
            f"Discord webhook -> {r.status_code}",
            retryable=r.status_code == 429 or r.status_code >= 500,
            status=r.status_code,
            retry_after=_retry_after(r) if r.status_code == 429 else None,
        )


def _retry_after(r: httpx.Response) -> float:
    try:
        return max(0.0, float(r.json()["retry_after"]))
    except Exception:
        try:
            return max(0.0, float(r.headers.get("retry-after", 1)))
        except ValueError:
            return 1.0
