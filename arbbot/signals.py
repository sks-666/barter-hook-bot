"""The terminal's combined signal: one timeframe, five readings, one trade idea per coin.

The timeframe is the Whale flow panel's (1 second to 1 week). It picks how each source is read:

  whales   whale buys vs sells and liquidations over the timeframe (arbbot/whales.py)
  crowd    Whales vs crowd: top traders' long share against all accounts', its change, taker buy/sell, open interest,
           in Binance rows of a step that fits the timeframe (5m rows for 1h, 4h rows for 1d, ...)
  curve    Futures curve: the perpetual's premium (basis) trend over the timeframe, and funding (crowding)
  macro    Macro: S&P 500, VIX, dollar index, 10-year yield over the timeframe in days (from 1h up; weighs most on 1d+)
  news     Headlines of the timeframe (at least the last hour) about this coin or crypto, by bullish / bearish words

Each reading is a score from -1 (bearish) to +1 (bullish) with a reason; their weights depend on the timeframe. The
combined score picks LONG / SHORT / WAIT; entry, stop and target come from the whales' own prices and the coin's ATR
on candles that fit the timeframe. A heuristic for the page: nothing here trades and nothing has been backtested.
"""
import asyncio
import logging
import math
import re
import time

from .market import CURVE_PERIODS, INTERVALS, SENT_PERIODS, SENT_ROWS

log = logging.getLogger("signals")

STEP_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14_400, "6h": 21_600,
          "12h": 43_200, "1d": 86_400, "1w": 604_800}
TTL_S = 60.0                 # a coin's combined signal is reused this long (each costs ~14 Binance calls)
MAX_COINS = 6                # coins scored per request (the busiest whale coins), plus the one the page has loaded
LONG_AT = 0.15               # |combined score| needed for a side
ATR_N = 14

BULL = re.compile(r"\b(surg\w*|rall\w*|soar\w*|jump\w*|gain\w*|climb\w*|rebound\w*|recover\w*|record high|all-time high|"
                  r"inflows?|approv\w*|adopt\w*|bullish|breakout|upgrade\w*|partnership|rate cuts?|cuts? rates|"
                  r"buy(s|ing)?|accumulat\w*|beats?|boost\w*|optimis\w*)\b", re.I)
BEAR = re.compile(r"\b(plung\w*|crash\w*|drop\w*|fall(s|ing|en)?|fell|slump\w*|sell-?offs?|selling|outflows?|hack\w*|"
                  r"exploit\w*|lawsuits?|sues?|sued|ban\w*|reject\w*|bearish|liquidat\w*|fears?|declin\w*|tumbl\w*|"
                  r"sink\w*|sank|probe|fraud|warn\w*|rate hikes?|hikes? rates|recession|delist\w*|misses?|losses)\b", re.I)


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def step_for(window_s: float, steps: tuple, rows: int) -> tuple[str, int]:
    """The step to read a source in for this timeframe, and how many steps back the timeframe reaches: about six
    rows per timeframe, never under the source's smallest step, never past what it returns."""
    target = max(window_s / 6, STEP_S[steps[0]])
    best = steps[0]
    for s in steps:
        if s in STEP_S and STEP_S[s] <= target:
            best = s
    return best, int(clamp(round(window_s / STEP_S[best]), 1, rows - 1))


def weights(window_s: float) -> dict[str, float]:
    """How much each reading counts at this timeframe: macro means nothing to a 5-minute trade and much to a week."""
    macro = 0.0 if window_s < 3600 else 0.05 if window_s < 14_400 else 0.10 if window_s < 86_400 else 0.20
    news = 0.05 if window_s < 900 else 0.15 if window_s <= 86_400 else 0.10
    return {"whales": 0.35, "crowd": 0.25, "curve": 0.15, "macro": macro, "news": news}


# ---- the five readings: each returns (score -1..+1, reason) ----------------------------------------------------
def score_whales(row: dict | None) -> tuple[float | None, str]:
    if not row:
        return None, "fewer than 3 whale orders"                     # no reading: left out of the weighting
    s = row["pressure"] * min(1.0, row["n"] / 5)
    liq = ""
    if row["liq_short"] or row["liq_long"]:
        liq = f", shorts liquidated ${_big(row['liq_short'])} / longs ${_big(row['liq_long'])}"
    return s, f"whale buys ${_big(row['buy'])} vs sells ${_big(row['sell'])}{liq} ({row['n']} orders)"


def score_crowd(data: dict, k: int) -> tuple[float, str]:
    rows = [r for r in data.get("rows", []) if "top_pos_long_pct" in r and "long_pct" in r]
    if not rows:
        raise ValueError("no rows")
    now, then = rows[0], rows[min(k, len(rows) - 1)]
    gap = now["top_pos_long_pct"] - now["long_pct"]
    # Binance's top traders nearly always sit ~10 pts less long than the crowd, so the gap is read against its own
    # average over the rows returned: whales longer than they usually are against the crowd is bullish
    gaps = [r["top_pos_long_pct"] - r["long_pct"] for r in rows]
    rel = gap - sum(gaps) / len(gaps)
    d_top = now["top_pos_long_pct"] - then["top_pos_long_pct"]      # whales adding longs over the timeframe
    takers = [r["taker_ratio"] for r in rows[:max(k, 1)] if r.get("taker_ratio")]
    taker = sum(takers) / len(takers) if takers else 1.0
    s = 0.35 * clamp(rel / 5) + 0.35 * clamp(d_top / 3) + 0.3 * clamp((taker - 1) / 0.15)
    oi = ""
    if now.get("oi_usd") and then.get("oi_usd"):
        d_oi = (now["oi_usd"] / then["oi_usd"] - 1) * 100
        s *= 1.2 if d_oi > 1 else 0.8 if d_oi < -1 else 1.0          # new money behind the move, or money leaving
        oi = f", open interest {d_oi:+.1f}%"
    return clamp(s), (f"whales {now['top_pos_long_pct']:.0f}% long vs crowd {now['long_pct']:.0f}% "
                      f"({gap:+.1f} pts, {rel:+.1f} vs usual), "
                      f"whales {d_top:+.1f} pts, taker buy/sell {taker:.2f}{oi}")


def score_curve(data: dict, k: int) -> tuple[float, str]:
    perp = next((p for p in data.get("points", []) if p["type"] == "PERPETUAL"), None)
    if not perp:
        raise ValueError("no perpetual")
    hist = data.get("history", {}).get("PERPETUAL") or []
    trend, why = 0.0, ""
    if len(hist) >= 2:
        d = hist[-1]["basis_pct"] - hist[-1 - min(k, len(hist) - 1)]["basis_pct"]
        trend = clamp(d / 0.05)                                       # buyers paying up for the perpetual: bullish
        why = f"premium {d:+.3f} pts, "
    ann = perp.get("ann_pct") or 0.0
    crowd = -clamp((ann - 11) / 40)                                   # ~11%/yr is normal; far above = crowded longs
    return clamp(0.6 * trend + 0.4 * crowd), f"{why}funding {ann:+.1f}%/yr"


def score_macro(data: dict, window_s: float) -> tuple[float, str]:
    days = int(clamp(round(window_s / 86_400), 1, 30))
    series = data.get("series", {})

    def change(sym, pct=True):
        rows = (series.get(sym) or {}).get("rows") or []
        if len(rows) <= days:
            return None
        a, b = rows[-1 - days][1], rows[-1][1]
        return (b / a - 1) * 100 if pct else b - a
    parts, why = [], []
    for sym, label, scale, sign, pct in (("^GSPC", "S&P", 2.0, 1, True), ("^VIX", "VIX", 10.0, -1, True),
                                         ("DX-Y.NYB", "dollar", 1.0, -1, True), ("^TNX", "10y", 0.15, -1, False)):
        c = change(sym, pct)
        if c is not None:
            parts.append(sign * clamp(c / scale))
            why.append(f"{label} {c:+.2f}{'%' if pct else ' pts'}")
    if not parts:
        raise ValueError("no macro data")
    return sum(parts) / len(parts), f"over {days}d: " + ", ".join(why)


def score_news(data: dict, base: str, window_s: float, now_ms: float) -> tuple[float, str]:
    since = now_ms - max(window_s, 3600) * 1000
    total = weight = 0.0
    n = pos = neg = 0
    for it in data.get("items", []):
        if it["ts"] < since:
            continue
        coins = it.get("coins") or []
        if base in coins:
            w = 1.0
        elif it.get("kind") == "crypto" or "BTC" in coins or set(it.get("topics") or []) & {"fed", "inflation", "regulation"}:
            w = 0.4                                                   # market-wide news moves every coin a little
        else:
            continue
        text = f"{it['title']} {it.get('summary', '')}"
        b, r = len(BULL.findall(text)), len(BEAR.findall(text))
        n += 1
        pos += b > r
        neg += r > b
        total += w * clamp((b - r) / 2)
        weight += w
    if not n:
        return None, "no headlines in the timeframe"
    return clamp(total / weight * min(1.0, n / 5)), f"{n} headlines: {pos} bullish, {neg} bearish"


def _big(v: float) -> str:
    for n, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= n:
            return f"{v / n:.1f}{s}"
    return f"{v:.0f}"


def atr(klines: list[list], n: int = ATR_N) -> float | None:
    """Average true range of the last n candles ([open time, open, high, low, close, ...])."""
    rows = [(float(k[2]), float(k[3]), float(k[4])) for k in klines[-(n + 1):]]
    if len(rows) < 2:
        return None
    trs = [max(h - l, abs(h - pc), abs(l - pc)) for (h, l, _), (_, _, pc) in zip(rows[1:], rows[:-1])]
    return sum(trs) / len(trs)


LABELS = {"whales": "Whale flow", "crowd": "Whales vs crowd", "curve": "Futures curve", "macro": "Macro", "news": "News"}


class SignalDesk:
    def __init__(self, whales, market, macro=None, clock=time.monotonic, wall=time.time):
        self.whales, self.market, self.macro, self.clock, self.wall = whales, market, macro, clock, wall
        self._cache: dict[tuple, tuple[float, dict]] = {}

    async def view(self, window_s: float, symbol: str | None = None) -> dict:
        w = self.whales.clamp_window(window_s)
        whale_rows = {r["symbol"]: r for r in self.whales.signals(w)}
        busiest = sorted(whale_rows.values(), key=lambda r: -(r["buy"] + r["sell"]))
        coins = [r["symbol"] for r in busiest[:MAX_COINS]]
        if symbol and symbol not in coins:
            coins.append(symbol)
        sent_p, sent_k = step_for(w, SENT_PERIODS, SENT_ROWS)
        curve_p, curve_k = step_for(w, CURVE_PERIODS, 60)
        iv = max((i for i in INTERVALS if STEP_S[i] <= max(w / 4, 60)), key=lambda i: STEP_S[i])
        macro, news = await self._shared(w)
        rows = await asyncio.gather(*(self._coin(c, w, whale_rows.get(c), (sent_p, sent_k), (curve_p, curve_k), iv,
                                                 macro, news) for c in coins))
        rows = [r for r in rows if r]
        rows.sort(key=lambda r: (r["bias"] == "wait", -r["confidence"]))
        return {"window_s": w, "steps": {"crowd": f"{sent_p} x{sent_k}", "curve": f"{curve_p} x{curve_k}", "atr": iv},
                "weights": weights(w), "rows": rows}

    async def _shared(self, w: float):
        """Macro and news are the same for every coin: fetch them once per request (MacroNews caches them)."""
        if self.macro is None:
            return None, None
        got = await asyncio.gather(self.macro.macro(), self.macro.news(), return_exceptions=True)
        return tuple(None if isinstance(g, BaseException) else g for g in got)

    async def _coin(self, sym, w, whale, sent, curve, iv, macro, news) -> dict | None:
        key = (sym, w)
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < TTL_S:
            return hit[1]
        try:
            row = await self._score(sym, w, whale, sent, curve, iv, macro, news)
        except Exception as e:                                        # one coin failing never hides the rest
            log.warning("signal for %s failed: %s: %s", sym, type(e).__name__, e)
            return None
        self._cache[key] = (self.clock(), row)
        if len(self._cache) > 200:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                del self._cache[k]
        return row

    async def _score(self, sym, w, whale, sent, curve, iv, macro, news) -> dict:
        crowd_d, curve_d, kl = await asyncio.gather(self.market.trading_data(sym, sent[0]),
                                                    self.market.curve(sym, curve[0]),
                                                    self.market.klines(sym, iv), return_exceptions=True)
        wt = weights(w)
        factors = []

        def add(name, fn):
            try:
                s, why = fn()                                         # s None: nothing to read, left out
                factors.append({"key": name, "label": LABELS[name], "score": None if s is None else round(s, 3),
                                "weight": 0.0 if s is None else wt[name], "why": why})
            except Exception as e:                                    # a source that is down just drops out
                factors.append({"key": name, "label": LABELS[name], "score": None, "weight": 0.0,
                                "why": f"unavailable ({type(e).__name__})"})

        def need(d):
            if d is None or isinstance(d, BaseException):
                raise d if isinstance(d, BaseException) else ValueError("not available")
            return d
        add("whales", lambda: score_whales(whale))
        add("crowd", lambda: score_crowd(need(crowd_d), sent[1]))
        add("curve", lambda: score_curve(need(curve_d), curve[1]))
        if wt["macro"]:
            add("macro", lambda: score_macro(need(macro), w))
        add("news", lambda: score_news(need(news), sym.removesuffix("USDT"), w, self.wall() * 1000))
        used = [f for f in factors if f["score"] is not None and f["weight"] > 0]
        total_w = sum(f["weight"] for f in used) or 1.0
        score = sum(f["score"] * f["weight"] for f in used) / total_w
        bias = "long" if score >= LONG_AT else "short" if score <= -LONG_AT else "wait"
        sign = 1 if score > 0 else -1
        agree = sum(f["weight"] for f in used if f["score"] * sign > 0.05) / total_w
        coverage = total_w / sum(wt.values())                         # how much of the full weight had something to read
        conf = round((clamp(abs(score) / 0.5, 0, 1) * 60 + agree * 40) * (0.5 + 0.5 * coverage))

        mark = (self.whales.marks.get(sym) or (None, 0))[0]
        if mark is None:
            mark = float(kl[-1][4]) if isinstance(kl, list) and kl else (whale or {}).get("mark")
        a = atr(kl) if isinstance(kl, list) else None
        row = {"symbol": sym, "mark": mark, "score": round(score, 3), "bias": bias, "confidence": conf if bias != "wait" else 0,
               "agree": round(agree, 2), "coverage": round(coverage, 2), "factors": factors, "atr": a, "atr_interval": iv,
               "entry": None, "stop": None, "target": None, "rr": None}
        if bias == "wait" or not mark:
            return row
        long = bias == "long"
        lv = self.whales.levels(sym, long, w) if whale else None
        risk = max(x for x in ((lv or {}).get("risk"), a, mark * 0.003) if x)
        risk = min(risk, mark * 0.05)
        anchor = (lv or {}).get("anchor")
        entry = mark
        if anchor:                                                    # a pullback to the whales' price, at most 1 risk away
            entry = max(min(mark, anchor), mark - risk) if long else min(max(mark, anchor), mark + risk)
        stop = entry - risk if long else entry + risk
        other = (lv or {}).get("other")
        far = other is not None and ((other - entry) if long else (entry - other)) >= 1.5 * risk
        target = other if far else (entry + 2 * risk if long else entry - 2 * risk)
        if target <= 0:
            return row
        row.update(entry=entry, stop=stop, target=target, rr=abs(target - entry) / risk,
                   to_entry_pct=(entry / mark - 1) * 100)
        return row
