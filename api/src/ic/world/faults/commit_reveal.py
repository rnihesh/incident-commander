"""Judge-mode commit-reveal (U14, KTD11): the public commitment hash and its reveal."""

from __future__ import annotations

import hashlib
import json

from ic.store import Store

# ================================================================== judge mode (U14, KTD11)
#
# Commit-reveal: before the run the judge sees only ``commitment``, a sha256 over the
# canonical fault JSON followed by the nonce. The nonce is 128 random bits, so the hash
# reveals nothing; at reveal the API hands back the preimage and the nonce and anyone can
# recompute the hash. The answer itself stays in the SQLite store (R26).


def commitment_preimage(fault_id: str, incident_id: str, fault_class: str, root_id: str) -> str:
    """Canonical JSON (sorted keys, no whitespace, UTF-8) of the committed fault."""
    obj = {
        "fault_id": fault_id,
        "incident_id": incident_id,
        "fault_class": fault_class,
        "root_id": root_id,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def commitment(fault_id: str, incident_id: str, fault_class: str, root_id: str, nonce: str) -> str:
    """``sha256(canonical JSON {fault_id, incident_id, fault_class, root_id} + nonce)``."""
    pre = commitment_preimage(fault_id, incident_id, fault_class, root_id) + nonce
    return hashlib.sha256(pre.encode("utf-8")).hexdigest()


def reveal(store: Store, session_id: str, incident_id: str) -> dict | None:
    """The opened commitment for a session's incident: the answer, the nonce, the
    commitment stored at injection and the hash recomputed from the revealed values.
    None when the session has no answer for that incident. API-only: never call this from
    a tool, an MCP handler or anything that feeds agent context."""
    a = store.get_answer(incident_id, session_id)
    if a is None:
        return None
    fields = (a["fault_id"], a["incident_id"], a["fault_class"], a["root_id"])
    recomputed = commitment(*fields, a["nonce"])
    stored = a.get("commitment")
    return {
        "fault_id": a["fault_id"],
        "incident_id": a["incident_id"],
        "fault_class": a["fault_class"],
        "root_id": a["root_id"],
        "nonce": a["nonce"],
        "preimage": commitment_preimage(*fields),
        "commitment": stored,
        "recomputed": recomputed,
        "commitment_ok": stored is not None and stored == recomputed,
    }
