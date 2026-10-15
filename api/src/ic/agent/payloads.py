"""Event payloads for the console, shaped like ``web/lib/payloads.ts`` (the UI's contract).

Pure functions over tool results; the loop and the triage agent call them and hand the
dicts to the ``emit`` callback. Extra keys are allowed (the reducer ignores them); every
key the TypeScript types require is always present.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ic.proof.canonical import content_hash
from ic.tools.result import ToolResult
from ic.world.causality import CLAUSES

CLAUSE_LABELS = {
    "after_onset": "Precedes earliest onset",
    "origin": "Origin service fails first",
    "coverage": "Reach covers every symptomatic service",
    "order": "Onset order matches hop order",
    "signature": "Change type fits the symptom signatures",
}
TOP_SUSPECTS = 5


def iso(t: int | float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def tool_call(r: ToolResult) -> dict:
    return r.model_dump(mode="json")


def _expected_text(expected: dict) -> str:
    return ", ".join(f"{k} = {json.dumps(v)}" for k, v in sorted(expected.items()))


def hypothesis(p: dict) -> dict:
    """``HypothesisData`` from a ``test_hypothesis`` result (the registered hypothesis)."""
    return {
        "id": p["hypothesis_id"],
        "suspect": p["entity_id"],
        "statement": p["claim"],
        "falsifier_tool": p["falsifier_tool"],
        "expected": _expected_text(p["expected"]),
        "state": "REGISTERED",
        "kind": p["kind"],
        "registered_at": p.get("registered_at"),
    }


def proof_summary(kind: str, entity: str, holds: bool, fr: dict) -> str:
    """One sentence a human can check against the stored query."""
    if kind in ("suspect_qualifies", "entity_refuted"):
        v = fr.get("verdict") or {}
        qualifies = bool(fr.get("qualifies"))
        if qualifies:
            return f"{entity} qualifies: every KTD23 clause holds against every symptom."
        return f"{entity} is refuted at clause {v.get('clause')}: {v.get('detail')}."
    if kind == "suspect_necessary":
        n = len(fr.get("checked") or [])
        if fr.get("necessary"):
            return f"Necessary: with {entity} removed, none of {n} remaining candidates qualifies."
        rivals = ", ".join(r["id"] for r in fr.get("rivals") or [])
        return f"Not necessary: with {entity} removed, {rivals} still explain every symptom."
    if kind == "no_other_qualifying":
        n_c, n_s = len(fr.get("checked") or []), len(fr.get("checked_services") or [])
        q = fr.get("qualifying") or []
        if not q:
            return (
                f"Clean certificate: {n_c} candidates on {n_s} upstream services checked; "
                f"none other than {entity} qualifies."
            )
        return f"Certificate not clean: {', '.join(q)} also qualify."
    return "HOLDS" if holds else "REFUTED"


def _main_query(fr: dict) -> int | None:
    for key in ("verdict", "suspect"):
        q = (fr.get(key) or {}).get("query")
        if isinstance(q, int) and q >= 0:
            return q
    return None


def proof_query_index(p: dict) -> int:
    """Index into a ``test_hypothesis`` result's ``cypher`` of the query a proof shows: the
    one that decided the verdict, else the last (-1 when there are none)."""
    cypher = p.get("cypher") or []
    q = _main_query(p.get("falsifier_result") or {})
    if q is None or q >= len(cypher):
        q = len(cypher) - 1
    return q


def proof(p: dict, position: int) -> dict:
    """``ProofData`` from a ``test_hypothesis`` result. ``position`` is 1-based."""
    fr = p.get("falsifier_result") or {}
    cypher, params = p.get("cypher") or [], p.get("params") or []
    q = proof_query_index(p)
    return {
        "hypothesis_id": p["hypothesis_id"],
        "proof_id": p["id"],
        "position": position,
        "hash": p["hash"],
        "prev_hash": p.get("prev_hash"),
        "state": p["state"],
        "summary": proof_summary(p["kind"], p["entity_id"], p["holds"], fr),
        "query": cypher[q] if cypher else "",
        "params": params[q] if params and q < len(params) else {},
        "kind": p["kind"],
        "suspect": p["entity_id"],
    }


def funnel_query_index(res: dict, clause: str) -> int | None:
    """Index into a ``screen_candidates`` result's ``cypher`` of the query a funnel cut
    shows: the first query that eliminated something at ``clause``, else the first verdict
    query (a cut that removed nothing still shows how clauses are checked)."""
    s = next((s for s in res["steps"] if s["step"] == clause), None)
    if s is None:
        return None
    if s["queries"]:
        return s["queries"][0]
    return next((v["query"] for v in res["verdicts"] if v.get("query", -1) >= 0), None)


def funnel(r: ToolResult) -> list[dict]:
    """One ``FunnelData`` per KTD23 clause from a ``screen_candidates`` result. The window
    pre-filter (``outside_window``) is not a clause in the UI's type: its count rides along
    on step 1 as ``outside_window``."""
    res = r.result
    verdicts = {v["id"]: v for v in res["verdicts"]}
    steps = {s["step"]: s for s in res["steps"]}
    rank = {c: i for i, c in enumerate(CLAUSES)}
    before = steps["outside_window"]["remaining"]
    out = []
    for i, clause in enumerate(CLAUSES, 1):
        s = steps[clause]
        q = funnel_query_index(res, clause)
        still = [
            v
            for v in res["verdicts"]
            if v["ok"] or (v["clause"] in rank and rank[v["clause"]] > rank[clause])
        ]
        still.sort(key=lambda v: (not v["ok"], -rank.get(v["clause"], 99), v["id"]))
        data = {
            "step": i,
            "clause": clause,
            "label": CLAUSE_LABELS[clause],
            "before": before,
            "after": s["remaining"],
            "cypher": r.cypher[q] if q is not None else "",
            "params": r.params[q] if q is not None and s["queries"] else {},
            "eliminated": [
                {"change": e, "service": verdicts[e]["service_id"], "clause": clause}
                for e in s["eliminated"]
            ],
            "top_suspects": [v["id"] for v in still[:TOP_SUSPECTS]],
        }
        if i == 1:
            ow = steps["outside_window"]
            data["outside_window"] = ow["eliminated_count"]
            data["window"] = {
                "from": iso(res["window"]["from"]),
                "to": iso(res["window"]["to"]),
            }
        out.append(data)
        before = s["remaining"]
    return out


def certificate(cert: dict, *, hash: str | None = None) -> dict:
    """``CertificateData`` from an ``absence_certificate`` result. ``hash`` is the proof hash
    when the certificate came from a chained proof, else the result's content hash."""
    suspect = cert["suspect_id"]
    win = cert["window"]
    out = {
        "services_checked": len(cert["checked_services"]),
        "window": {"from": iso(win["from"]), "to": iso(win["to"])},
        "qualifying": len(cert["qualifying"]),
        "qualifying_ids": list(cert["qualifying"]),
        "suspect": suspect,
        "checked": [
            {"change": v["id"], "service": v["service_id"], "clause": v["clause"]}
            | ({"event_time": iso(v["event_time"])} if v.get("event_time") is not None else {})
            for v in cert["checked"]
            if v["id"] != suspect and not v["ok"]
        ],
        "clean": bool(cert.get("clean", not cert["qualifying"])),
        "hash": hash or content_hash(cert),
    }
    # UI-only extras for the time strip, derived from the result and never fed to the hash:
    # the window opens `minutes` before the earliest symptom onset.
    if win.get("minutes") is not None:
        out["onset"] = iso(win["from"] + win["minutes"] * 60)
    suspect_v = cert.get("suspect") or {}
    st = suspect_v.get("event_time")
    if st is not None:
        out["suspect_event_time"] = iso(st)
    # The suspect's own first failing clause (None when it passes every clause or was not
    # screened): the certificate may be issued for a suspect that does not qualify.
    out["suspect_clause"] = suspect_v.get("clause")
    return out


def path_ref(label: str, change: str, service: str, nodes: list[str]) -> dict:
    return {"label": label, "change": change, "service": service, "nodes": nodes}


def compact(obj: Any, *, max_list: int = 15, max_str: int = 400) -> Any:
    """Bound the size of anything sent back to the model."""
    if isinstance(obj, dict):
        return {k: compact(v, max_list=max_list, max_str=max_str) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [compact(v, max_list=max_list, max_str=max_str) for v in obj[:max_list]]
        if len(obj) > max_list:
            items.append(f"... {len(obj) - max_list} more")
        return items
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + "..."
    return obj
