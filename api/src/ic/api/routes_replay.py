"""Replay scrubber data (U18, R33): the outage spreading through the graph over time.

One response carries the static subgraph (identical to the graph route's) and a list of
frames; each frame is only a set of highlights, so the UI never re-lays-out. ``view=true``
is the world as it happened (late-arriving records included and listed in ``late_hidden``);
``view=known`` is the world as the agent knew it at alert time (late records absent).
The root event is deliberately not revealed: the only answer-shaped marker is the time of
the suspect in the agent's own ledger verdict, when one exists.
"""

from __future__ import annotations

import math
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from ic.api.common import _check_iid, _not_ready, _require_session
from ic.api.graph_view import (
    GRAPH_CHANGE_WINDOW_S,
    SnapshotsNotReady,
    _ensure_snapshots,
    _graph_payload,
    _ro,
)
from ic.api.state import get_state
from ic.graph import naming
from ic.graph.client import graph_exists
from ic.proof import ledger
from ic.tools.result import ToolError

router = APIRouter(prefix="/api")

MAX_FRAMES = 120
LEAD_BEFORE_ONSET_S = 30 * 60
LEAD_BEFORE_CHANGE_S = 30
TAIL_AFTER_ALERT_S = 30


def frame_times(start: int, end: int, max_frames: int = MAX_FRAMES) -> list[int]:
    """Evenly spaced times from ``start`` to ``end`` inclusive, at most ``max_frames``,
    with the smallest whole-second step that fits."""
    span = max(end - start, 0)
    step = max(1, math.ceil(span / (max_frames - 1)))
    ts = list(range(start, end + 1, step))
    if ts[-1] != end:
        ts.append(end)
    return ts


async def _verdict_suspect_time(sid: str, iid: str, graph: str) -> int | None:
    """valid_from of the change the agent's verdict names (latest held suspect_qualifies)."""
    lg = naming.ledger(sid)
    if not await graph_exists(lg):
        return None
    try:
        hyps = await ledger.hypotheses(sid, iid)
    except ToolError:
        return None
    held = [h for h in hyps if h.kind == "suspect_qualifies" and h.state == ledger.HOLDS]
    if not held:
        return None
    rows = await _ro(
        graph, "MATCH (c:Change {id: $id}) RETURN c.valid_from", {"id": held[-1].entity_id}
    )
    return int(rows[0][0]) if rows and rows[0][0] is not None else None


@router.get("/incidents/{incident_id}/replay")
async def incident_replay(
    incident_id: str, request: Request, view: Literal["known", "true"] = "known"
):
    s = get_state(request)
    sess = _require_session(request, s)
    sid = sess.sid
    _check_iid(sid, incident_id)
    if s.runs.active_for(sid, incident_id):
        return _not_ready()
    try:
        true_g, known_g = await _ensure_snapshots(s, sid, incident_id)
    except SnapshotsNotReady:
        return _not_ready()
    view_g = true_g if view == "true" else known_g
    payload = await _graph_payload(known_g, incident_id)
    t_alert: int = payload["t"]
    node_ids = [n["id"] for n in payload["nodes"]]
    if not node_ids:
        raise HTTPException(404, "no replayable graph for this incident")
    onset_of: dict[str, int] = {}
    for n in payload["nodes"]:
        if n["onset"] is not None:
            onset_of[n["id"]] = int(n["onset"])
    earliest_onset = min(onset_of.values()) if onset_of else None

    change_rows = await _ro(
        view_g,
        "MATCH (c:Change)-[:CHANGES]->(s:Service) "
        "WHERE s.id IN $ids AND $lo <= c.valid_from AND c.valid_from <= $t "
        "RETURN c.id, s.id, c.kind, c.valid_from, c.recorded_at "
        "ORDER BY c.valid_from, c.id",
        {"ids": node_ids, "lo": t_alert - GRAPH_CHANGE_WINDOW_S, "t": t_alert},
    )
    changes = [
        {
            "id": cid,
            "service_id": svc,
            "kind": kind,
            "valid_from": vf,
            "recorded_at": rec,
            "late": rec is None or int(rec) > t_alert,
        }
        for cid, svc, kind, vf, rec in change_rows
    ]
    if view == "known":  # the known snapshot never contains late records
        changes = [c for c in changes if not c["late"]]

    starts = []
    if earliest_onset is not None:
        starts.append(earliest_onset - LEAD_BEFORE_ONSET_S)
    if changes:
        starts.append(min(int(c["valid_from"]) for c in changes) - LEAD_BEFORE_CHANGE_S)
    start = min(starts) if starts else t_alert - LEAD_BEFORE_ONSET_S
    end = t_alert + TAIL_AFTER_ALERT_S
    late_ids = sorted(c["id"] for c in changes if c["late"])

    onsets_sorted = sorted(onset_of.items(), key=lambda kv: (kv[1], kv[0]))
    frames = [
        {
            "t": t,
            "changes_active": [c["id"] for c in changes if int(c["valid_from"]) <= t],
            "symptomatic": [svc for svc, o in onsets_sorted if o <= t],
            "late_hidden": late_ids if view == "true" else [],
        }
        for t in frame_times(start, end)
    ]
    return {
        "incident_id": incident_id,
        "view": view,
        "alert_service_id": payload["alert_service_id"],
        "nodes": payload["nodes"],
        "edges": payload["edges"],
        "changes": changes,
        "frames": frames,
        "markers": {
            "root_event_t": None,
            "verdict_suspect_t": await _verdict_suspect_time(sid, incident_id, known_g),
            "alert_t": t_alert,
            "earliest_onset_t": earliest_onset,
        },
    }
