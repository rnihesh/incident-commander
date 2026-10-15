"""Global PR/page budget per hour and per-incident status debounce (KTD10).

Over budget the caller runs the action in mock mode labelled "simulated".
"""

from __future__ import annotations

import time

from ic.config import get_settings
from ic.store import Store

HOUR = 3600.0


class ActionBudget:
    def __init__(self, store: Store, *, per_hour: int | None = None):
        self.store = store
        self.per_hour = get_settings().actions_per_hour if per_hour is None else per_hour

    def allow(self, kind: str, *, now: float | None = None) -> bool:
        """Consume one unit of ``kind`` (e.g. "pr", "page") if the hourly budget allows."""
        now = time.time() if now is None else now
        with self.store.conn() as c:
            used = c.execute(
                "SELECT count(*) FROM limit_events WHERE kind = ? AND key = '*' AND ts > ?",
                (f"action:{kind}", now - HOUR),
            ).fetchone()[0]
            if used >= self.per_hour:
                return False
            c.execute("INSERT INTO limit_events VALUES (?, '*', ?)", (f"action:{kind}", now))
        return True

    def allow_status_rerun(
        self, incident_id: str, min_interval_s: float = 60, *, now: float | None = None
    ) -> bool:
        now = time.time() if now is None else now
        with self.store.conn() as c:
            last = c.execute(
                "SELECT max(ts) FROM limit_events WHERE kind = 'status_rerun' AND key = ?",
                (incident_id,),
            ).fetchone()[0]
            if last is not None and now - last < min_interval_s:
                return False
            c.execute("INSERT INTO limit_events VALUES ('status_rerun', ?, ?)", (incident_id, now))
        return True
