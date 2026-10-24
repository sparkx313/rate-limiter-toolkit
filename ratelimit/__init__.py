"""只用 Python 标准库实现的一组限流算法，外加一套把它们放到真实流量轨迹上量一量的基准。

六个算法各管一件事：`FixedWindowLimiter` 简单但会越界放行；`SlidingWindowLogLimiter`
精确但内存随流量涨；`SlidingWindowCounterLimiter` 用插值换回 O(1) 内存、代价是近似；
`TokenBucketLimiter` 允许突发；`LeakyBucketLimiter` 强制平滑；`GCRALimiter` 用单个理论
到达时间做到 O(1) 且精确。`MultiKeyLimiter` 决定多个 key 是各留额度还是共用额度，
`trace` 用固定种子生成可复现的到达轨迹。公共构造参数都是「每窗口多少」或「容量 + 速率」。
"""

from .fixed_window import FixedWindowLimiter
from .gcra import GCRALimiter
from .leaky_bucket import LeakyBucketLimiter
from .common import Request
from .multi_key import MultiKeyLimiter
from .sliding_counter import SlidingWindowCounterLimiter
from .sliding_log import SlidingWindowLogLimiter
from .token_bucket import TokenBucketLimiter
from .trace import VirtualClock, burst, diurnal, drip, generate, poisson

__version__ = "0.1.0"

__all__ = ["FixedWindowLimiter", "GCRALimiter", "LeakyBucketLimiter",
           "MultiKeyLimiter", "Request", "SlidingWindowCounterLimiter",
           "SlidingWindowLogLimiter", "TokenBucketLimiter", "VirtualClock",
           "burst", "diurnal", "drip", "generate", "poisson", "__version__"]
