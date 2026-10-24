"""固定窗口计数器：把时间切成等长格子，每格一个计数器。

实现最简单、内存恒定（每 key 只存「当前格编号 + 计数」两项），但代价是跨格边界的突发
可以放行到配置值的两倍——上一格末尾和下一格开头各能塞满一整格。`fixed_window` 一节
基准就是在量这个越界放行到底有多大。
"""

import math

from .common import (resolve_now, validate_capacity, validate_cost,
                    validate_positive_number)

_INF = math.inf


class FixedWindowLimiter:
    """每个时间窗口最多放行 `limit` 个成本单位。"""

    def __init__(self, limit, window):
        validate_capacity("limit", limit)
        validate_positive_number("window", window)
        self.limit = limit
        self.window = float(window)
        # index -> 本窗口已用量；last_index -> 上一次写入时的窗口编号；用 dict 存
        # (index, count) 两个值即可，无需保存时间戳，因此内存与流量无关。
        self._state = {}

    def _index(self, now):
        return math.floor(now / self.window)

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return True
        if cost > self.limit:
            # 单个请求比整格还大：本算法永远放不下，直接拒绝而不是抛错，
            # 让调用方能按同一份配置安全降级。
            return False
        now = resolve_now(now)
        index = self._index(now)
        prev_index, count = self._state.get(key, (index, 0))
        # 回拨保护：请求时间落在比已记录窗口更早的格子里时，沿用来时的那一格，
        # 免得把计数清零后又放行一轮，凭空多出额度。
        if index < prev_index:
            index = prev_index
        elif index > prev_index:
            count = 0
        if count + cost <= self.limit:
            self._state[key] = (index, count + cost)
            return True
        self._state[key] = (index, count)
        return False

    def wait_time(self, key, now=None, cost=1):
        """被拒后还要等多久才可能放行：固定窗口只能等到下一格开始。"""
        validate_cost(cost)
        if cost == 0:
            return 0.0
        if cost > self.limit:
            return _INF
        now = resolve_now(now)
        index = self._index(now)
        prev_index, count = self._state.get(key, (index, 0))
        if index < prev_index:
            index = prev_index
        elif index > prev_index:
            count = 0
        if count + cost <= self.limit:
            return 0.0
        return (index + 1) * self.window - now

    def used(self, key, now=None):
        """当前窗口已消耗的成本单位，供基准核对越界放行。"""
        now = resolve_now(now)
        index = self._index(now)
        prev_index, count = self._state.get(key, (index, 0))
        if index != prev_index:
            return 0
        return count

    def memory_entries(self):
        return len(self._state)

    def snapshot(self):
        return {"limit": self.limit, "window": self.window,
                "state": {k: list(v) for k, v in self._state.items()}}

    def restore(self, data):
        if data["limit"] != self.limit or data["window"] != self.window:
            raise ValueError("snapshot 与本限流器的 limit/window 不一致")
        self._state = {k: (v[0], v[1]) for k, v in data["state"].items()}
        return self
