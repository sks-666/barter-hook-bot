"""The quality check on top of the combined signal (arbbot/signals.py): TRADE or NO_TRADE, and why.

The combined score says which way the flow leans. This layer asks whether the chart, the derivatives market and the
clock back a trade that way right now, and places the stop and targets on market structure. A marginal or choppy setup
is NO_TRADE. It runs on three candle sets picked from the timeframe (LTF trigger / MID / HTF context, see intervals()):

1. Regime (HTF): TRENDING_BULL / TRENDING_BEAR (ADX >= 25, EMA 21 over/under EMA 55, price on that side),
   VOLATILE_CHOP (true range well above its own average with no trend, or a strong ADX with the EMAs mixed: never
   traded), RANGING_MEAN_REVERSION (ADX < 25).
2. Four pillars, 0-25 points each (missing data scores half, so one dead source does not veto everything):
   a) structure & liquidity: break of structure (BOS), liquidity sweep of a swing, fair value gap retest
   b) momentum: RSI, MACD histogram, EMA 8/21/55 ribbon, HTF alignment, RSI divergence
   c) volume & volatility: RVOL of the trigger candle, ATR contraction before it, VWAP side, taker buy share
   d) derivatives & cross-market: open interest delta, funding, futures premium trend, BTC (or for BTC: S&P / dollar)
3. Guardrails learned from losing trades (any one = NO_TRADE, except the session one which costs 20% confidence):
   [REGIME]   no mean-reversion trade when ADX > 30 on the MID or HTF candles without a BOS its way
   [FAKEOUT]  no breakout when the trigger candle's RVOL is under 1.5x its 20-candle average
   [FUNDING]  no long when funding > 0.03% per 8h while spot volume declines (and the mirror for shorts)
   [SESSION]  confidence x0.8 in the low-liquidity hours (weekends, 21:00-24:00 UTC) or within 15 minutes of a
              high-impact US economic release
4. Invalidation & R:R: the stop goes past the last swing (at least 1 LTF ATR away) plus 0.25 ATR, and is pushed off
   round numbers. TP1 is the first unbroken structure level at least 1.8R away (else 1.8R), TP2 the runner at 3R or
   more. An unbroken HTF swing level against the trade closer than 1.5R (or 1.8R for TP1) = NO_TRADE.
5. Pre-mortem: the most likely reason this trade hits its stop. Active and severe (trading against the HTF trend or
   against BTC) = NO_TRADE; active and milder (exhausted momentum, crowded funding, stop inside one MID candle's
   range) = confidence -15.

6. Signal type (arbbot/signal_types.py): the idea is classified (funding squeeze, sweep reversal, breakout, trend
   pullback, mean reversion, or one of your custom types) and that type sets its own entry style, minimum stop
   distance, TP1 / TP2, extra required conditions and quality bar.

Every function here is pure (candles in, numbers out) so it can be tested without Binance. Nothing has been
backtested: these are rules, not a forecast.
"""
import math
import time

from . import signal_types as st
from .market import INTERVALS

STEP_S = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400, "1w": 604_800}
MIN_QUALITY = 55             # pillar points (0-100) needed for a TRADE
ADX_TREND = 25.0             # HTF ADX at or above this with aligned EMAs: a trend
ADX_STRONG = 30.0            # [REGIME] mean reversion is refused above this without a BOS
CHOP_VOL = 1.4               # recent true range this many times its 100-candle average, no trend: VOLATILE_CHOP
RVOL_BREAKOUT = 1.5          # [FAKEOUT] trigger candle volume vs its 20-candle average
FUNDING_EXTREME = 0.0003     # [FUNDING] 0.03% per funding interval
SPOT_DECLINE = 0.85          # spot volume of the last 6 MID candles under this share of the 6 before: declining
SESSION_PENALTY = 0.8        # [SESSION] confidence multiplier
DEAD_HOUR_UTC = 21           # 21:00-24:00 UTC (US closed, Asia not yet open) and weekends: thin books
NEWS_WINDOW_S = 900          # within 15 minutes of a high-impact release
STOP_BUFFER_ATR = 0.25       # the stop sits this many LTF ATRs past the swing (and off round numbers)
MIN_STOP_ATR = 1.0           # ...and at least this many LTF ATRs from the entry
MIN_STOP_PCT = 0.003
MAX_STOP_PCT = 0.05          # a structural stop further than this: NO_TRADE (never pulled inside the structure)
TP1_R, TP2_R, TP2_MAX_R, HTF_CAP_R = 1.8, 3.0, 5.0, 1.5
HOLD_X = 2.0                 # the auto-trader closes a trade after this many timeframes (between 30 min and 7 days)
REACH_X = 1.25               # TP1 at most this x LTF ATR x sqrt(LTF candles in the hold) away: else it cannot be
                             # reached before the time exit closes the trade (a slow loser instead of a winner)


def hold_s(window_s: float) -> float:
    """How long the auto-trader holds a trade of this timeframe before closing it."""
    return max(1800.0, min(7 * 86_400.0, window_s * HOLD_X))
PREMORTEM_DOWNGRADE = 15
FRACTAL = 2                  # a swing high/low: higher/lower than the 2 candles on each side
REGIMES = ("TRENDING_BULL", "TRENDING_BEAR", "RANGING_MEAN_REVERSION", "VOLATILE_CHOP")
PILLARS = (("structure", "Structure & liquidity"), ("momentum", "Momentum & divergence"),
           ("volume", "Volume & volatility"), ("derivs", "Derivatives & cross-market"))


# ---- candles -> indicators ----------------------------------------------------------------------------------------
# a kline row: [open ms, open, high, low, close, base volume, quote volume, taker-buy quote volume]
def closed(kl: list) -> list:
    """The candles that have closed: Binance's last row is still forming."""
    return kl[:-1] if len(kl) > 1 else list(kl)


def ema(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k, out = 2 / (n + 1), [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def rma(vals: list[float], n: int) -> list[float]:
    """Wilder's smoothing (RSI, ATR, ADX)."""
    if len(vals) < n:
        return []
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append((out[-1] * (n - 1) + v) / n)
    return out


def true_ranges(kl: list) -> list[float]:
    return [max(h - l, abs(h - pc), abs(l - pc)) for (_, _, h, l, *_), (_, _, _, _, pc, *_) in zip(kl[1:], kl[:-1])]


def atr(kl: list, n: int = 14) -> float | None:
    s = rma(true_ranges(kl), n)
    return s[-1] if s else None


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """RSI aligned with closes (None until there is enough data)."""
    if len(closes) <= n:
        return [None] * len(closes)
    d = [b - a for a, b in zip(closes[:-1], closes[1:])]
    up, dn = rma([max(x, 0.0) for x in d], n), rma([max(-x, 0.0) for x in d], n)
    vals = [100.0 if b == 0 else 100 - 100 / (1 + a / b) for a, b in zip(up, dn)]
    return [None] * (len(closes) - len(vals)) + vals


def macd_hist(closes: list[float]) -> list[float]:
    line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    return [a - b for a, b in zip(line, ema(line, 9))]


def adx(kl: list, n: int = 14) -> float | None:
    if len(kl) < 3 * n:
        return None
    pdm, mdm = [], []
    for (_, _, h, l, *_), (_, _, ph, pl, *_) in zip(kl[1:], kl[:-1]):
        up, down = h - ph, pl - l
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    tr, p, m = rma(true_ranges(kl), n), rma(pdm, n), rma(mdm, n)
    dx = []
    for t, a, b in zip(tr, p, m):
        if t <= 0:
            dx.append(0.0)
            continue
        dp, dm = 100 * a / t, 100 * b / t
        dx.append(0.0 if dp + dm == 0 else 100 * abs(dp - dm) / (dp + dm))
    s = rma(dx, n)
    return s[-1] if s else None


def trend_dir(kl: list) -> int:
    """+1 up (EMA 21 over EMA 55, price over EMA 55, EMA 21 rising), -1 the mirror, 0 neither."""
    c = [k[4] for k in kl]
    if len(c) < 60:
        return 0
    e21, e55 = ema(c, 21), ema(c, 55)
    slope = e21[-1] - e21[-4]
    if e21[-1] > e55[-1] and c[-1] > e55[-1] and slope > 0:
        return 1
    if e21[-1] < e55[-1] and c[-1] < e55[-1] and slope < 0:
        return -1
    return 0


def swings(kl: list, k: int = FRACTAL) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed swing highs and lows as (index, price), oldest first."""
    highs, lows = [], []
    for i in range(k, len(kl) - k):
        h, l = kl[i][2], kl[i][3]
        if all(h > kl[j][2] for j in range(i - k, i)) and all(h >= kl[j][2] for j in range(i + 1, i + k + 1)):
            highs.append((i, h))
        if all(l < kl[j][3] for j in range(i - k, i)) and all(l <= kl[j][3] for j in range(i + 1, i + k + 1)):
            lows.append((i, l))
    return highs, lows


def unbroken(kl: list, long: bool, beyond: float) -> list[float]:
    """Swing levels the trade runs into that price has not traded through since they formed, nearest first:
    swing highs above `beyond` for a long, swing lows below it for a short."""
    highs, lows = swings(kl)
    out = []
    for i, p in (highs if long else lows):
        later = kl[i + 1:]
        if long and p > beyond and all(x[2] <= p for x in later):
            out.append(p)
        elif not long and p < beyond and all(x[3] >= p for x in later):
            out.append(p)
    return sorted(out, reverse=not long)


def bos(kl: list, side: int, lookback: int = 5) -> float | None:
    """Break of structure: one of the last `lookback` candles closed past the latest swing high (long) / low (short)
    confirmed before it. The broken level, or None."""
    highs, lows = swings(kl)
    for j in range(max(len(kl) - lookback, 1), len(kl)):
        prior = [p for i, p in (highs if side > 0 else lows) if i + FRACTAL < j]
        if prior and (kl[j][4] > prior[-1] if side > 0 else kl[j][4] < prior[-1]):
            return prior[-1]
    return None


def sweep(kl: list, side: int, lookback: int = 10) -> float | None:
    """Liquidity sweep: a recent candle wicked past the latest swing low (long) / high (short) and closed back
    inside, taking the stops resting there. The swept level, or None."""
    highs, lows = swings(kl)
    for j in range(max(len(kl) - lookback, 1), len(kl)):
        prior = [p for i, p in (lows if side > 0 else highs) if i + FRACTAL < j]
        if not prior:
            continue
        lvl = prior[-1]
        if side > 0 and kl[j][3] < lvl < kl[j][4] or side < 0 and kl[j][2] > lvl > kl[j][4]:
            return lvl
    return None


def fvg_retest(kl: list, side: int, price: float, a: float, lookback: int = 30) -> tuple[float, float] | None:
    """A fair value gap of the trade's direction in the last `lookback` candles that price is back in (within 0.25
    ATR): bullish = a candle's low above the high two candles earlier. The gap (low, high), or None."""
    for i in range(len(kl) - 1, max(len(kl) - lookback, 2) - 1, -1):
        lo, hi = (kl[i - 2][2], kl[i][3]) if side > 0 else (kl[i][2], kl[i - 2][3])
        if hi > lo and lo - 0.25 * a <= price <= hi + 0.25 * a:
            return lo, hi
    return None


def divergence(kl: list, r: list) -> int:
    """+1 bullish (price lower low, RSI higher low), -1 bearish (price higher high, RSI lower high), 0 none: the
    last two swings of the last 40 candles."""
    highs, lows = swings(kl)
    lows = [(i, p) for i, p in lows if i >= len(kl) - 40 and r[i] is not None]
    highs = [(i, p) for i, p in highs if i >= len(kl) - 40 and r[i] is not None]
    if len(lows) >= 2 and lows[-1][1] < lows[-2][1] and r[lows[-1][0]] > r[lows[-2][0]]:
        return 1
    if len(highs) >= 2 and highs[-1][1] > highs[-2][1] and r[highs[-1][0]] < r[highs[-2][0]]:
        return -1
    return 0


# ---- candlestick patterns ------------------------------------------------------------------------------------------
# Crypto trades 24/7, so the classic gap rules (piercing line, the star's gaps) cannot apply: the patterns below are
# the gap-free forms. Sizes are measured in LTF ATRs so a pattern of tiny candles in a dead market does not count.
# A REVERSAL pattern only counts as a signal AT A LEVEL (its extreme within 0.5 ATR of the 20-candle low / high or of a
# recent swing): a hammer in the middle of nowhere is a coin flip. CONTINUATION patterns need no level.
CANDLE_FRESH = 2             # the pattern completed on one of the last this-many closed candles
CANDLES = {                  # key -> (long name, short name, kind)
    "engulfing": ("bullish engulfing", "bearish engulfing", "reversal"),
    "pin_bar": ("hammer (pin bar)", "shooting star (pin bar)", "reversal"),
    "star": ("morning star", "evening star", "reversal"),
    "three_soldiers": ("three white soldiers", "three black crows", "continuation"),
    "inside_break": ("inside-bar breakout", "inside-bar breakdown", "continuation"),
}


def _parts(k: list) -> tuple[float, float, float, float]:
    """(signed body, range, upper wick, lower wick) of one candle."""
    o, h, l, c = k[1], k[2], k[3], k[4]
    return c - o, h - l, h - max(o, c), min(o, c) - l


def _pattern_at(kl: list, j: int, side: int, a: float) -> str | None:
    """The pattern (a CANDLES key) that completes on candle j for this side (+1 bullish, -1 bearish), or None."""
    b, rng, up, lo = _parts(kl[j])
    near, far = (lo, up) if side > 0 else (up, lo)                    # the wick toward the reversal / away from it
    # three white soldiers / black crows: 3 solid candles its way, each closing further, opening inside the last body
    if j >= 2:
        three = [kl[j - 2], kl[j - 1], kl[j]]
        if all(_parts(k)[0] * side >= 0.5 * a for k in three) and \
                all((three[i][4] - three[i - 1][4]) * side > 0 for i in (1, 2)) and \
                all(min(three[i - 1][1], three[i - 1][4]) <= three[i][1] <= max(three[i - 1][1], three[i - 1][4])
                    for i in (1, 2)) and \
                all((_parts(k)[2] if side > 0 else _parts(k)[3]) <= 0.35 * _parts(k)[1] for k in three):
            return "three_soldiers"
    # morning / evening star: a big candle against, a small one, then one its way closing past the first one's middle
    if j >= 2:
        b1, b2 = _parts(kl[j - 2])[0], _parts(kl[j - 1])[0]
        if -b1 * side >= 0.8 * a and abs(b2) <= 0.3 * abs(b1) and b * side >= 0.5 * a and \
                (kl[j][4] - (kl[j - 2][1] + kl[j - 2][4]) / 2) * side > 0:
            return "star"
    # engulfing: the last candle against, this one its way with a strictly bigger body that covers it, of some size
    if j >= 1:
        pb = _parts(kl[j - 1])[0]
        po, pc = kl[j - 1][1], kl[j - 1][4]
        if pb * side < 0 and b * side > abs(pb) and b * side >= 0.3 * a and \
                (kl[j][1] - pc) * side <= 0 and (kl[j][4] - po) * side >= 0:
            return "engulfing"
    # hammer / shooting star: a long wick toward the reversal (rejection), a small body, almost no wick the other way
    if rng >= 0.8 * a and near >= 2 * abs(b) and near >= 0.55 * rng and far <= 0.25 * rng:
        return "pin_bar"
    # inside-bar breakout: a candle inside the one before it, then a close past that mother candle
    if j >= 2:
        m, ins = kl[j - 2], kl[j - 1]
        if ins[2] <= m[2] and ins[3] >= m[3] and (kl[j][4] > m[2] if side > 0 else kl[j][4] < m[3]):
            return "inside_break"
    return None


def candle_pattern(kl: list, side: int, a: float) -> dict | None:
    """The freshest candlestick pattern for this side on the last CANDLE_FRESH closed candles, or None:
    {key, name, kind, ago (0 = the last candle), extreme (the pattern's low for a long / high for a short: the stop
    belongs past it), at_level}. A pattern whose extreme has since been closed through no longer counts."""
    n = len(kl)
    for j in range(n - 1, max(n - 1 - CANDLE_FRESH, 1), -1):
        key = _pattern_at(kl, j, side, a)
        if key is None:
            continue
        span = kl[j - (2 if key in ("three_soldiers", "star", "inside_break") else 1 if key == "engulfing" else 0): j + 1]
        extreme = min(k[3] for k in span) if side > 0 else max(k[2] for k in span)
        if any((k[4] - extreme) * side < 0 for k in kl[j + 1:]):     # closed through since: failed
            return None
        before = kl[max(0, j - 20 - len(span) + 1): j - len(span) + 1]
        _, lows = swings(kl[: j + 1])
        highs, _ = swings(kl[: j + 1])
        levels_ = [p for i, p in (lows if side > 0 else highs) if i >= j - 50][-6:]
        if before:
            levels_.append(min(k[3] for k in before) if side > 0 else max(k[2] for k in before))
        # at a level: the extreme from 1.5 ATR through it (a dip past support that was rejected) to 0.5 ATR short of it
        at = any(-1.5 * a <= (extreme - p) * side <= 0.5 * a for p in levels_)
        name = CANDLES[key][0 if side > 0 else 1]
        return {"key": key, "name": name, "kind": CANDLES[key][2], "ago": n - 1 - j, "extreme": extreme,
                "at_level": bool(at) and CANDLES[key][2] == "reversal"}
    return None


def rvol(kl: list, n: int = 20) -> float | None:
    """The last candle's quote volume against the average of the n before it."""
    if len(kl) < n + 1:
        return None
    avg = sum(k[6] for k in kl[-n - 1:-1]) / n
    return kl[-1][6] / avg if avg > 0 else None


def atr_contracted(kl: list) -> bool:
    """The 10 candles before the trigger ranged less than 0.8x the 40 before them (a squeeze before the move)."""
    tr = true_ranges(kl)
    if len(tr) < 52:
        return False
    recent, before = tr[-11:-1], tr[-51:-11]
    return sum(recent) / len(recent) < 0.8 * sum(before) / len(before)


def vwap(kl: list, since_ms: float) -> float | None:
    rows = [k for k in kl if k[0] >= since_ms] or kl[-20:]
    vol = sum(k[5] for k in rows)
    return sum((k[2] + k[3] + k[4]) / 3 * k[5] for k in rows) / vol if vol > 0 else None


def round_step(price: float) -> float:
    """The round-number spacing traders put stops on: 500 for BTC near 97k, 50 for ETH near 3.5k, 0.005 for DOGE."""
    return 5 * 10 ** (math.floor(math.log10(price)) - 2) if price > 0 else 0.0


def off_round(stop: float, long: bool, buf: float) -> float:
    """Push a stop that sits on (or just inside) a round number past it by the buffer: obvious stops get run."""
    step = round_step(stop)
    if step <= 0 or buf <= 0:
        return stop
    near = round(stop / step) * step
    if abs(stop - near) < buf:
        return near - buf if long else near + buf
    return stop


# ---- the timeframe's candle sets ----------------------------------------------------------------------------------
def intervals(window_s: float) -> dict[str, str]:
    """LTF = the trigger candles (the signal's ATR candles, about a quarter of the timeframe), MID = about the
    timeframe itself (15m at least), HTF = the context, at least 4x the timeframe (1h at least)."""
    ivs = [i for i in INTERVALS if i in STEP_S]
    ltf = max((i for i in ivs if STEP_S[i] <= max(window_s / 4, 60)), key=lambda i: STEP_S[i])
    mid = max((i for i in ivs if STEP_S[i] <= max(window_s, 900)), key=lambda i: STEP_S[i])
    htf = min((i for i in ivs if STEP_S[i] >= max(4 * window_s, 3600)), key=lambda i: STEP_S[i], default="1w")
    return {"ltf": ltf, "mid": mid, "htf": htf}


def regime(htf: list, mid: list | None = None) -> dict:
    kl = htf if len(htf) >= 60 else (mid or htf)
    a, d = adx(kl), trend_dir(kl)
    tr = true_ranges(kl)
    vol = (sum(tr[-14:]) / 14) / (sum(tr[-100:]) / len(tr[-100:])) if len(tr) >= 30 else 1.0
    if a is None:
        name = "RANGING_MEAN_REVERSION"
    elif a >= ADX_TREND and d:
        name = "TRENDING_BULL" if d > 0 else "TRENDING_BEAR"
    elif vol >= CHOP_VOL or a >= ADX_TREND:
        name = "VOLATILE_CHOP"
    else:
        name = "RANGING_MEAN_REVERSION"
    return {"name": name, "adx": None if a is None else round(a, 1), "dir": d, "vol": round(vol, 2)}


def session(now_s: float, events: list[dict] | None) -> dict:
    """[SESSION]: thin hours or a high-impact release within 15 minutes -> confidence x0.8. `events` None = the
    calendar could not be read (said so, not penalised)."""
    t = time.gmtime(now_s)
    why = []
    if t.tm_wday >= 5:
        why.append("weekend (thin books)")
    elif t.tm_hour >= DEAD_HOUR_UTC:
        why.append(f"{DEAD_HOUR_UTC}:00-24:00 UTC dead zone")
    near = [e for e in events or [] if abs(e["ts"] / 1000 - now_s) <= NEWS_WINDOW_S]
    if near:
        e = near[0]
        mins = (e["ts"] / 1000 - now_s) / 60
        why.append(f"{e.get('country', '')} {e['title']} {'in' if mins >= 0 else ''} {abs(mins):.0f} min{'' if mins >= 0 else ' ago'}"
                   .replace("  ", " ").strip())
    return {"penalty": bool(why), "mult": SESSION_PENALTY if why else 1.0,
            "why": "; ".join(why) or "liquid hours, no high-impact release within 15 min",
            "calendar": events is not None}


# ---- the four pillars: (points 0..25, reasons) --------------------------------------------------------------------
def _cap(pts: float) -> int:
    return int(round(max(0.0, min(25.0, pts))))


def pillar_structure(ltf: list, side: int, price: float, a: float) -> tuple[int, list[str], dict]:
    pts, why = 0.0, []
    b, s, f = bos(ltf, side), sweep(ltf, side), fvg_retest(ltf, side, price, a)
    if b is not None:
        pts += 10
        why.append(f"BOS past {b:.6g}")
    if s is not None:
        pts += 8
        why.append(f"swept the liquidity at {s:.6g}")
    if f is not None:
        pts += 7
        why.append(f"retesting a fair value gap {f[0]:.6g}-{f[1]:.6g}")
    return _cap(pts), why or ["no BOS, sweep or gap retest its way"], {"bos": b}


def pillar_momentum(ltf: list, side: int, setup: str, htf_dir: int) -> tuple[int, list[str], dict]:
    c = [k[4] for k in ltf]
    r = rsi(c)
    rv, prev = r[-1], r[-2] if len(r) > 1 else None
    pts, why, flags = 0.0, [], {"exhausted": False, "div_against": False}
    if rv is not None:
        x = rv if side > 0 else 100 - rv                              # read a short's RSI as a long's
        xp = None if prev is None else (prev if side > 0 else 100 - prev)
        if setup in ("mean_reversion", "range"):
            if x < 40 and xp is not None and x > xp:
                pts += 8
            elif x < 50:
                pts += 4
        elif 50 <= x <= 70:
            pts += 8
        elif 45 <= x < 50 or 70 < x <= 75:
            pts += 4
        flags["exhausted"] = x > 75
        why.append(f"RSI {rv:.0f}{' (stretched)' if flags['exhausted'] else ''}")
    h = macd_hist(c)
    if len(h) >= 2 and h[-1] * side > 0:
        rising = abs(h[-1]) > abs(h[-2])
        pts += 7 if rising else 4
        why.append(f"MACD histogram {'building' if rising else 'with it'}")
    if len(c) >= 60:
        e8, e21, e55 = ema(c, 8)[-1], ema(c, 21), ema(c, 55)[-1]
        stacked = (e8 > e21[-1] > e55) if side > 0 else (e8 < e21[-1] < e55)
        if stacked and (e21[-1] - e21[-4]) * side > 0:
            pts += 6
            why.append("EMA ribbon stacked its way")
    if htf_dir == side:
        pts += 4
        why.append("HTF trend agrees")
    d = divergence(ltf, r)
    if d == side:
        pts += 4
        why.append(f"{'bullish' if d > 0 else 'bearish'} RSI divergence")
    elif d == -side:
        flags["div_against"] = True
        why.append(f"{'bearish' if d < 0 else 'bullish'} RSI divergence against it")
    return _cap(pts), why, flags


def pillar_volume(ltf: list, side: int, price: float, now_ms: float) -> tuple[int, list[str], dict]:
    pts, why = 0.0, []
    rv = rvol(ltf)
    if rv is None:
        pts += 4.5
    elif rv >= RVOL_BREAKOUT:
        pts += 9
    elif rv >= 1.0:
        pts += 4
    if rv is not None:
        why.append(f"RVOL {rv:.1f}x")
    if atr_contracted(ltf):
        pts += 6
        why.append("ATR contracted before the trigger")
    vw = vwap(ltf, now_ms - 86_400_000)
    if vw and (price - vw) * side > 0:
        pts += 6
        why.append(f"{'above' if side > 0 else 'below'} the 24h VWAP {vw:.6g}")
    elif vw:
        why.append(f"wrong side of the 24h VWAP {vw:.6g}")
    last = ltf[-3:]
    q = sum(k[6] for k in last)
    if q > 0:
        share = sum(k[7] for k in last) / q
        if (share - 0.5) * side >= 0.02:
            pts += 4
        why.append(f"taker buys {share * 100:.0f}%")
    return _cap(pts), why, {"rvol": rv}


def pillar_derivs(side: int, *, oi_pct: float | None, price_pct: float, funding: float | None,
                  curve_score: float | None, leader_dir: int | None, leader: str) -> tuple[int, list[str], dict]:
    pts, why = 0.0, []
    if oi_pct is None:
        pts += 3.5
    else:
        with_move = price_pct * side > 0
        if oi_pct > 1 and with_move:
            pts += 7
            why.append(f"open interest {oi_pct:+.1f}% behind the move")
        elif oi_pct < -1 and not with_move:
            pts += 3                                                    # the other side closing out
            why.append(f"open interest {oi_pct:+.1f}% (the other side closing)")
        else:
            why.append(f"open interest {oi_pct:+.1f}%")
    if funding is None:
        pts += 3
    else:
        f = funding * side                                             # positive = the trade's side pays
        pts += 6 if f <= 0.0001 else 3 if f <= FUNDING_EXTREME else 0
        why.append(f"funding {funding * 100:+.4f}%")
    if curve_score is None:
        pts += 2.5
    elif curve_score * side > 0.05:
        pts += 5
        why.append("premium trend agrees")
    elif abs(curve_score) <= 0.05:
        pts += 2
    if leader_dir is None:
        pts += 3.5
    elif leader_dir == side:
        pts += 7
        why.append(f"{leader} agrees")
    elif leader_dir == 0:
        pts += 3
        why.append(f"{leader} flat")
    else:
        why.append(f"{leader} against it")
    return _cap(pts), why, {"leader_against": leader_dir == -side}


# ---- stop and targets ---------------------------------------------------------------------------------------------
def levels(entry: float, long: bool, ltf: list, mid: list, htf: list, a: float, stop_atr: float = MIN_STOP_ATR,
           tp1_r: float = TP1_R, tp2_r: float = TP2_R, reach: float | None = None, hold: str = "",
           beyond: float | None = None) -> dict:
    """Stop past the last swing (>= stop_atr LTF ATRs from the entry) + STOP_BUFFER_ATR, off round numbers; TP1 at
    the first unbroken structure level >= tp1_r (else tp1_r), TP2 at the first one from tp2_r to
    max(TP2_MAX_R, tp2_r + 2) (else tp2_r). `reach`: the furthest price distance TP1 may be (how far price can
    travel in the hold time); a TP1 beyond it aborts. `beyond`: a price the stop must sit past too (a candlestick
    pattern's low for a long / high for a short). 'abort' says why there is no trade."""
    side = 1 if long else -1
    buf = STOP_BUFFER_ATR * a
    _, lows = swings(ltf)
    highs, _ = swings(ltf)
    pool = [p for _, p in (lows if long else highs)[-6:] if (p < entry if long else p > entry)]
    swing = (max(pool) if long else min(pool)) if pool else None      # the nearest swing behind the entry
    far = entry - stop_atr * a if long else entry + stop_atr * a
    base = (min(swing, far) if long else max(swing, far)) if swing is not None else far
    if beyond is not None and (beyond < entry if long else beyond > entry):
        base = min(base, beyond) if long else max(base, beyond)
    stop = off_round(base - buf if long else base + buf, long, buf)
    risk = abs(entry - stop)
    if risk < entry * MIN_STOP_PCT:
        risk = entry * MIN_STOP_PCT
        stop = entry - risk if long else entry + risk
    out = {"stop": stop, "risk": risk, "swing": swing, "tp1": None, "tp2": None, "rr1": None, "rr2": None,
           "htf_level": None, "room_r": None, "abort": ""}
    if risk > entry * MAX_STOP_PCT:
        out["abort"] = f"the structural stop is {risk / entry * 100:.1f}% away (more than {MAX_STOP_PCT * 100:.0f}%)"
        return out
    R = lambda p: (p - entry) * side / risk                           # noqa: E731
    cap = next(iter(unbroken(htf, long, entry)), None)
    if cap is not None:
        out["htf_level"], out["room_r"] = cap, round(R(cap), 2)
        if R(cap) < HTF_CAP_R:
            out["abort"] = f"an HTF {'resistance' if long else 'support'} at {cap:.6g} caps the reward at {R(cap):.1f}R (under {HTF_CAP_R}R)"
            return out
        if R(cap) < tp1_r:
            out["abort"] = f"an HTF {'resistance' if long else 'support'} at {cap:.6g} leaves {R(cap):.1f}R, TP1 needs {tp1_r:g}R"
            return out
    structure = sorted(set(unbroken(ltf, long, entry) + unbroken(mid, long, entry)), reverse=not long)
    tp1 = next((p for p in structure if tp1_r <= R(p) < tp2_r), entry + side * tp1_r * risk)
    top = max(TP2_MAX_R, tp2_r + 2)
    tp2 = next((p for p in structure if tp2_r <= R(p) <= top and R(p) > R(tp1)), entry + side * tp2_r * risk)
    if tp2 <= 0 or tp1 <= 0:
        out["abort"] = "the target would be below zero"
        return out
    out.update(tp1=tp1, tp2=tp2, rr1=round(R(tp1), 2), rr2=round(R(tp2), 2))
    if reach and abs(tp1 - entry) > reach:
        out["abort"] = (f"TP1 is {abs(tp1 - entry) / a:.1f} ATR away ({R(tp1):.1f}R behind a {risk / a:.1f} ATR stop): "
                        f"too far to reach in the {hold or 'hold'} before the time exit (at most {reach / a:.1f} ATR)")
    return out


# ---- everything together ------------------------------------------------------------------------------------------
def type_entry(style: str, *, long: bool, mark: float, whale_entry: float, ltf: list, a: float,
               level: float | None) -> float:
    """Where a type enters: the price, the whales' pullback, the LTF EMA 21 or a retest of the broken level, each only
    when it lies on the right side of the price and close enough to fill (else the price)."""
    side = 1 if long else -1
    if style == "whales":
        return whale_entry
    if style == "ema21":
        e = ema([k[4] for k in ltf], 21)[-1]
        return e if 0 < (mark - e) * side <= 1.5 * a else mark
    if style == "retest" and level is not None:
        e = level + side * 0.1 * a                                    # just on the broken side of it
        return e if 0 < (mark - e) * side <= 1.0 * a else mark
    return mark


def assess(*, side: int, mark: float, entry: float, base_conf: float, ltf: list, mid: list, htf: list,
           leader_mid: list | None, is_leader: bool, macro_score: float | None, funding: float | None,
           spot: list | None, oi_pct: float | None, curve_score: float | None, events: list[dict] | None,
           now_s: float, ivs: dict, score: float = 0.0, types: list[dict] | None = None,
           hold: float | None = None) -> dict:
    """One coin's verdict for one side (+1 long, -1 short). Candles are full Binance rows (the forming one is
    dropped here). `entry` is the whales' entry (used by types that enter "whales"); `types` the classification
    order (arbbot/signal_types.TypeBook.resolved(); the built-in types when None). `hold`: seconds the trade may be
    held (hold_s()); when given, a TP1 too far to reach in that time is NO_TRADE."""
    ltf, mid, htf = closed(ltf), closed(mid), closed(htf)
    if len(ltf) < 60 or len(mid) < 30:
        raise ValueError("not enough candles")
    long = side > 0
    a = atr(ltf) or mark * 0.003
    reg = regime(htf, mid)
    htf_dir = reg["dir"]
    trig = ltf[-1]
    prior = ltf[-21:-1]
    breakout = (trig[4] > max(k[2] for k in prior)) if long else (trig[4] < min(k[3] for k in prior))
    if breakout:
        setup = "breakout"
    elif htf_dir == side:
        setup = "trend"
    elif htf_dir == -side:
        setup = "mean_reversion"
    else:
        setup = "range"

    s_pts, s_why, s_f = pillar_structure(ltf, side, mark, a)
    m_pts, m_why, m_f = pillar_momentum(ltf, side, setup, htf_dir)
    v_pts, v_why, v_f = pillar_volume(ltf, side, mark, now_s * 1000)
    price_pct = (ltf[-1][4] / ltf[-5][4] - 1) * 100 if len(ltf) >= 5 else 0.0
    if is_leader:
        lead_dir = None if macro_score is None else (1 if macro_score > 0.05 else -1 if macro_score < -0.05 else 0)
        leader = "macro (S&P / dollar)"
    else:
        lead_dir = trend_dir(closed(leader_mid)) if leader_mid else None
        leader = "BTC"
    d_pts, d_why, d_f = pillar_derivs(side, oi_pct=oi_pct, price_pct=price_pct, funding=funding,
                                      curve_score=curve_score, leader_dir=lead_dir, leader=leader)
    q = s_pts + m_pts + v_pts + d_pts
    pillars = {"structure": {"pts": s_pts, "why": s_why}, "momentum": {"pts": m_pts, "why": m_why},
               "volume": {"pts": v_pts, "why": v_why}, "derivs": {"pts": d_pts, "why": d_why}}

    gates = []
    if reg["name"] == "VOLATILE_CHOP":
        gates.append(f"choppy market: HTF ADX {reg['adx']}, ranges {reg['vol']}x their average, no clean trend")
    adx_mid, adx_htf = adx(mid), reg["adx"]
    strong = max(x for x in (adx_mid, adx_htf, 0.0) if x is not None)
    if setup in ("mean_reversion", "range") and strong > ADX_STRONG and s_f["bos"] is None:
        gates.append(f"[REGIME] mean reversion against ADX {strong:.0f} (> {ADX_STRONG:.0f}) with no break of structure")
    if setup == "breakout" and v_f["rvol"] is not None and v_f["rvol"] < RVOL_BREAKOUT:
        gates.append(f"[FAKEOUT] breakout on RVOL {v_f['rvol']:.1f}x (needs {RVOL_BREAKOUT}x)")
    spot_falling = None
    if spot and len(closed(spot)) >= 12:
        sv = [k[6] for k in closed(spot)]
        spot_falling = sum(sv[-6:]) < SPOT_DECLINE * sum(sv[-12:-6])
    if funding is not None and funding * side > FUNDING_EXTREME:
        if spot_falling is None:
            vol = [k[6] for k in mid]
            spot_falling = sum(vol[-6:]) < SPOT_DECLINE * sum(vol[-12:-6])    # no spot market: futures volume
        if spot_falling:
            gates.append(f"[FUNDING] {'longs' if long else 'shorts'} pay {abs(funding) * 100:.3f}% per 8h while spot volume declines")

    # the signal type: classify, then its own entry, stop distance, targets, requirements and quality bar
    r_now = rsi([k[4] for k in ltf])[-1]
    vw = vwap(ltf, now_s * 1000 - 86_400_000)
    cp, cp_against = candle_pattern(ltf, side, a), candle_pattern(ltf, -side, a)
    if cp:
        where = (" at a support" if long else " at a resistance") if cp["at_level"] else ""
        when = "the last candle" if cp["ago"] == 0 else "the candle before"
        pillars["structure"]["why"].append(f"candles: {cp['name']}{where} ({when})")
    if cp_against:
        pillars["structure"]["why"].append(f"candles against it: {cp_against['name']}")
    features = {
        "regime": reg["name"], "htf": "with" if htf_dir == side else "against" if htf_dir == -side else "flat",
        "btc": None if lead_dir is None else "with" if lead_dir == side else "against" if lead_dir == -side else "flat",
        "breakout": breakout, "bos": s_f["bos"] is not None, "sweep": sweep(ltf, side) is not None,
        "fvg": fvg_retest(ltf, side, mark, a) is not None, "squeeze": atr_contracted(ltf),
        "vwap_side": None if vw is None else (mark - vw) * side > 0, "rvol": v_f["rvol"],
        "rsi": None if r_now is None else (r_now if long else 100 - r_now),
        "adx_htf": reg["adx"], "adx_mid": None if adx_mid is None else round(adx_mid, 1),
        "vwap_dist": None if vw is None else round((mark - vw) * side / a, 2),
        "funding": None if funding is None else round(funding * side * 100, 4),
        "oi": None if oi_pct is None else round(oi_pct, 2), "quality": q, "score": round(abs(score) * 100, 1),
        "candle": cp is not None, "candle_at_level": bool(cp and cp["at_level"]),
        "candle_pattern": cp["key"] if cp else "none", "candle_against": cp_against is not None,
    }
    t = st.classify(features, types or [dict(b, custom=False, enabled=True) for b in st.BUILTIN])
    if not t.get("enabled", True):
        gates.append(f"{t['name']} trades are switched off")
    gates.extend(st.unmet(features, t))
    level = s_f["bos"] if s_f["bos"] is not None else (max(k[2] for k in prior) if long else min(k[3] for k in prior))
    entry = type_entry(t["entry"], long=long, mark=mark, whale_entry=entry, ltf=ltf, a=a, level=level)
    reach = hold_label = None
    if hold and ivs.get("ltf") in STEP_S:
        reach = REACH_X * a * math.sqrt(max(1.0, hold / STEP_S[ivs["ltf"]]))
        hold_label = f"{hold / 3600:g}h hold" if hold >= 3600 else f"{hold / 60:g}min hold"
    uses_candle = cp and any(c["k"].startswith("candle") and c["k"] != "candle_against" for c in t["when"] + t["require"])
    lv = levels(entry, long, ltf, mid, htf, a, t["stop_atr"], t["tp1_r"], t["tp2_r"], reach=reach, hold=hold_label or "",
                beyond=cp["extreme"] if uses_candle else None)
    if lv["abort"]:
        gates.append(lv["abort"])

    # pre-mortem: the first active risk in this order is THE reason it would fail
    risks = [
        (reg["name"] == ("TRENDING_BEAR" if long else "TRENDING_BULL") and s_f["bos"] is None, "cancel",
         f"it fights the HTF {'down' if long else 'up'}trend (ADX {reg['adx']}) with no break of structure: the next "
         f"trend leg runs it over"),
        (d_f["leader_against"] and not is_leader, "cancel",
         f"BTC is trending {'down' if long else 'up'} on the {ivs['mid']} candles: alts follow BTC"),
        (m_f["exhausted"] or m_f["div_against"], "downgrade",
         "momentum is stretched / diverging: the move may be ending as it enters"),
        (funding is not None and funding * side > FUNDING_EXTREME, "downgrade",
         f"{'longs' if long else 'shorts'} are crowded (funding {funding * 100:+.3f}%): a squeeze the other way"
         if funding is not None else ""),
        (lv["risk"] and (atr(mid) or 0) > 2 * lv["risk"], "downgrade",
         f"the stop is inside one normal {ivs['mid']} candle's range: noise can take it"),
    ]
    active = next(((sev, text) for on, sev, text in risks if on), None)
    if active:
        pm = {"text": active[1], "severity": active[0], "active": True}
    else:
        pm = {"text": f"a sudden BTC-led move: the stop is {lv['risk'] / a:.1f} LTF ATRs away" if lv["risk"] else
              "a sudden BTC-led move", "severity": "none", "active": False}
    if pm["severity"] == "cancel":
        gates.append("pre-mortem: " + pm["text"])
    if q < t["min_quality"]:
        gates.append(f"quality {q}/100 ({t['name']} needs {t['min_quality']})")

    sess = session(now_s, events)
    conf = (0.4 * base_conf + 0.6 * q) * sess["mult"] - (PREMORTEM_DOWNGRADE if pm["severity"] == "downgrade" else 0)
    return {"verdict": "no_trade" if gates else "trade", "gates": gates, "quality": q, "pillars": pillars,
            "regime": reg, "setup": setup, "session": sess, "premortem": pm,
            "confidence": int(max(0, min(100, round(conf)))), "levels": lv, "atr_ltf": a, "intervals": ivs,
            "entry": entry, "features": features,
            "candles": {"its_way": cp, "against": cp_against},
            "type": {"key": t["key"], "name": t["name"], "custom": bool(t.get("custom")), "entry": t["entry"],
                     "risk_mult": t["risk_mult"], "min_quality": t["min_quality"], "tp1_r": t["tp1_r"],
                     "tp2_r": t["tp2_r"], "stop_atr": t["stop_atr"], "enabled": t.get("enabled", True)},
            "adx_mid": None if adx_mid is None else round(adx_mid, 1)}
