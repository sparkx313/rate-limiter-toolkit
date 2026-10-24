"""GCRA（Generic Cell Rate Algorithm）：只存一个「理论到达时间」TAT 的 O(1) 精确限流。

这是 Redis `rate-limit` 用的那套。它和令牌桶在放行数量上等价，但状态只有一个浮点数 TAT，
且对每个被拒请求给出的是「等到 TAT 前沿退到此刻」的精确时间，而不是令牌桶那种线性外推。
参数：`rate` 个/秒的稳态速率 + `capacity` 允许的突发上限。

内部常量由这两者推出：
    emission = 1 / rate                # 相邻两个单元必须隔开的最小时间
    tau      = capacity * emission     # 允许的迟到变化量，正好等于一整桶突发
放行判据 new_tat - tau <= now 在空闲时起算能连放 capacity 个单元，之后每 1/rate 秒放行一个，
与「容量 capacity、速率 rate」的令牌桶逐点吻合。
"""

import math

from .common import (resolve_now, validate_cost, validate_positive_number)

_INF = math.inf


class GCRALimiter:
    def __init__(self, capacity, rate):
        validate_positive_number("capacity", capacity)
        validate_positive_number("rate", rate)
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.emission = 1.0 / self.rate
        self.tau = self.capacity * self.emission
        # key -> TAT（理论到达时间，秒）。这是全部状态，故为 O(1)。
        self._tat = {}

    def theoretical_arrival_time(self, key, now=None):
        now = resolve_now(now)
        return self._tat.get(key, now)

    def tokens(self, key, now=None):
        """此刻还能立刻放行的单元数，供与令牌桶并排核对（向上取整前的整单元）。"""
        now = resolve_now(now)
        tat = self._tat.get(key)
        base = now if tat is None else max(tat, now)
        room = (now + self.tau - base) / self.emission
        return max(0.0, min(self.capacity, math.floor(room + 1e-12)))

    def _check(self, key, now, cost):
        """返回 (admissible, new_tat, wait)。不改状态，供 allow 与 wait_time 共用。"""
        tat = self._tat.get(key)
        # 空闲保护：tat 落后于 now 时以 now 为准，绝不把过去攒下的时间当额度发出去。
        base = now if tat is None or tat < now else tat
        new_tat = base + cost * self.emission
        if new_tat - self.tau <= now:
            return True, new_tat, 0.0
        return False, new_tat, new_tat - self.tau - now

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return True
        now = resolve_now(now)
        admissible, new_tat, _ = self._check(key, now, cost)
        if admissible:
            self._tat[key] = new_tat
            return True
        # 被拒不推进 TAT；但首见 key 时把 now 记为基线，避免下次从「无历史」重新给突发。
        if key not in self._tat:
            self._tat[key] = now
        return False

    def wait_time(self, key, now=None, cost=1):
        validate_cost(cost)
        if cost == 0:
            return 0.0
        now = resolve_now(now)
        _, _, wait = self._check(key, now, cost)
        if cost > self.capacity:
            # 单请求超过整桶突发，TAT 永远追不平，承诺 inf。
            return _INF
        return wait

    def memory_entries(self):
        return len(self._tat)

    def snapshot(self):
        return {"capacity": self.capacity, "rate": self.rate,
                "tat": dict(self._tat)}

    def restore(self, data):
        if data["capacity"] != self.capacity or data["rate"] != self.rate:
            raise ValueError("snapshot 与本限流器的 capacity/rate 不一致")
        self._tat = dict(data["tat"])
        return self
