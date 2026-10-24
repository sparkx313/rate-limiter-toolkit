"""`ratelimit` 包的单元测试。

运行：`python -u -m unittest discover -s tests -p 'test_*.py' -t tests`。

覆盖面按算法分节：首个/空请求、精确的窗口边界时刻、cost 大于容量、时钟回拨（不得崩溃、
不得白送额度）、非正 limits 的入参校验、两个限流器在同一条确定性轨迹上的等价性、令牌
补充算术、GCRA 的理论到达时间记账、多键隔离，以及暴露出的 snapshot/restore 往返。
统计或计时类的断言一律带显式容差。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ratelimit import (FixedWindowLimiter, GCRALimiter, LeakyBucketLimiter,
                       MultiKeyLimiter, SlidingWindowCounterLimiter,
                       SlidingWindowLogLimiter, TokenBucketLimiter,
                       VirtualClock, burst, diurnal, drip, generate, poisson)
from ratelimit.common import Request


def run_trace(limiter, events, key_attr=True):
    """把一条 (now, key, cost) 序列喂进 limiter，返回每次 allow 的布尔结果。"""
    results = []
    for ev in events:
        results.append(limiter.allow(ev.key, ev.now, ev.cost))
    return results


def trace(count=40, seed=7):
    """一条小规模、确定性的混合轨迹，用于等价性测试。"""
    evs = []
    import random
    rng = random.Random(seed)
    for i in range(count):
        evs.append(Request(i * 0.1 + rng.random() * 0.05,
                           "k%d" % (i % 3), 1))
    evs.sort(key=lambda e: e.now)
    return evs


class FixedWindowTests(unittest.TestCase):
    def test_first_request_allowed(self):
        self.assertTrue(FixedWindowLimiter(2, 10.0).allow("a", now=0.0))

    def test_limit_enforced_within_window(self):
        lim = FixedWindowLimiter(2, 10.0)
        self.assertEqual([lim.allow("a", now=t) for t in (0, 1, 2, 3)],
                         [True, True, False, False])

    def test_exact_boundary_instant_opens_new_window(self):
        lim = FixedWindowLimiter(1, 10.0)
        self.assertTrue(lim.allow("a", now=0.0))
        self.assertFalse(lim.allow("a", now=9.999))
        # now=10.0 恰好落到下一格（floor(10/10)=1），额度重置。
        self.assertTrue(lim.allow("a", now=10.0))

    def test_window_rollover_resets_count(self):
        lim = FixedWindowLimiter(2, 10.0)
        for t in (0, 1, 2):
            lim.allow("a", now=t)
        self.assertEqual(lim.used("a", now=9), 2)
        self.assertEqual(lim.used("a", now=10), 0)

    def test_cost_over_limit_always_rejected(self):
        lim = FixedWindowLimiter(2, 10.0)
        self.assertFalse(lim.allow("a", now=0.0, cost=3))
        self.assertEqual(lim.used("a", now=0.0), 0)

    def test_cost_equal_to_limit(self):
        lim = FixedWindowLimiter(3, 10.0)
        self.assertTrue(lim.allow("a", now=0.0, cost=3))
        self.assertFalse(lim.allow("a", now=1.0))

    def test_cost_zero_is_free(self):
        lim = FixedWindowLimiter(1, 10.0)
        self.assertTrue(lim.allow("a", now=0.0))
        # cost=0 不消耗额度，满窗时依然放行。
        self.assertTrue(lim.allow("a", now=1.0, cost=0))

    def test_clock_backwards_grants_no_credit(self):
        lim = FixedWindowLimiter(2, 10.0)
        lim.allow("a", now=15.0)
        lim.allow("a", now=15.5)
        # 回拨到窗口内更早时刻，不得把计数清零再放行。
        self.assertFalse(lim.allow("a", now=12.0))
        self.assertEqual(lim.used("a", now=12.0), 2)

    def test_wait_time_full_window(self):
        lim = FixedWindowLimiter(1, 10.0)
        lim.allow("a", now=3.0)
        self.assertAlmostEqual(lim.wait_time("a", now=5.0), 5.0)

    def test_wait_time_not_full(self):
        lim = FixedWindowLimiter(2, 10.0)
        lim.allow("a", now=3.0)
        self.assertEqual(lim.wait_time("a", now=5.0), 0.0)

    def test_wait_time_cost_over_limit_is_infinite(self):
        import math
        lim = FixedWindowLimiter(2, 10.0)
        self.assertEqual(lim.wait_time("a", now=0.0, cost=5), math.inf)

    def test_invalid_limit(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                FixedWindowLimiter(bad, 10.0)
        with self.assertRaises(TypeError):
            FixedWindowLimiter(2.5, 10.0)

    def test_invalid_window(self):
        for bad in (0, -1.0):
            with self.assertRaises(ValueError):
                FixedWindowLimiter(2, bad)

    def test_invalid_cost(self):
        lim = FixedWindowLimiter(2, 10.0)
        with self.assertRaises(ValueError):
            lim.allow("a", now=0.0, cost=-1)
        with self.assertRaises(TypeError):
            lim.allow("a", now=0.0, cost=1.5)

    def test_snapshot_roundtrip(self):
        lim = FixedWindowLimiter(3, 10.0)
        for t in (0, 1, 2):
            lim.allow("a", now=t)
        data = lim.snapshot()
        fresh = FixedWindowLimiter(3, 10.0)
        fresh.restore(data)
        self.assertEqual(fresh.allow("a", now=9.0),
                         lim.allow("a", now=9.0))
        self.assertEqual(fresh.used("a", now=9.0), 3)

    def test_snapshot_rejects_mismatched_config(self):
        data = FixedWindowLimiter(3, 10.0).snapshot()
        with self.assertRaises(ValueError):
            FixedWindowLimiter(5, 10.0).restore(data)


class SlidingLogTests(unittest.TestCase):
    def test_first_request_allowed(self):
        self.assertTrue(SlidingWindowLogLimiter(2, 10.0).allow("a", now=0.0))

    def test_exact_boundary_expires_oldest(self):
        lim = SlidingWindowLogLimiter(1, 10.0)
        self.assertTrue(lim.allow("a", now=0.0))
        self.assertFalse(lim.allow("a", now=9.999))
        # t=0 的事件在 now=10 时恰好滑出（0 <= now-window=0）。
        self.assertTrue(lim.allow("a", now=10.0))

    def test_sliding_not_fixed(self):
        lim = SlidingWindowLogLimiter(2, 10.0)
        self.assertTrue(lim.allow("a", now=0.0))
        self.assertTrue(lim.allow("a", now=1.0))
        # 固定窗口到 t=10 才重置，滑动日志不会：第 3 个在 now=3 仍被拒。
        self.assertFalse(lim.allow("a", now=3.0))

    def test_wait_time_promises_oldest_expiry(self):
        lim = SlidingWindowLogLimiter(2, 10.0)
        lim.allow("a", now=0.0)
        lim.allow("a", now=4.0)
        # 需等最老事件（now=0）过期 → 10.0；从 now=6 起算还差 4 秒。
        self.assertAlmostEqual(lim.wait_time("a", now=6.0), 4.0)

    def test_cost_over_limit_rejected(self):
        lim = SlidingWindowLogLimiter(2, 10.0)
        self.assertFalse(lim.allow("a", now=0.0, cost=5))
        self.assertEqual(lim.memory_entries("a"), 0)

    def test_cost_consumes_multiple_entries(self):
        lim = SlidingWindowLogLimiter(3, 10.0)
        self.assertTrue(lim.allow("a", now=0.0, cost=2))
        self.assertEqual(lim.memory_entries("a"), 2)
        self.assertFalse(lim.allow("a", now=0.0, cost=2))

    def test_clock_backwards_no_credit(self):
        lim = SlidingWindowLogLimiter(2, 10.0)
        lim.allow("a", now=20.0)
        lim.allow("a", now=20.0)
        # 回拨不恢复已被剔除的旧事件，也不会腾出空间。
        self.assertFalse(lim.allow("a", now=15.0))

    def test_memory_grows_with_traffic(self):
        lim = SlidingWindowLogLimiter(1000, 100.0)
        for i in range(50):
            lim.allow("a", now=float(i))
        self.assertEqual(lim.memory_entries("a"), 50)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            SlidingWindowLogLimiter(0, 10.0)
        with self.assertRaises(ValueError):
            SlidingWindowLogLimiter(2, 0.0)

    def test_snapshot_roundtrip(self):
        lim = SlidingWindowLogLimiter(3, 10.0)
        for t in (0, 2, 4):
            lim.allow("a", now=t)
        fresh = SlidingWindowLogLimiter(3, 10.0)
        fresh.restore(lim.snapshot())
        self.assertEqual(list(fresh.snapshot()["events"]["a"]),
                         [0.0, 2.0, 4.0])


class SlidingCounterTests(unittest.TestCase):
    def test_first_request_allowed(self):
        self.assertTrue(SlidingWindowCounterLimiter(2, 10.0).allow("a", now=0.0))

    def test_within_single_window_matches_log(self):
        log = SlidingWindowLogLimiter(3, 10.0)
        ctr = SlidingWindowCounterLimiter(3, 10.0)
        times = (0, 2, 4, 6, 8, 9)
        self.assertEqual([log.allow("a", now=t) for t in times],
                         [ctr.allow("a", now=t) for t in times])

    def test_approximation_error_bounded_vs_log(self):
        # 插值近似的窗口用量与精确日志之差，最多不超过一个完整窗口的真实计数。
        log = SlidingWindowLogLimiter(10, 10.0)
        ctr = SlidingWindowCounterLimiter(10, 10.0)
        max_err = 0.0
        for t in range(0, 30):
            now = float(t)
            allow = log.allow("a", now=now)
            ctr.allow("a", now=now)
            err = abs(ctr.approximate("a", now=now) - log.used("a", now=now))
            max_err = max(max_err, err)
        self.assertLess(max_err, 1e-9 + 10.0)  # 容差：不超过单窗口上限

    def test_boundary_uses_interpolation(self):
        ctr = SlidingWindowCounterLimiter(4, 10.0)
        for _ in range(4):
            ctr.allow("a", now=1.0)  # 上一窗口满 4
        # 进入下一窗口 offset 很小 → prev 权重接近 1 → 近似仍接近 4，应被拒。
        self.assertFalse(ctr.allow("a", now=10.1))
        # 到窗口末尾 prev 权重趋 0 → 近似趋 0 → 放行。
        self.assertTrue(ctr.allow("a", now=19.9))

    def test_wait_time_full_in_window_waits_to_boundary(self):
        ctr = SlidingWindowCounterLimiter(1, 10.0)
        ctr.allow("a", now=3.0)
        # curr 已装满 limit，只能等下一格边界。
        self.assertAlmostEqual(ctr.wait_time("a", now=5.0), 5.0)

    def test_cost_over_limit_rejected(self):
        ctr = SlidingWindowCounterLimiter(2, 10.0)
        self.assertFalse(ctr.allow("a", now=0.0, cost=5))

    def test_clock_backwards_no_credit(self):
        ctr = SlidingWindowCounterLimiter(2, 10.0)
        ctr.allow("a", now=25.0)
        ctr.allow("a", now=25.0)
        self.assertFalse(ctr.allow("a", now=21.0))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            SlidingWindowCounterLimiter(-1, 10.0)
        with self.assertRaises(TypeError):
            SlidingWindowCounterLimiter(2, "x")

    def test_snapshot_roundtrip(self):
        ctr = SlidingWindowCounterLimiter(3, 10.0)
        for t in (0, 1, 2):
            ctr.allow("a", now=t)
        fresh = SlidingWindowCounterLimiter(3, 10.0).restore(ctr.snapshot())
        self.assertEqual(fresh.approximate("a", now=5.0),
                         ctr.approximate("a", now=5.0))


class TokenBucketTests(unittest.TestCase):
    def test_starts_full(self):
        tb = TokenBucketLimiter(5, 1.0)
        self.assertEqual([tb.allow("a", now=0.0) for _ in range(5)].count(True), 5)
        self.assertFalse(tb.allow("a", now=0.0))

    def test_refill_arithmetic(self):
        tb = TokenBucketLimiter(4, 2.0)  # 2 令牌/秒
        for _ in range(4):
            tb.allow("a", now=0.0)       # 掏空
        # 1 秒后应补 min(capacity, 0+1*2)=2 个。
        self.assertAlmostEqual(tb.tokens("a", now=1.0), 2.0)
        self.assertTrue(tb.allow("a", now=1.0, cost=2))
        self.assertAlmostEqual(tb.tokens("a", now=1.0), 0.0, places=6)

    def test_refill_caps_at_capacity(self):
        tb = TokenBucketLimiter(4, 1.0)
        for _ in range(4):
            tb.allow("a", now=0.0)
        # 空闲很久也不得超过 capacity。
        self.assertAlmostEqual(tb.tokens("a", now=1000.0), 4.0)

    def test_steady_state_rate(self):
        tb = TokenBucketLimiter(3, 1.0)  # 稳态 1/秒
        admitted = 0
        for i in range(200):
            if tb.allow("a", now=float(i)):
                admitted += 1
        # 每步 1 秒、速率 1/秒 → 除初始突发外应约等于步数，容差给突发 +1。
        self.assertGreaterEqual(admitted, 190)
        self.assertLessEqual(admitted, 200)

    def test_cost_over_capacity_gives_infinite_wait(self):
        import math
        tb = TokenBucketLimiter(3, 1.0)
        self.assertFalse(tb.allow("a", now=0.0, cost=10))
        self.assertEqual(tb.wait_time("a", now=0.0, cost=10), math.inf)

    def test_wait_time_matches_deficit(self):
        tb = TokenBucketLimiter(1, 0.5)  # 2 秒补一个
        tb.allow("a", now=0.0)           # 掏空
        self.assertAlmostEqual(tb.wait_time("a", now=0.0), 2.0)

    def test_clock_backwards_no_credit(self):
        tb = TokenBucketLimiter(5, 1.0)
        tb.allow("a", now=100.0, cost=5)
        # 回拨不得按负经过时间扣令牌，也不得补令牌；余额保持被掏空。
        self.assertAlmostEqual(tb.tokens("a", now=50.0), 0.0, places=6)
        self.assertFalse(tb.allow("a", now=50.0))

    def test_cost_zero_free(self):
        tb = TokenBucketLimiter(1, 1.0)
        tb.allow("a", now=0.0)
        self.assertTrue(tb.allow("a", now=0.0, cost=0))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            TokenBucketLimiter(0, 1.0)
        with self.assertRaises(ValueError):
            TokenBucketLimiter(5, 0.0)
        with self.assertRaises(ValueError):
            TokenBucketLimiter(5, -1.0)

    def test_snapshot_roundtrip(self):
        tb = TokenBucketLimiter(5, 1.0)
        tb.allow("a", now=10.0, cost=3)
        snap = tb.snapshot()
        fresh = TokenBucketLimiter(5, 1.0).restore(snap)
        self.assertAlmostEqual(fresh.tokens("a", now=10.0),
                               tb.tokens("a", now=10.0), places=9)


class LeakyBucketTests(unittest.TestCase):
    def test_first_request_allowed(self):
        self.assertTrue(LeakyBucketLimiter(3, 1.0).allow("a", now=0.0))

    def test_smooths_burst(self):
        lb = LeakyBucketLimiter(3, 1.0)
        # 同一时刻最多放行 capacity 个，攒不出额外突发。
        admitted = sum(1 for _ in range(10) if lb.allow("a", now=0.0))
        self.assertEqual(admitted, 3)

    def test_drain_arithmetic(self):
        lb = LeakyBucketLimiter(4, 2.0)  # 2/秒漏出
        for _ in range(4):
            lb.allow("a", now=0.0)       # 灌满
        self.assertAlmostEqual(lb.water_level("a", now=0.0), 4.0)
        # 1 秒后漏到 2。
        self.assertAlmostEqual(lb.water_level("a", now=1.0), 2.0)

    def test_wait_time_to_make_room(self):
        lb = LeakyBucketLimiter(2, 1.0)
        lb.allow("a", now=0.0)
        lb.allow("a", now=0.0)           # 满
        self.assertAlmostEqual(lb.wait_time("a", now=0.0), 1.0)

    def test_cost_over_capacity_never(self):
        import math
        lb = LeakyBucketLimiter(2, 1.0)
        self.assertFalse(lb.allow("a", now=0.0, cost=5))
        self.assertEqual(lb.wait_time("a", now=0.0, cost=5), math.inf)

    def test_clock_backwards_no_credit(self):
        lb = LeakyBucketLimiter(3, 1.0)
        lb.allow("a", now=100.0, cost=3)  # 灌满
        # 回拨不得多漏水。
        self.assertAlmostEqual(lb.water_level("a", now=50.0), 3.0)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            LeakyBucketLimiter(0, 1.0)
        with self.assertRaises(ValueError):
            LeakyBucketLimiter(3, 0.0)

    def test_snapshot_roundtrip(self):
        lb = LeakyBucketLimiter(5, 1.0)
        lb.allow("a", now=10.0, cost=2)
        fresh = LeakyBucketLimiter(5, 1.0).restore(lb.snapshot())
        self.assertAlmostEqual(fresh.water_level("a", now=10.0),
                               lb.water_level("a", now=10.0), places=9)


class GCRATests(unittest.TestCase):
    def test_burst_equals_capacity(self):
        g = GCRALimiter(3, 1.0)
        admitted = sum(1 for _ in range(6) if g.allow("a", now=0.0))
        self.assertEqual(admitted, 3)

    def test_steady_rate(self):
        g = GCRALimiter(1, 2.0)  # 0.5 秒一个
        admitted = sum(1 for i in range(100) if g.allow("a", now=i * 0.5))
        self.assertGreaterEqual(admitted, 95)  # 容差覆盖首格边界
        self.assertLessEqual(admitted, 100)

    def test_tat_bookkeeping(self):
        g = GCRALimiter(3, 1.0)  # emission=1.0
        start = 100.0
        self.assertTrue(g.allow("a", now=start, cost=3))
        # 一次 cost=3 的放行把 TAT 推进 3 个 emission。
        self.assertAlmostEqual(g.theoretical_arrival_time("a", now=start), start + 3.0)
        self.assertTrue(g.allow("a", now=start + 3.0))
        # 下一格起算，再放行 1 个单元，TAT 只前进 1 个 emission。
        self.assertAlmostEqual(g.theoretical_arrival_time("a", now=start + 3.0), start + 4.0)

    def test_tat_never_behind_now(self):
        g = GCRALimiter(2, 1.0)
        g.allow("a", now=50.0)
        # 空闲后 TAT 落后；下一次以 now 为基线，不发放过去攒下的额度。
        self.assertTrue(g.allow("a", now=100.0))
        self.assertGreaterEqual(g.theoretical_arrival_time("a", now=100.0), 100.0)

    def test_wait_time_positive_on_reject(self):
        g = GCRALimiter(2, 1.0)
        g.allow("a", now=0.0, cost=2)  # 用满突发
        wait = g.wait_time("a", now=0.0)
        self.assertGreaterEqual(wait, 0.0)
        self.assertTrue(g.allow("a", now=wait))

    def test_cost_over_capacity_infinite_wait(self):
        import math
        g = GCRALimiter(2, 1.0)
        self.assertFalse(g.allow("a", now=0.0, cost=5))
        self.assertEqual(g.wait_time("a", now=0.0, cost=5), math.inf)

    def test_equivalent_to_token_bucket_on_trace(self):
        # 相同的 capacity/rate，GCRA 与令牌桶在同一条轨迹上放行数量一致。
        evs = trace(120, seed=3)
        bucket = TokenBucketLimiter(5, 2.0)
        gcra = GCRALimiter(5, 2.0)
        b = sum(run_trace(bucket, evs))
        c = sum(run_trace(gcra, evs))
        self.assertLessEqual(abs(b - c), 1)  # 容差 1：浮点边界

    def test_clock_backwards_no_credit(self):
        g = GCRALimiter(2, 1.0)
        g.allow("a", now=100.0, cost=2)  # 满
        self.assertFalse(g.allow("a", now=99.0))  # 回拨不得放行

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            GCRALimiter(0, 1.0)
        with self.assertRaises(ValueError):
            GCRALimiter(2, 0.0)

    def test_snapshot_roundtrip(self):
        g = GCRALimiter(3, 1.0)
        g.allow("a", now=10.0, cost=2)
        snap = g.snapshot()
        fresh = GCRALimiter(3, 1.0).restore(snap)
        self.assertAlmostEqual(fresh.theoretical_arrival_time("a", now=10.0),
                               g.theoretical_arrival_time("a", now=10.0), places=9)

    def test_tokens_matches_capacity_when_idle(self):
        g = GCRALimiter(4, 1.0)
        g.allow("a", now=0.0)  # 只初始化 TAT
        self.assertAlmostEqual(g.tokens("a", now=50.0), 4.0)


class MultiKeyTests(unittest.TestCase):
    def test_isolation_between_keys(self):
        base = FixedWindowLimiter(1, 10.0)
        mk = MultiKeyLimiter(base, shared_budget=False)
        self.assertTrue(mk.allow("hot", now=0.0))
        self.assertFalse(mk.allow("hot", now=1.0))
        # 独立额度下，冷 key 不受热 key 影响。
        self.assertTrue(mk.allow("cold", now=2.0))

    def test_shared_budget_contends(self):
        base = FixedWindowLimiter(2, 10.0)
        mk = MultiKeyLimiter(base, shared_budget=True)
        self.assertTrue(mk.allow("a", now=0.0))
        self.assertTrue(mk.allow("b", now=0.0))
        # 共用总闸：第 3 个请求无论哪个 key 都被拒。
        self.assertFalse(mk.allow("c", now=0.0))

    def test_keys_tracking(self):
        mk = MultiKeyLimiter(FixedWindowLimiter(2, 10.0))
        mk.allow("x", now=0.0)
        mk.allow("y", now=0.0)
        mk.allow("x", now=0.0)
        self.assertEqual(sorted(mk.keys()), ["x", "y"])
        self.assertEqual(mk.memory_entries(), 2)

    def test_wait_time_delegates(self):
        # 共用总闸时 key "b" 走的是同一条额度，等待时间按已占用的那条窗口算。
        mk = MultiKeyLimiter(FixedWindowLimiter(1, 10.0), shared_budget=True)
        mk.allow("a", now=2.0)
        self.assertAlmostEqual(mk.wait_time("b", now=3.0), 7.0)

    def test_invalid_cost(self):
        mk = MultiKeyLimiter(FixedWindowLimiter(1, 10.0))
        with self.assertRaises(ValueError):
            mk.allow("a", now=0.0, cost=-2)

    def test_snapshot_roundtrip(self):
        mk = MultiKeyLimiter(SlidingWindowLogLimiter(3, 10.0), shared_budget=True)
        mk.allow("a", now=0.0, cost=2)
        fresh = MultiKeyLimiter(SlidingWindowLogLimiter(3, 10.0),
                                shared_budget=True).restore(mk.snapshot())
        self.assertEqual(sorted(fresh.keys()), ["a"])
        self.assertFalse(fresh.allow("b", now=1.0, cost=2))

    def test_shared_snapshot_rejects_mismatch(self):
        data = MultiKeyLimiter(FixedWindowLimiter(1, 10.0)).snapshot()
        with self.assertRaises(ValueError):
            MultiKeyLimiter(FixedWindowLimiter(1, 10.0),
                            shared_budget=True).restore(data)


class TraceTests(unittest.TestCase):
    def test_deterministic(self):
        a = generate(seed=42, span=5.0, keys=("a", "b"))
        b = generate(seed=42, span=5.0, keys=("a", "b"))
        self.assertEqual([(r.now, r.key, r.cost) for r in a],
                         [(r.now, r.key, r.cost) for r in b])

    def test_different_seed_differs(self):
        a = generate(seed=1, span=5.0, keys=("a",))
        b = generate(seed=2, span=5.0, keys=("a",))
        self.assertNotEqual([r.now for r in a], [r.now for r in b])

    def test_sorted_and_nonnegative(self):
        evs = generate(seed=99, span=8.0, keys=("a", "b", "c"))
        self.assertTrue(all(e.now >= 0.0 for e in evs))
        self.assertEqual([e.now for e in evs], sorted(e.now for e in evs))

    def test_poisson_count_within_tolerance(self):
        import random
        rng = random.Random(5)
        # 期望 ~ rate*span = 500；泊松标准差 ~ sqrt(500)≈22.4，容差放到 ±6σ≈135。
        n = len(poisson(rng, 0.0, 10.0, 50.0, ["a"]))
        self.assertGreater(n, 500 - 135)
        self.assertLess(n, 500 + 135)

    def test_burst_count_exact(self):
        import random
        rng = random.Random(1)
        evs = burst(rng, 30.0, 0.05, 40, ["a"])
        self.assertEqual(len(evs), 40)
        self.assertTrue(all(30.0 <= e.now <= 30.05 for e in evs))

    def test_diurnal_produces_events(self):
        import random
        rng = random.Random(3)
        evs = diurnal(rng, 0.0, 40.0, 50.0, 0.5, 20.0, ["a"])
        self.assertGreater(len(evs), 0)
        self.assertTrue(all(0.0 <= e.now < 40.0 for e in evs))

    def test_drip_exact_interval(self):
        import random
        rng = random.Random(0)
        evs = drip(rng, 0.0, 10.0, 2.0, "a")
        self.assertEqual([e.now for e in evs], [0.0, 2.0, 4.0, 6.0, 8.0])

    def test_virtual_clock_monotonic_guard(self):
        clock = VirtualClock(0.0)
        clock.advance(5.0)
        self.assertEqual(clock.now(), 5.0)
        with self.assertRaises(ValueError):
            clock.set(-1.0)


class CrossAlgorithmEquivalenceTests(unittest.TestCase):
    def test_two_identical_limiters_agree(self):
        evs = trace(80, seed=11)
        a = FixedWindowLimiter(4, 5.0)
        b = FixedWindowLimiter(4, 5.0)
        self.assertEqual(run_trace(a, evs), run_trace(b, evs))

    def test_log_and_counter_agree_when_no_second_window(self):
        # 单窗口内（无上一窗口参与插值）两者必须完全一致。
        log = SlidingWindowLogLimiter(3, 100.0)
        ctr = SlidingWindowCounterLimiter(3, 100.0)
        times = [float(i) for i in range(50)]
        self.assertEqual([log.allow("a", now=t) for t in times],
                         [ctr.allow("a", now=t) for t in times])

    def test_leaky_vs_bucket_steady_throughput(self):
        # 相同 capacity/rate 下，长时间恒定到达两者稳态吞吐相当（容差 2%）。
        rate, cap, span = 10.0, 5.0, 60.0
        evs = [Request(i / rate, "a", 1) for i in range(int(rate * span))]
        lb = LeakyBucketLimiter(cap, rate)
        tb = TokenBucketLimiter(cap, rate)
        admitted_lb = sum(run_trace(lb, evs))
        admitted_tb = sum(run_trace(tb, evs))
        self.assertLessEqual(abs(admitted_lb - admitted_tb), 0.02 * len(evs) + 1)


if __name__ == "__main__":
    unittest.main()
