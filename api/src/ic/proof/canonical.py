"""Canonical form of a tool result for hashing (KTD4).

Domain ids only, sorted sets, no timings, and paths kept in the order the tool returned
them (tools already pick the lexicographically smallest equal-cost path). The display path
from ``algo.SPpaths`` is a UI artifact that may differ between equal-cost ties, so it is
dropped along with every timing key.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Timing and presentation keys never enter a hash.
DROP_KEYS = frozenset({"ms", "build_ms", "elapsed", "display_path"})
# String lists under these keys are sequences (order is meaning), not sets.
ORDERED_KEYS = frozenset({"path", "ids", "steps", "symptoms"})


def _canon(obj: Any, key: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {str(k): _canon(v, str(k)) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, (list, tuple)):
        items = [_canon(v) for v in obj]
        if key not in ORDERED_KEYS and items and all(isinstance(v, str) for v in items):
            return sorted(items)
        return items
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, timings dropped, id sets sorted."""
    return json.dumps(_canon(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def content_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))
