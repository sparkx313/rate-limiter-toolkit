"""多键限流器：把若干 key 的额度按策略路由到底层算法上，用于度量租户间公平性。

`isolated` 模式给每个 key 一份独立额度（API 网关里常见的「每租户配额」），热 key 挤不动
冷 key。`shared` 模式让所有 key 共用同一份额度（「后端总闸」），此时到达多的 key 会占用
更多总容量——这就是 `fairness` 一节对比的对象。底层算法本身是带 key 参数的，这里只决定
同一个底层实例里 key 如何映射，所以两种模式复用了完全相同的算法实现。
"""

from .common import resolve_now, validate_cost

_SHARED_KEY = "\x00shared"


class MultiKeyLimiter:
    def __init__(self, limiter, shared_budget=False):
        self.limiter = limiter
        self.shared_budget = bool(shared_budget)
        self._keys = []

    def _route(self, key):
        return _SHARED_KEY if self.shared_budget else key

    def allow(self, key, now=None, cost=1):
        validate_cost(cost)
        if key not in self._keys:
            self._keys.append(key)
        return self.limiter.allow(self._route(key), now, cost)

    def wait_time(self, key, now=None, cost=1):
        validate_cost(cost)
        return self.limiter.wait_time(self._route(key), now, cost)

    def keys(self):
        return list(self._keys)

    def memory_entries(self):
        """已观测到的不同 key 数（公平性报告的分母）。"""
        return len(self._keys)

    def snapshot(self):
        return {"shared_budget": self.shared_budget, "keys": list(self._keys),
                "limiter": self.limiter.snapshot()}

    def restore(self, data):
        if data["shared_budget"] != self.shared_budget:
            raise ValueError("snapshot 与本限流器的 shared_budget 不一致")
        self._keys = list(data["keys"])
        self.limiter.restore(data["limiter"])
        return self
