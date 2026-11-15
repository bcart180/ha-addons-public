"""In-memory limits for CallCatch Line.  Simple, bounded, no I/O."""
from __future__ import annotations

import time
from collections import OrderedDict


class Window:
    """At most ``limit`` events per ``period`` seconds, per key (sliding log)."""

    def __init__(self, limit: int, period: float, max_keys: int = 2000):
        self.limit = max(1, int(limit))
        self.period = float(period)
        self.max_keys = max_keys
        self._hits: OrderedDict[str, list[float]] = OrderedDict()

    def allow(self, key: str = "_", now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        hits = [h for h in self._hits.get(key, []) if t - h < self.period]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            self._hits.move_to_end(key)
            return False
        hits.append(t)
        self._hits[key] = hits
        self._hits.move_to_end(key)
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        return True


class Lockout:
    """Per client: after ``limit`` failures inside ``period``, refuse that client for ``hold`` seconds.

    Keyed per client.  Callers check the token first and consult this only
    on a failure, so a valid token is never refused by a lockout.
    """

    def __init__(self, limit: int = 10, period: float = 60, hold: float = 300, max_keys: int = 5000):
        self.window = Window(limit, period, max_keys=max_keys)
        self.hold = hold
        self.max_keys = max_keys
        self.until: OrderedDict[str, float] = OrderedDict()

    def locked(self, key: str = "_", now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        return t < self.until.get(key, 0.0)

    def fail(self, key: str = "_", now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        if not self.window.allow(key, t):
            self.until[key] = t + self.hold
            self.until.move_to_end(key)
            while len(self.until) > self.max_keys:
                self.until.popitem(last=False)
