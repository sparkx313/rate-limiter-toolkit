"""确定性虚拟时钟与流量轨迹生成器。

所有事件都携带一个显式 `now`，限流器只认这个 `now`、不读系统时间，因此同一随机种子在
任何机器上产出的整条轨迹完全一致，测量才可复现。四种到达形态按需组合：

- 泊松：指数间隔，恒定平均速率，模拟背景噪声。
- 突发：一段极窄时间内堆一大把请求，用来触发固定窗口的跨界越界。
- 昼夜波：速率按正弦起伏，模拟真实日/晚高峰。
- 慢滴：固定间隔的零星请求，用来验证长期稳态。
"""

import math
import random

from .common import Request


class VirtualClock:
    """只向前走的人工时钟：`advance` 累加、`set` 允许回拨但拒绝倒退到负数。

    限流器本身不引用它——轨迹里的 `now` 是生成时就写死进 `Request` 的。这个类只是让
    交互式测试能逐步推进时间，同时保持「时间非负」这一条不变式。
    """

    def __init__(self, start=0.0):
        self._t = float(start)

    def now(self):
        return self._t

    def advance(self, delta):
        self._t += delta
        return self._t

    def set(self, value):
        if value < 0:
            raise ValueError("虚拟时钟不允许走到负数时刻，收到 %r" % (value,))
        self._t = float(value)
        return self._t


def _key_sampler(keys, weights):
    """返回一个函数，按 `weights` 从 `keys` 里抽样；无权重则均匀。"""
    if weights is None:
        return lambda rng: rng.choice(keys)
    return lambda rng: rng.choices(keys, weights=weights)[0]


def poisson(rng, start, span, rate, keys, weights=None, cost=1):
    """在 [start, start+span) 内按平均速率 `rate` 撒泊松到达。"""
    sampler = _key_sampler(keys, weights)
    events = []
    t = start
    while True:
        t += rng.expovariate(rate)
        if t >= start + span:
            break
        events.append(Request(t, sampler(rng), cost))
    return events


def burst(rng, start, width, count, keys, weights=None, cost=1):
    """在 [start, start+width) 这段时间里堆 `count` 个请求，制造尖峰。"""
    sampler = _key_sampler(keys, weights)
    return [Request(start + rng.random() * width, sampler(rng), cost)
            for _ in range(count)]


def diurnal(rng, start, span, base_rate, amplitude, period, keys, weights=None,
            cost=1):
    """速率 = base_rate * (1 + amplitude * sin(2*pi*(t-start)/period)) 的非齐次泊松到达。

    用 Lewis-Shedden 稀释法实现：先按峰值速率 `peak = base_rate*(1+amplitude)` 撒一个齐次
    泊松流，再以 `rate(t)/peak` 的概率逐个保留。这样事件数期望 = base_rate*span，且时间
    间隔仍是指数分布；给定同一个 rng 时产出完全确定。
    """
    sampler = _key_sampler(keys, weights)
    end = start + span
    peak = base_rate * (1.0 + amplitude)
    events = []
    t = start
    while True:
        t += rng.expovariate(peak)
        if t >= end:
            break
        rate_t = base_rate * (1.0 + amplitude *
                              math.sin(2.0 * math.pi * (t - start) / period))
        if rng.random() < rate_t / peak:
            events.append(Request(t, sampler(rng), cost))
    return events


def drip(rng, start, span, every, key, cost=1):
    """从 start 起每隔 `every` 秒放一个固定 key 的请求，模拟零星稳态流量。"""
    events = []
    t = start
    while t < start + span:
        events.append(Request(t, key, cost))
        t += every
    return events


def generate(seed=20260101, span=60.0, poisson_rate=120.0, keys=("tenant-a",),
             weights=None, burst_start=30.0, burst_width=0.05, burst_count=40,
             diurnal_base_rate=60.0, diurnal_amplitude=0.6,
             diurnal_period=20.0, drip_every=3.0):
    """把四种形态拼成一条按时间升序排序的轨迹，返回 `Request` 列表。

    种子决定全部随机性；`span` 与各速率参数共同决定事件规模（默认约 1e4 量级，够基准用
    又不至于跑太久）。这里刻意不做去重、也不强行凑整数事件数——测量的是「这样一条轨迹
    实际有多少事件」，而不是「我以为有多少」。
    """
    rng = random.Random(seed)
    events = []
    events += poisson(rng, 0.0, span, poisson_rate, keys, weights)
    events += burst(rng, burst_start, burst_width, burst_count, keys, weights)
    events += diurnal(rng, 0.0, span, diurnal_base_rate, diurnal_amplitude,
                      diurnal_period, keys, weights)
    events += drip(rng, 0.0, span, drip_every, keys[0])
    events.sort(key=lambda r: r.now)
    return events
