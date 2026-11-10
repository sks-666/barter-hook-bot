"""Step 1 of the top-down plan: name the higher-timeframe trend with a number we can test.

Every function takes a DAILY Series and returns one label per candle, decided at that candle's close: "up", "down",
"range", or "" while the indicators are still warming up. Nothing reads a later candle (tests prove it).

  T1  close vs EMA200                      up / down only
  T2  EMA50 vs EMA200, with EMA200 sloping up / down / range
  T3  market structure: confirmed swing highs and lows both rising / both falling / mixed
  T4  T2 and the weekly close vs weekly EMA20 agree, else range
  T5  T2, but range when ADX(14) is under 20
"""
import math

from .indicators import adx, ema, pivot_highs, pivot_lows
from .models import Series
from .mtf import BAR_MS, align, resample

RULES = ("T1", "T2", "T3", "T4", "T5")
SLOPE_BARS = 10


def t1(s: Series) -> list[str]:
    e = ema(s.c, 200)
    return ["" if math.isnan(e[i]) else ("up" if s.c[i] > e[i] else "down") for i in range(len(s))]


def t2(s: Series) -> list[str]:
    f, sl = ema(s.c, 50), ema(s.c, 200)
    out = []
    for i in range(len(s)):
        if i < SLOPE_BARS or math.isnan(sl[i - SLOPE_BARS]) or math.isnan(f[i]):
            out.append("")
        elif f[i] > sl[i] and sl[i] > sl[i - SLOPE_BARS]:
            out.append("up")
        elif f[i] < sl[i] and sl[i] < sl[i - SLOPE_BARS]:
            out.append("down")
        else:
            out.append("range")
    return out


def t3(s: Series, k: int = 5) -> list[str]:
    """A pivot at j is known only from candle j + k on, so the label at i uses pivots with j + k <= i."""
    ph, pl = pivot_highs(s.h, k), pivot_lows(s.l, k)
    out, a, b = [], 0, 0
    for i in range(len(s)):
        while a < len(ph) and ph[a] + k <= i:
            a += 1
        while b < len(pl) and pl[b] + k <= i:
            b += 1
        if a < 2 or b < 2:
            out.append("")
            continue
        hh, hl = s.h[ph[a - 1]] > s.h[ph[a - 2]], s.l[pl[b - 1]] > s.l[pl[b - 2]]
        out.append("up" if hh and hl else "down" if not hh and not hl else "range")
    return out


def t4(s: Series, base_ms: int = BAR_MS["1d"]) -> list[str]:
    base, w = t2(s), resample(s, "1w", base_ms)
    we = ema(w.c, 20)
    idx = align(w.ts, BAR_MS["1w"], s.ts, base_ms)
    out = []
    for i, lab in enumerate(base):
        j = idx[i]
        if not lab or j < 0 or math.isnan(we[j]):
            out.append("")
        elif lab == "up" and w.c[j] > we[j]:
            out.append("up")
        elif lab == "down" and w.c[j] < we[j]:
            out.append("down")
        else:
            out.append("range")
    return out


def t5(s: Series) -> list[str]:
    a = adx(s.h, s.l, s.c, 14)
    return [lab if lab and not math.isnan(a[i]) and a[i] >= 20 else ("range" if lab and not math.isnan(a[i]) else "")
            for i, lab in enumerate(t2(s))]


def label(rule: str, s: Series) -> list[str]:
    return {"T1": t1, "T2": t2, "T3": t3, "T4": t4, "T5": t5}[rule](s)
