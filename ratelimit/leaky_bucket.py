"""漏桶：水位随到达累加，按 `rate` 个/秒恒定漏出，桶满即拒。

与令牌桶方向相反——它衡量的是「占用」而不是「余额」，因此输出被强制平滑：长期漏出速率
等于 rate，且**空闲期攒不出突发额度**。配置 `capacity` 与 `rate` 的漏桶，其稳态吞吐与
`capacity`、`rate` 相同的令牌桶一致，但突发上界更小。`leaky_vs_bucket` 会量出这个差别。
"""

import math

from .common import (clamp_delta, resolve_now, validate_cost,
                     validate_positive_number)

_INF = math.inf


class LeakyBucketLimiter:
    def __init__(self, capacity, rate):
        validate_positive_number("capacity", capacity)
        validate_positive_number("rate", rate)
        self.capacity = float(capacity)
        self.rate = float(rate)
        # key -> (水位, 上次放水时刻)。水位是浮点，允许非整数成本。
        self._levels = {}

    def _drain(self, key, now):
        level, last = self._levels.get(key, (0.0, now))
        elapsed, now = clamp_delta(now, last)
        # 只放不超时的水：max(0.0, ...) 防止把水位漏成负的，负水位等于欠了额度。
        level = max(0.0, level - elapsed * self.rate)
        self._levels[key] = (level, now)
        return level

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return True
        now = resolve_now(now)
        level = self._drain(key, now)
        if level + cost <= self.capacity:
            self._levels[key] = (level + cost, now)
            return True
        return False

    def wait_time(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return 0.0
        now = resolve_now(now)
        level = self._drain(key, now)
        if level + cost <= self.capacity:
            return 0.0
        if cost > self.capacity:
            return _INF
        return (level + cost - self.capacity) / self.rate

    def water_level(self, key, now=None):
        now = resolve_now(now)
        return self._drain(key, now)

    def memory_entries(self):
        return len(self._levels)

    def snapshot(self):
        return {"capacity": self.capacity, "rate": self.rate,
                "levels": {k: list(v) for k, v in self._levels.items()}}

    def restore(self, data):
        if data["capacity"] != self.capacity or data["rate"] != self.rate:
            raise ValueError("snapshot 与本限流器的 capacity/rate 不一致")
        self._levels = {k: (v[0], v[1]) for k, v in data["levels"].items()}
        return self
