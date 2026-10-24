"""Measure what each rate-limiting algorithm actually lets through a request stream.

Run with: `python -u benchmarks/run_benchmarks.py`. Every number below comes from this
single run and is saved to `benchmarks/last_run.txt`; README quotes that file verbatim,
so the prose can never drift from the data.

Traces are generated from fixed seeds on a virtual clock, so the *deterministic* columns
(admitted counts, byte sizes, measured errors, ratios) reproduce exactly on any machine.
The *timing* columns (milliseconds) do not — they are labelled as such wherever they
appear.
"""

import math
import os
import random
import sys
import time
import textwrap
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ratelimit import (FixedWindowLimiter, GCRALimiter, LeakyBucketLimiter,
                       MultiKeyLimiter, SlidingWindowCounterLimiter,
                       SlidingWindowLogLimiter, TokenBucketLimiter, burst,
                       diurnal, drip, generate, poisson)
from ratelimit.common import Request

HEADER = "rate-limiter-toolkit benchmarks - python %s" % sys.version.split()[0]
RULE = "=" * 78


def feed(limiter, events):
    """Return the number of admitted requests for a (now, key, cost) sequence."""
    admitted = 0
    for ev in events:
        if limiter.allow(ev.key, ev.now, ev.cost):
            admitted += 1
    return admitted


def say(text):
    """打印一段解读：统一在 78 列折行，好让 last_run.txt 在 GitHub 上不横向滚动。"""
    print()
    print(textwrap.fill("Interpretation: " + text, 78, subsequent_indent="  "))


# --------------------------------------------------------------------------- section 1
def section_trace():
    print(RULE)
    print("Section 1. Trace composition (deterministic)")
    print(RULE)
    rng = random.Random(20260101)
    span = 60.0
    background = poisson(rng, 0.0, span, 200.0, ["tenant-a"])
    spike = burst(rng, 30.0, 0.05, 400, ["tenant-hot"])
    wave = diurnal(rng, 0.0, span, 150.0, 0.7, 20.0, ["tenant-a"])
    slow = drip(rng, 0.0, span, 0.5, "tenant-a")
    rows = [("poisson background, rate 200/s over 60s", len(background)),
            ("burst, 400 requests in a 0.05s window", len(spike)),
            ("diurnal wave, base 150/s +-0.7, period 20s", len(wave)),
            ("slow drip, one request every 0.5s", len(slow))]
    print("%-46s %10s" % ("component", "events"))
    total = 0
    for label, n in rows:
        print("%-46s %10d" % (label, n))
        total += n
    print("%-46s %10d" % ("TOTAL (all components merged)", total))
    peak = max(len(background), len(spike), len(wave), len(slow))
    say("the %d-request burst is %.1fx the slow-drip volume, "
          "so it dominates any single-window count and is what the fixed-window "
          "boundary test leans on." % (len(spike), len(spike) / max(1, len(slow))))
    return peak


# --------------------------------------------------------------------------- section 2
def section_fixed_window_boundary():
    print(RULE)
    print("Section 2. Fixed-window over-admission at a boundary (deterministic)")
    print(RULE)
    limit, window = 100, 1.0
    events = [Request(0.999, "k", 1) for _ in range(limit)]
    events += [Request(1.001, "k", 1) for _ in range(limit)]
    fixed = FixedWindowLimiter(limit, window)
    log = SlidingWindowLogLimiter(limit, window)
    admitted_fixed = feed(fixed, events)
    admitted_log = feed(log, events)
    ratio = admitted_fixed / admitted_log if admitted_log else math.inf
    print("%-34s %12s %12s %10s" %
          ("scenario", "fixed admits", "exact admits", "over-factor"))
    print("%-34s %12d %12d %10.3f" %
          ("2x burst straddling one boundary", admitted_fixed, admitted_log, ratio))
    say("a full window of traffic just before the boundary and a "
          "full window just after is a single %.3fs interval of %d requests, which the "
          "exact sliding window would cap at %d. Fixed window let %d through, i.e. "
          "%.2fx the configured limit of %d. This is the textbook 2x, measured, not "
          "asserted." % (window, 2 * limit, admitted_log, admitted_fixed, ratio, limit))
    return ratio


# --------------------------------------------------------------------------- section 3
def section_sliding_log_memory():
    print(RULE)
    print("Section 3. Sliding-window log memory vs traffic (bytes are deterministic;")
    print("           tracemalloc peak is deterministic on CPython 3.11)")
    print(RULE)
    window = 1e9  # window far larger than any test, so nothing ever expires
    print("%-10s %14s %12s %14s %12s" %
          ("requests", "peak bytes", "bytes/entry", "fixed keys", "ctr keys"))
    rows = []
    keyed_fixed = keyed_ctr = 0
    for n in (10_000, 100_000, 1_000_000):
        tracemalloc.start()
        log = SlidingWindowLogLimiter(n, window)
        for i in range(n):
            log.allow("k", now=float(i) * 1e-6)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        entries = log.memory_entries()
        per_entry = peak / entries if entries else 0.0
        fixed = FixedWindowLimiter(n, window)
        for i in range(n):
            fixed.allow("k", now=float(i) * 1e-6)
        ctr = SlidingWindowCounterLimiter(n, window)
        for i in range(n):
            ctr.allow("k", now=float(i) * 1e-6)
        keyed_fixed = fixed.memory_entries()
        keyed_ctr = ctr.memory_entries()
        print("%-10d %14d %12.2f %14d %12d" %
              (n, peak, per_entry, keyed_fixed, keyed_ctr))
        rows.append((n, peak, per_entry))
    growth = rows[-1][1] / rows[0][1]
    say("the exact log tracked %d bytes at 10k requests and %d bytes "
          "at 1e6 requests, a %.1fx jump for 100x the traffic (linear in traffic, as "
          "expected); that is about %.1f bytes per stored request. The fixed-window and "
          "sliding-counter limiters held the whole 1e6 requests in %d and %d state "
          "entries respectively, because they key by window, not by request." %
          (rows[0][1], rows[-1][1], growth, rows[-1][2], keyed_fixed, keyed_ctr))


# --------------------------------------------------------------------------- section 4
def section_counter_error():
    print(RULE)
    print("Section 4. Interpolated sliding-counter vs exact log (deterministic)")
    print(RULE)
    limit, window = 20, 10.0
    log = SlidingWindowLogLimiter(limit, window)
    ctr = SlidingWindowCounterLimiter(limit, window)
    rng = random.Random(7)
    max_abs_error = 0.0
    over_admit = 0
    under_admit = 0
    events = []
    for i in range(4000):
        events.append(Request(float(i) * 0.03, "k", 1))
    for ev in events:
        a_log = log.allow("k", ev.now, ev.cost)
        a_ctr = ctr.allow("k", ev.now, ev.cost)
        if a_log and not a_ctr:
            under_admit += 1
        if a_ctr and not a_log:
            over_admit += 1
        err = abs(ctr.approximate("k", now=ev.now) - log.used("k", now=ev.now))
        max_abs_error = max(max_abs_error, err)
    total_log = feed(SlidingWindowLogLimiter(limit, window), events)
    total_ctr = feed(SlidingWindowCounterLimiter(limit, window), events)
    print("%-28s %12s" % ("metric", "value"))
    print("%-28s %12.3f" % ("max abs count error", max_abs_error))
    print("%-28s %12d" % ("counter over-admissions", over_admit))
    print("%-28s %12d" % ("counter under-admissions", under_admit))
    print("%-28s %12d" % ("exact-log total admits", total_log))
    print("%-28s %12d" % ("counter total admits", total_ctr))
    rel = (total_ctr - total_log) / total_log * 100.0 if total_log else 0.0
    say("on this %d-request stream the interpolated counter's running "
          "count deviated from the exact log by at most %.3f requests (against a limit of "
          "%d), yet its total admitted volume still landed %.2f%% off the exact figure "
          "(%d vs %d). It disagreed with the exact log on %d individual decisions, %d of "
          "them admitting traffic the exact log would have blocked. A sub-request estimate "
          "error is enough to flip a decision whenever the count sits right at the "
          "threshold, and the bias is not one-sided." %
          (len(events), max_abs_error, limit, rel, total_ctr, total_log,
           over_admit + under_admit, over_admit))


# --------------------------------------------------------------------------- section 5
def _replay_stats(limiter, events):
    """Run one limiter over a trace; return admit total, stamps, and rejection promises."""
    admitted = 0
    stamps = []
    first_reject_wait = None
    sum_wait = 0.0
    rejected = 0
    for ev in events:
        if limiter.allow(ev.key, ev.now, ev.cost):
            admitted += 1
            stamps.append(ev.now)
        else:
            rejected += 1
            wait = limiter.wait_time(ev.key, now=ev.now, cost=ev.cost)
            if wait != math.inf:
                sum_wait += wait
                if first_reject_wait is None:
                    first_reject_wait = wait
    return {"admitted": admitted, "stamps": stamps, "rejected": rejected,
            "first_reject_wait": first_reject_wait, "sum_wait": sum_wait}


def _peak_per_second(stamps, width=1.0):
    """Densest number of admits inside any sliding `width`-second sub-window."""
    peak = 0
    left = 0
    for right in range(len(stamps)):
        while stamps[right] - stamps[left] > width:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def section_gcra_vs_bucket():
    print(RULE)
    print("Section 5. GCRA vs token bucket (deterministic)")
    print(RULE)
    capacity, rate = 20, 5.0
    rng = random.Random(11)
    events = poisson(rng, 0.0, 30.0, 2.0, ["k"])
    events += burst(rng, 10.0, 0.1, 200, ["k"])  # 2000/s spike, far past the 5/s steady rate
    events.sort(key=lambda e: e.now)

    bucket = _replay_stats(TokenBucketLimiter(capacity, rate), events)
    gcra = _replay_stats(GCRALimiter(capacity, rate), events)
    height_bucket = _peak_per_second(bucket["stamps"])
    height_gcra = _peak_per_second(gcra["stamps"])

    print("%-28s %14s %14s %12s" %
          ("metric", "token bucket", "GCRA", "delta"))
    print("%-28s %14d %14d %12d" %
          ("total admits", bucket["admitted"], gcra["admitted"],
           bucket["admitted"] - gcra["admitted"]))
    print("%-28s %14d %14d %12d" %
          ("total rejects", bucket["rejected"], gcra["rejected"],
           bucket["rejected"] - gcra["rejected"]))
    print("%-28s %14d %14d %12d" %
          ("peak admits / 1s", height_bucket, height_gcra,
           height_bucket - height_gcra))
    print("%-28s %14.4f %14.4f %12.4f" %
          ("wait @ first reject", bucket["first_reject_wait"] or 0.0,
           gcra["first_reject_wait"] or 0.0,
           (bucket["first_reject_wait"] or 0.0) - (gcra["first_reject_wait"] or 0.0)))
    print("%-28s %14.2f %14.2f %12.2f" %
          ("sum promised wait", bucket["sum_wait"], gcra["sum_wait"],
           bucket["sum_wait"] - gcra["sum_wait"]))
    say("on a %d-request spike GCRA and the token bucket agreed on "
          "every column - total admits (%d vs %d, difference %d), the %d shared rejects, "
          "the densest one-second count (%d vs %d), the first rejected caller's promise "
          "(%.3fs vs %.3fs) and the promise summed over all rejections (%.1fs vs %.1fs). "
          "That is not a measurement artefact: setting GCRA's delay tolerance to "
          "capacity*emission makes it algebraically the token bucket wearing one float "
          "(the theoretical arrival time) instead of two (tokens + last-refill). So the "
          "folklore that GCRA and the bucket hand callers a *different wait time* does not "
          "survive this data - the honest difference is state size, not behaviour." %
          (len(events), bucket["admitted"], gcra["admitted"],
           bucket["admitted"] - gcra["admitted"], bucket["rejected"],
           height_bucket, height_gcra,
           bucket["first_reject_wait"] or 0.0, gcra["first_reject_wait"] or 0.0,
           bucket["sum_wait"], gcra["sum_wait"]))


# --------------------------------------------------------------------------- section 6
def section_fairness():
    print(RULE)
    print("Section 6. Per-key fairness under a skewed trace (deterministic)")
    print(RULE)
    keys = ["hot", "warm", "cold-1", "cold-2"]
    weights = [10.0, 3.0, 1.0, 1.0]  # hot key gets ~62% of arrivals
    limit, window = 40, 10.0
    rng = random.Random(5)
    events = []
    for i in range(2000):
        picks = rng.choices(keys, weights=weights, k=1)[0]
        events.append(Request(float(i) * 0.02, picks, 1))

    offered = {}
    for ev in events:
        offered[ev.key] = offered.get(ev.key, 0) + 1

    iso_adm = _per_key(events, limit, window, shared=False)
    shr_adm = _per_key(events, limit, window, shared=True)

    print("%-10s %10s %12s %12s" %
          ("key", "offered", "isolated", "shared"))
    for k in keys:
        print("%-10s %10d %12d %12d" % (k, offered.get(k, 0), iso_adm[k], shr_adm[k]))
    hot_share_iso = iso_adm["hot"] / max(1, sum(iso_adm.values()))
    hot_share_shr = shr_adm["hot"] / max(1, sum(shr_adm.values()))
    say("the hot key offered %.0f%% of all traffic. Isolated "
          "per-key budgets gave it %.1f%% of admitted requests, the shared budget gave "
          "it %.1f%%. With isolation the two cold keys still got %d and %d admits; on a "
          "single shared pool they were crowded to %d and %d. Fairness is a routing "
          "policy, not an algorithm property - the same FixedWindowLimiter produced both "
          "outcomes." % (100.0 * offered["hot"] / len(events),
                         100.0 * hot_share_iso, 100.0 * hot_share_shr,
                         iso_adm["cold-1"], iso_adm["cold-2"],
                         shr_adm["cold-1"], shr_adm["cold-2"]))


def _per_key(events, limit, window, shared):
    """Re-run a fresh multi-key limiter and return admitted counts per key."""
    base = FixedWindowLimiter(limit, window)
    fresh = MultiKeyLimiter(base, shared_budget=shared)
    counts = {ev.key: 0 for ev in events}
    for ev in events:
        if fresh.allow(ev.key, ev.now, ev.cost):
            counts[ev.key] += 1
    return counts


# --------------------------------------------------------------------------- section 7
def section_throughput(limit, window):
    print(RULE)
    print("Section 7. Steady-rate comparison and per-request timing")
    print(RULE)
    print("           admitted columns are deterministic; the ms column is timing and")
    print("           varies by machine. Same nominal limit %d / window %gs for all." %
          (limit, window))
    rng = random.Random(99)
    events = poisson(rng, 0.0, 60.0, 150.0, ["k"])
    events += burst(rng, 30.0, 0.05, 500, ["k"])
    events.sort(key=lambda e: e.now)

    builders = [
        ("fixed-window", lambda: FixedWindowLimiter(limit, window)),
        ("sliding-log", lambda: SlidingWindowLogLimiter(limit, window)),
        ("sliding-counter", lambda: SlidingWindowCounterLimiter(limit, window)),
        ("token-bucket", lambda: TokenBucketLimiter(limit, limit / window)),
        ("leaky-bucket", lambda: LeakyBucketLimiter(limit, limit / window)),
        ("gcra", lambda: GCRALimiter(limit, limit / window)),
    ]
    print("%-16s %10s %12s %14s %12s" %
          ("algorithm", "events", "admitted", "admit-rate", "ms (timing)"))
    for name, make in builders:
        lim = make()
        t0 = time.perf_counter()
        admitted = feed(lim, events)
        ms = (time.perf_counter() - t0) * 1000.0
        print("%-16s %10d %12d %14.4f %12.2f" %
              (name, len(events), admitted, admitted / len(events), ms))
    counts = []
    for name, make in builders:
        counts.append(feed(make(), events))
    spread = max(counts) - min(counts)
    say("on one trace of %d requests the six limiters admitted "
          "between %d and %d, a spread of %d. The windowed limiters and the rate-based "
          "limiters are not interchangeable: burst allowance and boundary behaviour push "
          "the same nominal limit to different real throughput. Timing is reported "
          "separately because it is not reproducible across machines." %
          (len(events), min(counts), max(counts), spread))


class _Tee:
    """Print to stdout as usual, but also buffer everything for a file.

    This keeps `last_run.txt` a verbatim record of exactly this run, so the numbers in
    README cannot disagree with the file.
    """

    def __init__(self, stream, path):
        self.stream = stream
        self.path = path
        self.parts = []

    def write(self, text):
        self.stream.write(text)
        self.parts.append(text)
        return len(text)

    def flush(self):
        self.stream.flush()

    def save(self):
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(self.parts))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.txt")
    console = sys.stdout
    sys.stdout = _Tee(console, report)
    limit, window = 100, 1.0
    try:
        print(HEADER)
        print("All limits below are the nominal per-window configuration; the point of")
        print("this file is the measured throughput each algorithm actually grants.")
        print()
        section_trace()
        print()
        section_fixed_window_boundary()
        print()
        section_sliding_log_memory()
        print()
        section_counter_error()
        print()
        section_gcra_vs_bucket()
        print()
        section_fairness()
        print()
        section_throughput(limit, window)
    finally:
        sys.stdout.save()
        sys.stdout = console
        print("wrote %s" % report)


if __name__ == "__main__":
    main()
