"""滑动窗口日志：每个 key 存一条放行时刻的 deque，判定精确，内存随流量线性增长。

它是「精确」的参照系——固定窗口和插值计数器都是在近似它。代价是每条放行都要占一个数组
槽位，1e6 请求就是 1e6 个浮点数。`sliding_log` 一节基准量的正是每 key 每请求的字节开销。
"""

import math
from collections import deque

from .common import (resolve_now, validate_capacity, validate_cost,
                     validate_positive_number)

_INF = math.inf


class SlidingWindowLogLimiter:
    """过去 `window` 秒内累计放行不超过 `limit` 个成本单位。"""

    def __init__(self, limit, window):
        validate_capacity("limit", limit)
        validate_positive_number("window", window)
        self.limit = limit
        self.window = float(window)
        # 每个 key 一条按到达时间升序的放行时刻队列。队列长度就是当前窗口内的用量，
        # 所以内存随「放行了多少」增长，而不是随「有多少 key」增长。
        self._events = {}

    def _expire(self, key, cutoff):
        events = self._events.get(key)
        if events is None:
            return None
        # cutoff = now - window：严格小于等于它的都已经滑出窗口。用 <= 而不是 <，
        # 让「恰好 window 秒前」的那条按惯例视为过期。
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return True
        if cost > self.limit:
            return False
        now = resolve_now(now)
        events = self._events.setdefault(key, deque())
        self._expire(key, now - self.window)
        if len(events) + cost <= self.limit:
            # cost 个单位记成 cost 条同刻事件，保持与「精确计数」一致。
            for _ in range(cost):
                events.append(now)
            return True
        return False

    def wait_time(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return 0.0
        if cost > self.limit:
            return _INF
        now = resolve_now(now)
        events = self._events.setdefault(key, deque())
        self._expire(key, now - self.window)
        need = cost - (self.limit - len(events))
        if need <= 0:
            return 0.0
        # 要腾出 need 个槽位，就得等最靠前的 need 条事件过期；第 need 条的到期时刻
        # 减掉 now 就是最早可放行的等待时间。
        target = events[need - 1] if need <= len(events) else None
        if target is None:
            return _INF
        return target + self.window - now

    def used(self, key, now=None):
        now = resolve_now(now)
        events = self._events.setdefault(key, deque())
        self._expire(key, now - self.window)
        return len(events)

    def memory_entries(self, key=None):
        """队列里事件总数：给定 key 就只数它，否则数全部。"""
        if key is not None:
            return len(self._events.get(key, ()))
        return sum(len(v) for v in self._events.values())

    def snapshot(self):
        return {"limit": self.limit, "window": self.window,
                "events": {k: list(v) for k, v in self._events.items()}}

    def restore(self, data):
        if data["limit"] != self.limit or data["window"] != self.window:
            raise ValueError("snapshot 与本限流器的 limit/window 不一致")
        self._events = {k: deque(v) for k, v in data["events"].items()}
        return self
