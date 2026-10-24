"""令牌桶：桶里最多攒 `capacity` 个令牌，按 `rate` 个/秒连续补充。

放行规则是「取走令牌」而不是「占用槽位」，因此空闲期能攒出一整桶用于突发，长期平均速率
等于 rate。和 GCRA 相比它的放行**数量**一致，但突发形状与承诺的等待时间不同，这正是
`gcra_vs_bucket` 一节要量出来的差别。
"""

import math

from .common import (clamp_delta, resolve_now, validate_cost,
                     validate_positive_number)

_INF = math.inf


class TokenBucketLimiter:
    def __init__(self, capacity, rate):
        validate_positive_number("capacity", capacity)
        validate_positive_number("rate", rate)
        self.capacity = float(capacity)
        self.rate = float(rate)
        # key -> (令牌数, 上次补充时刻)。令牌是浮点，所以支持非整数成本。
        self._buckets = {}

    def _refill(self, key, now):
        tokens, last = self._buckets.get(key, (self.capacity, now))
        elapsed, now = clamp_delta(now, last)
        # 补到 min(capacity, ...)：空闲太久也不许攒超过一整桶，否则突发上界失控。
        tokens = min(self.capacity, tokens + elapsed * self.rate)
        self._buckets[key] = (tokens, now)
        return tokens

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return True
        now = resolve_now(now)
        tokens = self._refill(key, now)
        if tokens >= cost:
            self._buckets[key] = (tokens - cost, now)
            return True
        return False

    def wait_time(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return 0.0
        now = resolve_now(now)
        tokens = self._refill(key, now)
        if tokens >= cost:
            return 0.0
        if cost > self.capacity:
            # 一个请求比整桶还大，永远补不满——承诺 inf 而不是某个假数字。
            return _INF
        return (cost - tokens) / self.rate

    def tokens(self, key, now=None):
        """补充后的当前令牌数（已把空闲攒下的算进来）。"""
        now = resolve_now(now)
        return self._refill(key, now)

    def memory_entries(self):
        return len(self._buckets)

    def snapshot(self):
        return {"capacity": self.capacity, "rate": self.rate,
                "buckets": {k: list(v) for k, v in self._buckets.items()}}

    def restore(self, data):
        if data["capacity"] != self.capacity or data["rate"] != self.rate:
            raise ValueError("snapshot 与本限流器的 capacity/rate 不一致")
        self._buckets = {k: (v[0], v[1]) for k, v in data["buckets"].items()}
        return self
