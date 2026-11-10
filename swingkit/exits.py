"""Step 4 of the top-down plan: where a trade is proven wrong (stop) and where it is closed in profit (target).

A long decided at the close of candle i is entered at the next open (or `delay` candles later) and managed candle by
candle. On `Series.mirrored()` the same code trades a short. Stops:

  ("atr", n)   n x ATR(14) below the entry
  ("swing",)   below the last CONFIRMED swing low (+ a 0.25 ATR buffer), kept between 1 and 4 ATR from the entry

Targets / management:

  ("rr", r)       fixed target r times the initial risk
  ("partial", r)  half the position at +1R, the stop moves to break-even, the rest runs to r R
  ("trail",)      no target: the stop follows each newly confirmed swing low upward
  ("ema",)        no target: leave at the next open after a close under the 1h EMA50

Every spec also has a time exit after `hold` candles. Intrabar rules are conservative: if a candle could have hit the
stop and the target, the stop is taken; a gap through the stop fills at the open; a scale-out candle that also falls back
to break-even is counted as stopped at break-even. Fees and slippage as in `swingkit.triggers`; a limit fill (a target)
pays `maker_fee_bps` and no slippage.
"""
import math
from dataclasses import dataclass

from .indicators import ema, pivot_lows
from .models import Series
from .triggers import Result

NAN = math.nan
PIVOT_K = 3


@dataclass(frozen=True)
class Spec:
    stop: tuple
    target: tuple
    hold: int = 48


@dataclass(frozen=True)
class XCosts:
    taker_fee_bps: float = 5.0
    maker_fee_bps: float = 5.0      # what a resting limit order (a target) pays; 5.0 = the same as step 3
    slippage_bps: float = 3.0
    funding_bps_8h: float = 1.0


@dataclass(frozen=True)
class Context:
    ema50: list[float]
    last_pl: list[float]            # at each candle: the newest swing low that is already confirmed (NaN before the first)


def context(s: Series) -> Context:
    last, out, p, cur = pivot_lows(s.l, PIVOT_K), [NAN] * len(s), 0, NAN
    for i in range(len(s)):
        while p < len(last) and last[p] + PIVOT_K <= i:
            cur, p = s.l[last[p]], p + 1
        out[i] = cur
    return Context(ema(s.c, 50), out)


def run(s: Series, a: list[float], ctx: Context, i: int, spec: Spec, costs: XCosts = XCosts(),
        delay: int = 0) -> Result | None:
    e = i + 1 + delay
    last = e + spec.hold - 1
    if last > len(s) - 1 or math.isnan(a[i]):
        return None                                          # the data ends before the trade could
    slip, taker, maker = costs.slippage_bps / 1e4, costs.taker_fee_bps / 1e4, costs.maker_fee_bps / 1e4
    ent = s.o[e] + slip * abs(s.o[e])
    if spec.stop[0] == "atr":
        risk = spec.stop[1] * a[i]
    else:
        lvl = ctx.last_pl[i]
        risk = ent - (lvl - 0.25 * a[i]) if not math.isnan(lvl) else 1.5 * a[i]
        risk = min(max(risk, 1.0 * a[i]), 4.0 * a[i])
    stop = ent - risk
    kind = spec.target[0]
    final = ent + spec.target[1] * risk if kind in ("rr", "partial") else None
    half = ent + risk
    legs: list[tuple[float, float, bool, int]] = []          # (share of the position, exit price, limit order?, candle)
    scaled, why, left = False, "time", 1.0
    for j in range(e, last + 1):
        if s.l[j] <= stop:                                    # the stop first; a gap fills at the open
            px = min(stop, s.o[j])
            legs.append((left, px - slip * abs(px), False, j))
            why = "stop"
            break
        if kind == "partial" and not scaled and s.h[j] >= half:
            legs.append((0.5, half, True, j))
            left, scaled, stop = 0.5, True, max(stop, ent)
            if s.l[j] <= ent:                                 # it may have fallen back the same candle: assume it did
                legs.append((0.5, ent - slip * abs(ent), False, j))
                why = "stop"
                break
            continue
        if final is not None and s.h[j] >= final:
            legs.append((left, final, True, j))
            why = "target"
            break
        if kind == "trail" and not math.isnan(ctx.last_pl[j]):
            ns = ctx.last_pl[j] - 0.25 * a[j]
            if stop < ns < s.c[j]:
                stop = ns
        if kind == "ema" and j < last and s.c[j] < ctx.ema50[j]:
            px = s.o[j + 1]
            legs.append((left, px - slip * abs(px), False, j + 1))
            why = "ema"
            break
    else:
        legs.append((left, s.c[last] - slip * abs(s.c[last]), False, last))
    net = 0.0
    for share, px, limit, j in legs:
        net += share * ((px - ent) / abs(ent) - taker - (maker if limit else taker)
                        - costs.funding_bps_8h / 1e4 * ((j - i) / 8.0))
    if why == "stop" and scaled:
        why = "break_even"
    return Result(e, legs[-1][3], why, net / (risk / abs(ent)), net * 1e4)
