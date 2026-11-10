"""Multi-timeframe helpers with no look-ahead.

A higher-timeframe (HTF) candle is only usable once it has CLOSED. `align` gives, for every lower-timeframe (LTF)
candle, the index of the newest HTF candle that was already closed when that LTF candle closed, so a rule can read
"the daily trend" at any 4h candle without ever seeing a daily candle that is still forming.
"""
from .models import Series

DAY_MS = 86_400_000
BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": DAY_MS, "1w": 7 * DAY_MS}
WEEK_ANCHOR_MS = 4 * DAY_MS           # 1970-01-01 was a Thursday; +4 days = the first Monday 00:00 UTC (Binance's week)


def resample(s: Series, interval: str, base_ms: int) -> Series:
    """Daily (or any finer) candles -> weekly (or coarser). Only COMPLETE buckets are kept: a bucket missing any of its
    base candles, or still forming, is dropped."""
    bar = BAR_MS[interval]
    per = bar // base_ms
    anchor = WEEK_ANCHOR_MS if interval == "1w" else 0
    buckets: dict[int, list[int]] = {}
    for i, t in enumerate(s.ts):
        buckets.setdefault((t - anchor) // bar, []).append(i)
    out = Series([], [], [], [], [], [])
    for key in sorted(buckets):
        idx = buckets[key]
        if len(idx) != per or s.ts[idx[-1]] - s.ts[idx[0]] != (per - 1) * base_ms:
            continue
        out.ts.append(key * bar + anchor)
        out.o.append(s.o[idx[0]])
        out.h.append(max(s.h[i] for i in idx))
        out.l.append(min(s.l[i] for i in idx))
        out.c.append(s.c[idx[-1]])
        out.v.append(sum(s.v[i] for i in idx))
    return out


def align(htf_ts: list[int], htf_ms: int, ltf_ts: list[int], ltf_ms: int) -> list[int]:
    """For each LTF candle i: the index of the newest HTF candle with open + htf_ms <= ltf open + ltf_ms (closed by the
    time candle i closed), or -1 if there is none yet."""
    out, j = [], -1
    for t in ltf_ts:
        known_until = t + ltf_ms
        while j + 1 < len(htf_ts) and htf_ts[j + 1] + htf_ms <= known_until:
            j += 1
        out.append(j)
    return out
