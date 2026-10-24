"""限流器的公共约定：请求结构、时间解析与参数校验。

所有算法实现共用这里的取值规则，好处是「非法输入怎么拒绝」「时钟回拨怎么处理」这两条
只写一遍。`resolve_now` 把 `now=None` 翻译成真实墙上时间；`clamp_delta` 负责回拨保护：
任何算法在时间倒退时都必须既不崩溃、也不白送额度。
"""

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    """一条到达的请求：时刻、所属 key、消耗的成本（单位令牌数）。"""

    now: float
    key: str
    cost: int = 1


def resolve_now(now):
    """把调用方传入的 `now` 规范成浮点秒；`None` 表示取当前墙上时间。

    限流器本身不保存「当前时间」，时间永远由调用方给定，因此同一份状态喂进虚拟时钟
    就能复现结果。真实部署里传 `None` 用系统时间即可。
    """
    return time.time() if now is None else float(now)


def clamp_delta(now, last):
    """返回本次可用经过了多久。时钟回拨时返回 0，绝不返回负数。

    负数会让令牌桶「倒着补令牌」、让漏桶「倒着放水」，等于凭空多出一段额度；这是本
    库唯一的重置语义约定，所以集中在这里实现，避免每个算法各写一份而漏掉边界。
    """
    if last is None:
        return 0.0, now
    if now < last:
        return 0.0, last
    return now - last, now


def validate_positive_number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("%s 必须是数字，收到 %r" % (name, value))
    if not (value > 0):
        raise ValueError("%s 必须为正数，收到 %r" % (name, value))


def validate_capacity(name, value):
    """额度类参数：必须是 >= 1 的整数（一次窗口至少要能放行一个请求才有意义）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s 必须是整数，收到 %r" % (name, value))
    if value < 1:
        raise ValueError("%s 必须 >= 1，收到 %r" % (name, value))


def validate_cost(cost):
    """cost 必须是 >= 0 的整数。cost=0 表示「纯探测」，任何限流器都放行且不扣额度。"""
    if isinstance(cost, bool) or not isinstance(cost, int):
        raise TypeError("cost 必须是整数，收到 %r" % (cost,))
    if cost < 0:
        raise ValueError("cost 必须 >= 0，收到 %r" % (cost,))
