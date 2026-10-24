# rate-limiter-toolkit

Six textbook rate-limiting algorithms and a reproducible benchmark that measures how much
traffic each one actually lets through a stream shaped like a real API gateway: Poisson
background, a sharp burst, a diurnal wave, and a slow drip. The target problem is
gateway-level throttling and per-tenant quotas — the place where "100 requests per minute"
has to survive a burst that straddles a minute boundary, or one tenant shouting louder than
the rest. Pure CPython standard library, no dependencies, deterministic on a virtual clock.

The point of the repo is the measurement, not the folklore. Two of the five candidate
claims were confirmed exactly as stated (fixed window over-admits 2.00x; sliding-log memory
is ~32 bytes/request and linear in traffic); two were more nuanced than the usual story
(the interpolated counter's sub-request error still flips 407 decisions; GCRA and the token
bucket are *numerically identical* here, not "different" as often claimed); one showed
fairness is a routing policy, not an algorithm property. The raw output of one run is in
[`benchmarks/last_run.txt`](benchmarks/last_run.txt) and every table below is copied from it
verbatim.

## What it does / does not do

Does:
- Implements fixed-window, sliding-window-log, sliding-window-counter (interpolated),
  token bucket, leaky bucket, and GCRA behind one interface — `allow(key, now=None, cost=1)`.
- Measures over-admission, memory scaling, approximation error, GCRA/token-bucket
  equivalence, and per-key fairness on a seeded virtual-clock trace.
- Handles a clock that goes backwards without crashing or granting extra credit.

Does not:
- It is single-process, in-memory. There is no Redis/etcd coordination; `snapshot()` /
  `restore()` hand you serializable state so you can move a limiter between calls, but two
  processes do not share one budget.
- `now=None` reads the wall clock, but the benchmark and tests drive an explicit virtual
  clock. Timings in the benchmark are wall-clock and not reproducible; the count/byte/error
  columns are.
- Leaky bucket here is the drop-on-full limiter variant, not a traffic *shaper* that queues
  and releases at a constant output rate.

## Install

No install step: standard library only.

```bash
git clone <this repo> && cd rate-limiter-toolkit
python -u -m unittest discover -s tests -p 'test_*.py' -t tests   # 82 tests
```

Python 3.9+ (verified on CPython 3.11.9). Optional `pip install -e .` registers the
`ratelimit` package via `pyproject.toml`.

## Quick start

Every block below was executed on CPython 3.11.9; the comments are the real outputs.
Passing an explicit `now=` is what makes these reproducible — omit it and the limiter reads
the system clock.

Fixed window, and the boundary reset:

```python
from ratelimit import FixedWindowLimiter

rl = FixedWindowLimiter(limit=2, window=10.0)
[rl.allow("k", now=t) for t in (0, 4, 8, 9)]   # [True, True, False, False]
rl.allow("k", now=10.0)                         # True — floor(10/10) is the next window
```

Sliding-log is exact and promises a real retry time:

```python
from ratelimit import SlidingWindowLogLimiter

rl = SlidingWindowLogLimiter(limit=2, window=10.0)
rl.allow("k", now=0.0); rl.allow("k", now=6.0)
rl.allow("k", now=7.0)                          # False — both events still inside the window
round(rl.wait_time("k", now=7.0), 3)            # 3.0 — until the t=0.0 event ages out
```

Token bucket drains then refills continuously:

```python
from ratelimit import TokenBucketLimiter

rl = TokenBucketLimiter(capacity=5, rate=1.0)
rl.allow("k", now=0.0, cost=5)                  # True — empties the whole bucket
rl.allow("k", now=0.0)                          # False — nothing left
rl.tokens("k", now=2.0)                         # 2.0 — two seconds of refill at 1/s
```

GCRA and the leaky bucket both cap a same-instant burst:

```python
from ratelimit import GCRALimiter, LeakyBucketLimiter

[GCRALimiter(5, 1.0).allow("k", now=0.0) for _ in range(7)]       # 5 True then 2 False
[LeakyBucketLimiter(3, 1.0).allow("k", now=0.0) for _ in range(5)]  # 3 True then 2 False
```

Multi-key limiter decides whether tenants contend or are isolated:

```python
from ratelimit import FixedWindowLimiter, MultiKeyLimiter
from ratelimit.trace import poisson
import random

events = poisson(random.Random(1), 0.0, 10.0, 20.0, ["hot", "cold"])   # 217 requests
mk = MultiKeyLimiter(FixedWindowLimiter(limit=5, window=10.0), shared_budget=False)
admitted = sum(mk.allow(e.key, e.now, e.cost) for e in events)         # 10 (5 per key)
```

State survives a `snapshot()` / `restore()` round trip:

```python
from ratelimit import GCRALimiter

rl = GCRALimiter(capacity=5, rate=1.0)
rl.allow("k", now=0.0, cost=5)                  # drain
copy = GCRALimiter(capacity=5, rate=1.0).restore(rl.snapshot())
copy.allow("k", now=0.0)                        # False — inherits the drained TAT
```

## Measured results

The tables below are copied line-for-line from `benchmarks/last_run.txt`, including the
column headers. Deterministic columns (counts, bytes, errors, ratios) reproduce on any
machine; the `ms (timing)` column in Section 7 does not — the benchmark says so inline.

Section 2 — a full window just before a boundary and a full window just after:

```text
scenario                           fixed admits exact admits over-factor
2x burst straddling one boundary            200          100      2.000
```

The fixed window let 200 through where the exact sliding window caps at 100 — the textbook
2x, measured.

Section 3 — sliding-log memory versus the O(1) windowed limiters (tracemalloc peak):

```text
requests       peak bytes  bytes/entry     fixed keys     ctr keys
10000              321888        32.19              1            1
100000            3224248        32.24              1            1
1000000          32249504        32.25              1            1
```

The exact log holds one entry per admitted request (~32 bytes each, linear in traffic); the
fixed-window and interpolated-counter limiters held 1e6 requests in 1 state entry each.

Section 4 — interpolated sliding-counter versus the exact log (limit 20, 4000 requests):

```text
metric                              value
max abs count error                 0.999
counter over-admissions               198
counter under-admissions              209
exact-log total admits                240
counter total admits                  229
```

The count error stays under one request, but that is enough to disagree with the exact log
on 407 decisions — and the bias goes both ways (198 over, 209 under), so the counter is not
a safe stand-in near the threshold.

Section 5 — GCRA versus the token bucket at the same `capacity`/`rate`:

```text
metric                         token bucket           GCRA        delta
total admits                             74             74            0
total rejects                           181            181            0
peak admits / 1s                         23             23            0
wait @ first reject                  0.1896         0.1896       0.0000
sum promised wait                     26.39          26.39         0.00
```

Against the usual claim that they differ on burst shape and promised wait, here they agree
on every column. With delay tolerance set to `capacity * emission`, GCRA is the token bucket
expressed with one float (the theoretical arrival time) instead of two — the real difference
is state size, not behaviour.

Section 6 — per-key fairness across a skewed trace (hot key offers ~68% of traffic):

```text
key           offered     isolated       shared
hot              1362          160          115
warm              380          160           20
cold-1            125          125           13
cold-2            133          132           12
```

Isolated per-key budgets cap the hot key and leave the cold keys 125 and 132 admits; a single
shared pool crowds them to 13 and 12. Same algorithm in both columns — fairness here is a
routing choice, not a property of the limiter.

Section 7 — one merged trace, all six limiters; `admitted`/`admit-rate` are deterministic,
`ms` is timing:

```text
algorithm            events     admitted     admit-rate  ms (timing)
fixed-window           9458         6000         0.6344         6.22
sliding-log            9458         5897         0.6235         8.89
sliding-counter        9458         5902         0.6240         8.43
token-bucket           9458         6092         0.6441         9.18
leaky-bucket           9458         6092         0.6441         9.39
gcra                   9458         6092         0.6441         7.37
```

The same nominal limit of 100/second produces a spread of 195 admits between the windowed
and rate-based families — burst allowance and boundary behaviour move real throughput.

## How it decides

Each class answers `allow(key, now, cost)` differently:

- **FixedWindowLimiter** — `index = floor(now / window)`; on a new index the counter resets,
  admit while `count + cost <= limit`. State is `(index, count)`, O(1) per key, but traffic
  near a boundary is not comparable to traffic near the middle of a window.
- **SlidingWindowLogLimiter** — keep a deque of admitted timestamps; pop everything `<= now -
  window`; admit while `len(events) + cost <= limit`, pushing `cost` entries. Exact. Memory
  grows with admitted volume. `wait_time` is the expiry of the entry that must age out.
- **SlidingWindowCounterLimiter** — keep `(index, curr, prev)`; the estimate is
  `prev * (window - offset)/window + curr` where `offset = now - index*window`. O(1) per key.
  Rolling: `curr -> prev`, `curr = 0`; a gap of two or more windows zeroes both.
- **TokenBucketLimiter** — `tokens = min(capacity, tokens + elapsed*rate)`; admit if
  `tokens >= cost`, subtract. Idle time refills up to one full bucket; state is
  `(tokens, last)`. `wait_time = (cost - tokens)/rate`.
- **LeakyBucketLimiter** — the inverse: `level = max(0, level - elapsed*rate)`; admit if
  `level + cost <= capacity`, add. Steady drain rate `rate` with no accumulated burst credit;
  `wait_time = (level + cost - capacity)/rate`.
- **GCRALimiter** — one value, the theoretical arrival time. `emission = 1/rate`,
  `tau = capacity*emission`. Let `base = max(tat, now)`, `new_tat = base + cost*emission`;
  admit while `new_tat - tau <= now`. On reject, `wait_time = new_tat - tau - now` and the TAT
  does not advance.

All six: `cost = 0` always admits and consumes nothing; `cost > capacity/limit` never admits
and `wait_time` returns `inf`; a `now` earlier than state already seen is clamped so it can
neither crash nor mint credit.

## Limitations

- In-process only. `snapshot()`/`restore()` move state between calls but there is no shared
  distributed budget.
- The virtual clock means order, not real concurrency: two requests at the same `now` are
  handled sequentially in trace order.
- The interpolated counter trades exactness for O(1) memory and is wrong near the threshold,
  as Section 4 shows.
- `time.time()` (used when `now=None`) can step backwards under NTP adjustment; the clamps
  prevent extra credit but a limiter cannot recover the requests it refused during a step.
- Leaky bucket drops rather than queues; for shaping (constant release rate) you need a queue.

## API

| Name | Constructor | `allow(key, now=None, cost=1)` | Extras |
|---|---|---|---|
| `FixedWindowLimiter` | `(limit, window)` | bool | `used`, `wait_time`, `memory_entries`, `snapshot`, `restore` |
| `SlidingWindowLogLimiter` | `(limit, window)` | bool | `used`, `wait_time`, `memory_entries`, `snapshot`, `restore` |
| `SlidingWindowCounterLimiter` | `(limit, window)` | bool | `approximate`, `used`, `wait_time`, `memory_entries`, `snapshot`, `restore` |
| `TokenBucketLimiter` | `(capacity, rate)` | bool | `tokens`, `wait_time`, `memory_entries`, `snapshot`, `restore` |
| `LeakyBucketLimiter` | `(capacity, rate)` | bool | `water_level`, `wait_time`, `memory_entries`, `snapshot`, `restore` |
| `GCRALimiter` | `(capacity, rate)` | bool | `tokens`, `theoretical_arrival_time`, `wait_time`, `memory_entries`, `snapshot`, `restore` |
| `MultiKeyLimiter` | `(limiter, shared_budget=False)` | bool | `wait_time`, `keys`, `memory_entries`, `snapshot`, `restore` |
| `trace.generate` | `(seed=..., span=..., ...)` | — | returns sorted `Request` list |
| `trace.poisson/burst/diurnal/drip` | see source | — | building blocks, all take `random.Random` |

`limit`/`capacity` must be `>= 1`; `window`/`rate` must be `> 0`; `cost` must be an int `>= 0`.
Violations raise `ValueError`/`TypeError` at construction or call time.

## Directory tree

```tree
rate-limiter-toolkit/
├── ratelimit/
│   ├── __init__.py            # public exports, __version__
│   ├── common.py              # Request, clock resolution, validation, back-shift guard
│   ├── fixed_window.py        # FixedWindowLimiter
│   ├── sliding_log.py         # SlidingWindowLogLimiter
│   ├── sliding_counter.py     # SlidingWindowCounterLimiter
│   ├── token_bucket.py        # TokenBucketLimiter
│   ├── leaky_bucket.py        # LeakyBucketLimiter
│   ├── gcra.py                # GCRALimiter
│   ├── multi_key.py           # MultiKeyLimiter (isolate vs shared budget)
│   └── trace.py               # VirtualClock + Poisson/burst/diurnal/drip generators
├── tests/
│   └── test_ratelimit.py      # 82 unittest cases
├── benchmarks/
│   ├── run_benchmarks.py      # 7 measured sections, tee-d to last_run.txt
│   └── last_run.txt           # verbatim output of one run
├── LICENSE
├── pyproject.toml
├── .gitignore
└── README.md
```

## Reproduce

```bash
# from the repository root
python -u -m unittest discover -s tests -p 'test_*.py' -t tests
python -u benchmarks/run_benchmarks.py
```

The benchmark prints to stdout and writes the identical text to
`benchmarks/last_run.txt`; it runs in about 15 seconds. Re-run it and the deterministic
columns stay put, only `ms (timing)` moves.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 sparkx313.
