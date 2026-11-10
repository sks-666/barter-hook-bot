"""Step 3 of the top-down plan: entry triggers on the lower timeframe, and a plain trade simulator to judge them.

Triggers are written for LONGS; a short is the same thing on `Series.mirrored()`. Each returns, for every candle, whether
it fires at THAT candle's close (so the order would fill at the next open). Nothing reads a later candle.

  E1 rsi_cross  RSI(14) crosses up through 40
  E2 bos        a close above the last confirmed swing high (break of structure); once per swing high
  E3 sweep      a wick below the last confirmed swing low that closes back above it; once per swing low
  E4 engulf     a bullish candle whose body engulfs the previous down candle's body
  E5 vol_thrust a bullish candle closing in its top 30% on >= 2x the 20-candle average volume
  E6 sweep_bos  a sweep (E3), then within 12 candles a close above the 6-candle high around the sweep, before price
                closes under the sweep's low

`simulate` trades every signal the same way, so triggers are compared on the entry alone: enter at the next open, stop
1.5 ATR below, target 2R, time exit after 48 candles, taker fees and slippage on every fill, flat funding while held.
"""
import math
from dataclasses import dataclass

from .indicators import atr, pivot_highs, pivot_lows, rsi
from .models import Series

TRIGGERS = ("rsi_cross", "bos", "sweep", "engulf", "vol_thrust", "sweep_bos")
PIVOT_K = 3


@dataclass(frozen=True)
class Costs:
    fee_bps: float = 5.0            # taker fee per fill
    slippage_bps: float = 3.0       # adverse move per market fill (entry, stop, time exit; a limit target has none)
    funding_bps_8h: float = 1.0     # flat, charged for every 8 h held (same conservative rule as swingkit.backtest)


def fire_all(s: Series) -> dict[str, list[bool]]:
    n = len(s)
    r = rsi(s.c, 14)
    out = {k: [False] * n for k in TRIGGERS}
    ph, pl = pivot_highs(s.h, PIVOT_K), pivot_lows(s.l, PIVOT_K)
    a = b = 0                                               # pivots known so far (a pivot at j is known from j + k)
    last_h = last_l = None
    used_h = used_l = True
    pending = None                                          # (expires, ref_high, sweep_low) for E6
    for i in range(n):
        while a < len(ph) and ph[a] + PIVOT_K <= i:
            last_h, used_h = s.h[ph[a]], False
            a += 1
        while b < len(pl) and pl[b] + PIVOT_K <= i:
            last_l, used_l = s.l[pl[b]], False
            b += 1
        if i >= 1 and r[i - 1] < 40 <= r[i]:
            out["rsi_cross"][i] = True
        if last_h is not None and not used_h and s.c[i] > last_h:
            out["bos"][i], used_h = True, True
        if last_l is not None and not used_l and s.l[i] < last_l < s.c[i]:
            out["sweep"][i], used_l = True, True
            pending = (i + 12, max(s.h[max(0, i - 5):i + 1]), s.l[i])
        elif pending is not None and i > 0:
            expires, ref, low = pending
            if s.c[i] < low or i > expires:
                pending = None
            elif s.c[i] > ref:
                out["sweep_bos"][i], pending = True, None
        if i >= 1 and s.c[i - 1] < s.o[i - 1] and s.c[i] > s.o[i] and s.c[i] >= s.o[i - 1] and s.o[i] <= s.c[i - 1]:
            out["engulf"][i] = True
        if i >= 20 and s.c[i] > s.o[i] and s.v[i] >= 2 * (sum(s.v[i - 20:i]) / 20) \
                and s.c[i] - s.l[i] >= 0.7 * (s.h[i] - s.l[i]) and s.h[i] > s.l[i]:
            out["vol_thrust"][i] = True
    return out


@dataclass(frozen=True)
class Result:
    entry_i: int
    exit_i: int
    reason: str                     # "stop" | "target" | "time"
    r: float                        # net result in R (initial risk) after all costs
    bps: float                      # net result in basis points of the entry price


def simulate(s: Series, a: list[float], i: int, costs: Costs = Costs(), stop_atr: float = 1.5, rr: float = 2.0,
             max_hold: int = 48) -> Result | None:
    """A long decided at the close of candle i. On a mirrored series this is a short; prices are only ever compared as
    ratios of |entry|, so the sign does not matter."""
    if i + 1 >= len(s) or math.isnan(a[i]):
        return None
    slip, fee = costs.slippage_bps / 1e4, costs.fee_bps / 1e4
    ent = s.o[i + 1] + slip * abs(s.o[i + 1])
    risk = stop_atr * a[i]
    stop, target = ent - risk, ent + rr * risk
    last = min(len(s) - 1, i + max_hold)
    for j in range(i + 1, last + 1):
        if s.l[j] <= stop:                                   # both in one candle: the stop is assumed to hit first
            px, why = min(stop, s.o[j]) - slip * abs(stop), "stop"      # a gap through the stop fills at the open
            break
        if s.h[j] >= target:
            px, why = target, "target"
            break
    else:
        j, px, why = last, s.c[last] - slip * abs(s.c[last]), "time"
        if i + max_hold > len(s) - 1:
            return None                                      # the data ended before the trade did
    held = j - i
    net = (px - ent) / abs(ent) - 2 * fee - costs.funding_bps_8h / 1e4 * (held / 8.0)
    return Result(i + 1, j, why, net / (risk / abs(ent)), net * 1e4)
