"""Sliding-window rate limit per Telegram user (in memory: one bot process)."""

import time
from collections import defaultdict, deque
from typing import Callable


class RateLimiter:
    def __init__(self, max_events: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_events, self.window_s, self._clock = max_events, window_s, clock
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = self._clock()
        q = self._events[user_id]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True
