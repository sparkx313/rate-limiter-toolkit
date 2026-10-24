"""滑动窗口计数器：用「上一窗口的计数 × 重叠比例 + 本窗口计数」插值近似精确窗口。

这是 Cloudflare 那类近似：内存恒定（每 key 只存两个计数），却把固定窗口的越界问题压回去
大半。它是**近似**不是精确，所以 `sliding_counter` 一节基准量的就是它相对精确日志的最大
计数误差。误差只可能为正（近似值 <= 精确值的情况才会多放行），下面逐项说明。
"""

import math

from .common import (resolve_now, validate_capacity, validate_cost,
                     validate_positive_number)

_INF = math.inf


class SlidingWindowCounterLimiter:
    """对精确滑动窗口的线性插值近似，`limit`/`window` 语义与日志版一致。"""

    def __init__(self, limit, window):
        validate_capacity("limit", limit)
        validate_positive_number("window", window)
        self.limit = limit
        self.window = float(window)
        # key -> (window_index, curr, prev)：本窗口用量与上一窗口用量，两个整数。
        self._state = {}

    def _view(self, key, now):
        """返回 (index, offset, curr, prev)，已完成窗口滚动与回拨保护。"""
        index = math.floor(now / self.window)
        stored = self._state.get(key)
        if stored is None:
            curr = prev = 0
        else:
            s_index, s_curr, s_prev = stored
            if index == s_index:
                curr, prev = s_curr, s_prev
            elif index == s_index + 1:
                # 恰好滚过一格：本窗口清零，上一窗口的「本窗口计数」降为 prev。
                curr, prev = 0, s_curr
            elif index > s_index + 1:
                # 跨了多格，s_curr 所在窗口已完全滑出视野，两个计数都归零。
                curr = prev = 0
            else:
                # 回拨：冻结在已记录的那一格，offset 取 0 让 prev 权重最大、最保守。
                index, curr, prev = s_index, s_curr, s_prev
                return index, 0.0, curr, prev
        offset = now - index * self.window
        return index, offset, curr, prev

    def approximate(self, key, now=None):
        """当前近似的窗口内用量（浮点），供基准与精确日志对比误差。"""
        now = resolve_now(now)
        _, offset, curr, prev = self._view(key, now)
        weight = (self.window - offset) / self.window
        return prev * weight + curr

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return True
        if cost > self.limit:
            return False
        now = resolve_now(now)
        index, offset, curr, prev = self._view(key, now)
        weight = (self.window - offset) / self.window
        if prev * weight + curr + cost <= self.limit:
            self._state[key] = (index, curr + cost, prev)
            return True
        self._state[key] = (index, curr, prev)
        return False

    def wait_time(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return 0.0
        if cost > self.limit:
            return _INF
        now = resolve_now(now)
        index, offset, curr, prev = self._view(key, now)
        weight = (self.window - offset) / self.window
        approx = prev * weight + curr
        if approx + cost <= self.limit:
            return 0.0
        headroom = self.limit - cost - curr
        if headroom >= 0 and prev > 0:
            # 只需 prev 的权重再降一点就能放行：解出需要的 offset，差值即等待秒数。
            needed_weight = headroom / prev
            needed_offset = self.window * (1.0 - needed_weight)
            if needed_offset <= self.window:
                return max(0.0, needed_offset - offset)
        # curr 本身已经装满：只能等到下一格边界让 curr 降级为 prev。
        return self.window - offset

    def used(self, key, now=None):
        now = resolve_now(now)
        _, _, curr, prev = self._view(key, now)
        return curr

    def memory_entries(self):
        return len(self._state)

    def snapshot(self):
        return {"limit": self.limit, "window": self.window,
                "state": {k: list(v) for k, v in self._state.items()}}

    def restore(self, data):
        if data["limit"] != self.limit or data["window"] != self.window:
            raise ValueError("snapshot 与本限流器的 limit/window 不一致")
        self._state = {k: (v[0], v[1], v[2]) for k, v in data["state"].items()}
        return self
