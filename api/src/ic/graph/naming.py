"""The only place graph names are formatted (KTD1). Ids are validated so a caller-supplied
value can never smuggle ':' or '*' into a name and cross a scope boundary."""

from __future__ import annotations

import re
import secrets

_SID = re.compile(r"[a-f0-9]{32}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
VIEWS = ("true", "known")


def _check(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def _sid(sid: str) -> str:
    return _check(sid, _SID, "session id")


def _id(value: str, what: str) -> str:
    return _check(value, _ID, what)


def _view(view: str) -> str:
    if view not in VIEWS:
        raise ValueError(f"invalid view: {view!r}")
    return view


def new_session_id() -> str:
    return secrets.token_hex(16)


def seed_world() -> str:
    return "seed:world"


def session_world(sid: str) -> str:
    return f"s:{_sid(sid)}:world"


def ledger(sid: str) -> str:
    return f"s:{_sid(sid)}:ledger"


def asof(sid: str, iid: str, view: str) -> str:
    return f"s:{_sid(sid)}:asof:{_id(iid, 'incident id')}:{_view(view)}"


def verify(sid: str, iid: str, view: str) -> str:
    return f"s:{_sid(sid)}:verify:{_id(iid, 'incident id')}:{_view(view)}"


def rehearse(sid: str, iid: str) -> str:
    return f"s:{_sid(sid)}:rehearse:{_id(iid, 'incident id')}"


def storm(sid: str, storm_id: str) -> str:
    return f"s:{_sid(sid)}:storm:{_id(storm_id, 'storm id')}"


def pr_prefix(sid: str, iid: str) -> str:
    """PR copies are scoped by session *and* incident: incident ids repeat across sessions
    (every session world numbers its incidents from the same seed), so ``pr:{iid}`` alone
    would let one session's PR copy overwrite another's."""
    return f"pr:{_sid(sid)}:{_id(iid, 'incident id')}:"


def pr_world(sid: str, iid: str) -> str:
    return pr_prefix(sid, iid) + "world"


def pr_ledger(sid: str, iid: str) -> str:
    return pr_prefix(sid, iid) + "ledger"


def pr_verify(sid: str, iid: str, view: str) -> str:
    """Verify snapshots of a PR copy (the public proof page's re-run): built from
    ``pr_world`` and kept under the PR scope, never under the live session's ``s:`` names."""
    return pr_prefix(sid, iid) + f"verify:{_view(view)}"


def bench(run: str, case: str, kind: str) -> str:
    return f"b:{_id(run, 'run id')}:{_id(case, 'case id')}:{_id(kind, 'kind')}"


def scope_allows(
    graph_name: str,
    *,
    sid: str | None = None,
    iid: str | None = None,
    run: str | None = None,
    allow_seed_ro: bool = False,
) -> bool:
    """Whether a tool scoped to these ids may touch ``graph_name`` (KTD3). PR copies need
    both the session and the incident: ``pr:{sid}:{iid}:*``."""
    if allow_seed_ro and graph_name == seed_world():
        return True
    if sid is not None and graph_name.startswith(f"s:{_sid(sid)}:"):
        return True
    if sid is not None and iid is not None and graph_name.startswith(pr_prefix(sid, iid)):
        return True
    return run is not None and graph_name.startswith(f"b:{_id(run, 'run id')}:")
