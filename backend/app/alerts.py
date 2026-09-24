"""In-memory alert store: ring buffer + de-duplication cooldowns.

Timing contract: a geofence breach is detected inside the request/tick that
causes it and pushed to the WebSocket fan-out immediately, so end-to-end
detection -> client delivery stays well under the 1 s budget.
"""

from __future__ import annotations

import itertools
import time
from collections import OrderedDict
from typing import Dict, Iterable, List, Optional

from . import config
from .models import Alert, Severity

_SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


class AlertStore:
    """Bounded alert history with per-key cooldowns to keep the feed readable."""

    def __init__(self, capacity: int = config.ALERT_BUFFER_SIZE) -> None:
        self.capacity = capacity
        self._alerts: "OrderedDict[str, Alert]" = OrderedDict()
        self._counter = itertools.count(1)
        # key -> monotonic timestamp of the last alert with that key
        self._last_at: Dict[str, float] = {}
        # key -> alert id currently open (used to resolve alerts later)
        self._open: Dict[str, str] = {}
        self.total = 0
        self.by_severity: Dict[str, int] = {k: 0 for k in _SEVERITY_ORDER}

    # -- creation ----------------------------------------------------------
    def fire(
        self,
        type: str,
        severity: Severity,
        message: str,
        *,
        dedupe_key: Optional[str] = None,
        cooldown_s: float = 0.0,
        shipIds: Iterable[str] = (),
        zoneId: Optional[str] = None,
        sim_time: str = "",
        now: Optional[float] = None,
        **data: object,
    ) -> Optional[Alert]:
        """Create an alert, or return None when suppressed by its cooldown."""
        import time

        now = time.time() if now is None else now
        key = dedupe_key or f"{type}:{','.join(sorted(shipIds))}:{zoneId or '-'}"
        last = self._last_at.get(key)
        if cooldown_s > 0 and last is not None and (now - last) < cooldown_s:
            return None
        self._last_at[key] = now

        alert_id = f"AL-{next(self._counter)}"
        alert = Alert(
            id=alert_id,
            type=type,
            severity=severity,
            message=message,
            shipIds=list(shipIds),
            zoneId=zoneId,
            data=dict(data),
            created_at=now,
            sim_time=sim_time,
        )
        self._alerts[alert_id] = alert
        self._open[key] = alert_id
        self.total += 1
        self.by_severity[severity] = self.by_severity.get(severity, 0) + 1
        while len(self._alerts) > self.capacity:
            self._alerts.popitem(last=False)
        return alert

    # -- queries -----------------------------------------------------------
    def recent(self, limit: int = 100, min_severity: Optional[Severity] = None) -> List[Alert]:
        items = list(self._alerts.values())[::-1]
        if min_severity:
            floor = _SEVERITY_ORDER[min_severity]
            items = [a for a in items if _SEVERITY_ORDER[a.severity] >= floor]
        return items[:limit]

    def since_id(self, alert_id: Optional[str]) -> List[Alert]:
        if not alert_id:
            return []
        ids = list(self._alerts.keys())
        if alert_id not in ids:
            return self.recent(20)
        idx = ids.index(alert_id)
        return list(self._alerts.values())[idx + 1 :]

    def clear(self) -> int:
        n = len(self._alerts)
        self._alerts.clear()
        self._last_at.clear()
        self._open.clear()
        return n

    def counts(self) -> Dict[str, int]:
        return {
            "total": self.total,
            "buffered": len(self._alerts),
            "by_severity": dict(self.by_severity),
        }
