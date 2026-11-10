"""Step 2 of the top-down plan: structural zones, and what happens when price comes back to them.

Everything is written for SUPPORT zones (price comes down onto a zone from above). A RESISTANCE zone is the same thing
on `Series.mirrored()`, exactly as the strategies do for shorts.

A zone is a price band `[lo, hi]` that is KNOWN from candle `born` on (never earlier: a swing low is only confirmed
`k` candles after it formed). Kinds:

  swing_sr     two confirmed swing lows within 0.75 ATR of each other; the band spans both
  range20/55   the lowest low of the previous 20 / 55 days (moves every candle), widened by 0.25 ATR
  order_block  the last down candle before a strong up-move (body >= 1.5 ATR that closes above the last 5 highs)
  fvg          fair value gap: candle i's low is above candle i-2's high by >= 0.3 ATR; the band is the gap
  ma50/ma200   a 4h EMA +- 0.5 ATR (moves every candle)

A TOUCH is a candle that comes down into the band from above (previous close above it), dips to the band's top and still
closes inside or above it. A static zone dies when a candle closes more than 0.5 ATR below it, or after MAX_AGE candles.
After a touch the zone is re-armed once price has gone a full ATR above it.

`outcome` answers the one question the study asks: after a touch, does price go 1 ATR UP or 1 ATR DOWN first?
"""
import math
from dataclasses import dataclass

from .indicators import ema, lowest_prev, pivot_lows
from .models import Series

HORIZON = 30            # candles (5 days of 4h) to wait for the +/- 1 ATR move
MAX_AGE = 180           # a static zone is forgotten after 30 days of 4h candles
BREAK_BUF = 0.5         # a close this many ATR below a static zone kills it
REARM = 1.0             # price must get this many ATR above the zone before the next touch counts
KINDS = ("swing_sr", "range20", "range55", "order_block", "fvg", "ma50", "ma200")
DYNAMIC = {"range20", "range55", "ma50", "ma200"}


@dataclass(frozen=True)
class Zone:
    kind: str
    born: int                       # first candle at which the zone is known
    lo: float | tuple               # a number, or one value per candle for a moving band
    hi: float | tuple
    dynamic: bool = False

    def band(self, i: int) -> tuple[float, float]:
        return (self.lo[i], self.hi[i]) if self.dynamic else (self.lo, self.hi)


@dataclass(frozen=True)
class Touch:
    kind: str
    born: int
    i: int                          # the touch candle (decided at its close)
    n: int                          # 1 = first touch of this zone, 2 = second, ...


def pivot_zones(s: Series, a: list[float], k: int = 5, lookback: int = 400, tol: float = 0.75) -> list[Zone]:
    pl, out = pivot_lows(s.l, k), []
    for bi, b in enumerate(pl):
        born = b + k                                         # a pivot is only known k candles after it formed
        if born >= len(s) or math.isnan(a[born]):
            continue
        for prev in reversed(pl[:bi]):
            if b - prev > lookback:
                break
            if abs(s.l[prev] - s.l[b]) <= tol * a[born]:
                lo = min(s.l[prev], s.l[b])
                out.append(Zone("swing_sr", born, lo, lo + max(abs(s.l[prev] - s.l[b]), 0.2 * a[born])))
                break
    return out


def _moving(kind: str, s: Series, lo: list[float], hi: list[float]) -> list[Zone]:
    born = next((i for i in range(len(s)) if not math.isnan(lo[i]) and not math.isnan(hi[i])), None)
    return [] if born is None else [Zone(kind, born, tuple(lo), tuple(hi), dynamic=True)]


def range_zones(s: Series, a: list[float], n: int, kind: str) -> list[Zone]:
    lo = lowest_prev(s.l, n)
    return _moving(kind, s, lo, [x + 0.25 * y for x, y in zip(lo, a)])


def ma_zones(s: Series, a: list[float], n: int, kind: str) -> list[Zone]:
    e = ema(s.c, n)
    return _moving(kind, s, [x - 0.5 * y for x, y in zip(e, a)], [x + 0.5 * y for x, y in zip(e, a)])


def order_block_zones(s: Series, a: list[float]) -> list[Zone]:
    out = []
    for i in range(6, len(s)):
        A = a[i - 1]
        if math.isnan(A) or s.c[i] - s.o[i] < 1.5 * A or s.c[i] <= max(s.h[i - 5:i]):
            continue
        for j in (i - 1, i - 2, i - 3):                      # the most recent down candle before the move
            if s.c[j] < s.o[j]:
                out.append(Zone("order_block", i, s.l[j], s.h[j]))
                break
    return out


def fvg_zones(s: Series, a: list[float]) -> list[Zone]:
    return [Zone("fvg", i, s.h[i - 2], s.l[i]) for i in range(2, len(s))
            if not math.isnan(a[i]) and s.l[i] - s.h[i - 2] >= 0.3 * a[i]]


def build(s: Series, a: list[float], kinds=KINDS) -> list[Zone]:
    makers = {"swing_sr": lambda: pivot_zones(s, a), "range20": lambda: range_zones(s, a, 120, "range20"),
              "range55": lambda: range_zones(s, a, 330, "range55"), "order_block": lambda: order_block_zones(s, a),
              "fvg": lambda: fvg_zones(s, a), "ma50": lambda: ma_zones(s, a, 50, "ma50"),
              "ma200": lambda: ma_zones(s, a, 200, "ma200")}
    return [z for k in kinds for z in makers[k]()]


def touches(s: Series, a: list[float], z: Zone) -> list[Touch]:
    """Every touch of the zone, oldest first. Each decision at candle i reads candles <= i only."""
    out: list[Touch] = []
    lo0, hi0 = z.band(z.born)
    if z.dynamic:
        armed, end = s.c[z.born] > hi0, len(s) - 1
    else:
        if not s.c[z.born] > hi0:
            return out                                       # not a support: price was not above it when it appeared
        armed, end = True, min(len(s) - 1, z.born + MAX_AGE)
    n = 0
    for i in range(z.born + 1, end + 1):
        lo, hi = z.band(i)
        if math.isnan(lo) or math.isnan(hi) or math.isnan(a[i]):
            continue
        if not z.dynamic and s.c[i] < lo - BREAK_BUF * a[i]:
            break                                            # broken: the zone is gone
        if armed:
            if s.l[i] <= hi and s.c[i] >= lo and s.c[i - 1] > hi:
                n = 1 if z.dynamic else n + 1                # a moving band has no "first" touch: each re-armed visit is new
                out.append(Touch(z.kind, z.born, i, n))
                armed = False
        elif s.h[i] >= hi + REARM * a[i]:
            armed = True
    return out


def outcome(s: Series, a: list[float], i: int, horizon: int = HORIZON) -> str | None:
    """From the touch candle's close: "up" if price gets +1 ATR first, "down" if -1 ATR first (a candle that reaches
    both counts as down: the conservative reading), "none" if neither within `horizon` candles, None if the data ends
    before the question is settled."""
    p0, step = s.c[i], a[i]
    up, dn = p0 + step, p0 - step
    for j in range(i + 1, min(len(s), i + 1 + horizon)):
        if s.l[j] <= dn:
            return "down"
        if s.h[j] >= up:
            return "up"
    return "none" if i + horizon < len(s) else None


def active_bands(s: Series, a: list[float], zs: list[Zone]) -> list[list[tuple[float, float]]]:
    """For every candle j: the bands of all support zones that are alive and KNOWN at j's close (born <= j, not broken by
    a close at or before j, not aged out). Used to ask "is price at a zone right now?" without ever seeing the future."""
    out: list[list[tuple[float, float]]] = [[] for _ in range(len(s))]
    for z in zs:
        if z.dynamic:
            for j in range(z.born, len(s)):
                lo, hi = z.band(j)
                if not (math.isnan(lo) or math.isnan(hi)):
                    out[j].append((lo, hi))
        elif s.c[z.born] > z.hi:
            for j in range(z.born, min(len(s), z.born + MAX_AGE + 1)):
                if j > z.born and (math.isnan(a[j]) or s.c[j] < z.lo - BREAK_BUF * a[j]):
                    break
                out[j].append((z.lo, z.hi))
    return out
