"""Whale watch: the market's big trades and its liquidations, live from Binance's PUBLIC USD-M futures streams (mainnet).

Read-only and key-less. One WebSocket connection carries `!forceOrder@arr` (every liquidation on the exchange), `!markPrice@arr@1s` (every
coin's mark price and funding, for the terminal's alerts: arbbot/price_alerts.py) and `<coin>@aggTrade` for the most-traded
coins. A taker order that sweeps several price levels arrives as several aggTrades
a few milliseconds apart, so consecutive trades of one coin on the same side within MERGE_MS are summed into one order.
An order is a whale when it is worth at least its coin's threshold: VOLUME_SHARE of the coin's 24h volume, never below
MIN_USD (so BTC needs ~$150K, a small coin $50K).

The dashboard reads `view()`: the latest whale orders and liquidations, and each coin's whale buying vs selling over a
timeframe of its choice (1 second to 1 week; `view(window_s=...)`). The bot never trades on any of this. A testnet bot watches mainnet too: that is where the real market is.
"""
import asyncio
import json
import logging
import math
import re
import time
from collections import deque

import aiohttp

log = logging.getLogger("whales")
# Binance serves the USD-M trade, liquidation and mark-price streams under /market (the root path still accepts them but
# sends nothing); order-book streams stay on the root.
WS_BASE = "wss://fstream.binance.com/market"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
MIN_USD = 50_000.0
VOLUME_SHARE = 1e-5          # a coin's whale threshold as a share of its 24h quote volume
TOP_N = 40                   # coins watched for big trades (liquidations: every coin)
MERGE_MS = 100               # aggTrades of one sweep arrive this close together
KEEP = 400                   # whale orders / liquidations kept in memory, all coins together
KEEP_PER_COIN = 200          # and per coin, for the chart's markers (a busy coin would push a quiet one out of KEEP)
FLOW_WINDOW_S = 3600.0       # the default flow timeframe
MAX_WINDOW_S = 604_800.0     # the longest one (1 week): whale orders and liquidations are logged this far back
LOG_CAP = 600_000            # hard cap on each log (about 60 MB), should a crash week produce more than it can hold
RERANK_S = 1800.0            # pick the most-traded coins again this often
# trade ideas from whale activity (signals()): a heuristic for the terminal, never traded on
SIG_MIN_ORDERS = 3           # fewer whale orders in the window than this: no idea, too little to read
SIG_PRESSURE = 0.25          # |net pressure| needed for a LONG / SHORT bias
SIG_STOP_SIGMA = 1.5         # the stop sits this many "whale price spreads" beyond the entry
SIG_MIN_RISK = 0.003         # but never closer than 0.3% of the price ...
SIG_MAX_RISK = 0.03          # ... and the spread used is capped at 3%
SIG_RR = 2.0                 # the target when the other side's VWAP is too close: 2R
SIG_MIN_RR = 1.5             # the other side's VWAP is the target only if at least this many R away


class WhaleWatch:
    def __init__(self, session: aiohttp.ClientSession | None, rest, clock=time.time, top_n: int = TOP_N,
                 base: str = WS_BASE):
        self.session, self.rest, self.clock, self.top_n, self.base = session, rest, clock, top_n, base.rstrip("/")
        self.thresholds: dict[str, float] = {}
        self.trades: deque[dict] = deque(maxlen=KEEP)
        self.liqs: deque[dict] = deque(maxlen=KEEP)
        self._pending: dict[str, dict] = {}
        self.history: dict[str, dict[str, deque]] = {}    # symbol -> {"trades": ..., "liqs": ...}
        # compact, time-ordered logs behind the flow table, as seen by this bot (its own clock, so a 1 s window is exact):
        self.flow_log: deque[tuple] = deque(maxlen=LOG_CAP)   # (seen_ms, symbol, is_buy, usd, price)
        self.liq_log: deque[tuple] = deque(maxlen=LOG_CAP)    # (seen_ms, is_long, usd, symbol, price)
        self._born = clock()                                  # how far back the logs can possibly reach
        self.marks: dict[str, tuple[float, float]] = {}   # symbol -> (mark price, funding rate), every second
        # callbacks: on_event("whale" | "liq", order) for each new one, on_marks(marks) after each mark-price update
        self.on_event = lambda kind, ev: None
        self.on_marks = lambda marks: None
        self._ranked = 0.0
        self._swept = 0.0
        self.status, self.error = "starting", ""

    # ---- which coins, and how big is a whale on each --------------------------------------------
    async def rank(self) -> None:
        rows = await self.rest.get("fapi", "/fapi/v1/ticker/24hr", timeout=10.0)
        liquid = sorted((r for r in rows if SYMBOL_RE.match(r.get("symbol", ""))),
                        key=lambda r: -float(r.get("quoteVolume") or 0))[:self.top_n]
        self.thresholds = {r["symbol"]: max(MIN_USD, float(r["quoteVolume"]) * VOLUME_SHARE) for r in liquid}
        self._ranked = self.clock()

    def url(self) -> str:
        streams = ["!forceOrder@arr", "!markPrice@arr@1s"] + [f"{s.lower()}@aggTrade" for s in self.thresholds]
        return f"{self.base}/stream?streams={'/'.join(streams)}"

    # ---- stream messages -----------------------------------------------------------------------
    def handle(self, payload: dict) -> None:
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        if isinstance(data, list):                        # !markPrice@arr: every coin, once a second
            for e in data:
                if e.get("e") == "markPriceUpdate" and e.get("s"):
                    self.marks[e["s"]] = (float(e["p"]), float(e.get("r") or 0.0))
            self._call(self.on_marks, self.marks)
            return
        if not isinstance(data, dict):
            return
        kind = data.get("e")
        if kind == "aggTrade":
            self._trade(data)
        elif kind == "forceOrder" and isinstance(data.get("o"), dict):
            self._liq(data["o"])
        now = self.clock()
        if now - self._swept >= 0.25:                     # orders that stopped growing are complete
            self._swept = now
            self.sweep(now * 1000)

    @staticmethod
    def _log(log_: deque, row: tuple) -> None:
        log_.append(row)
        cutoff = row[0] - MAX_WINDOW_S * 1000
        while log_[0][0] < cutoff:
            log_.popleft()

    def _remember(self, sym: str, kind: str, ev: dict) -> None:
        h = self.history.setdefault(sym, {"trades": deque(maxlen=KEEP_PER_COIN), "liqs": deque(maxlen=KEEP_PER_COIN)})
        h[kind].append(ev)

    @staticmethod
    def _call(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:                                 # an alert must never break the stream
            log.exception("whale watch listener failed")

    def _trade(self, d: dict) -> None:
        sym = d.get("s", "")
        side = "sell" if d.get("m") else "buy"          # buyer is maker: the taker SOLD
        px, qty, t = float(d["p"]), float(d["q"]), int(d["T"])
        p = self._pending.get(sym)
        if p and p["side"] == side and t - p["last"] <= MERGE_MS:
            p["qty"] += qty
            p["usd"] += px * qty
            p["last"] = t
            p["n"] += 1
            return
        if p:
            self._flush(sym)
        self._pending[sym] = {"side": side, "qty": qty, "usd": px * qty, "first": t, "last": t, "n": 1}

    def _flush(self, sym: str) -> None:
        p = self._pending.pop(sym, None)
        if p and p["usd"] >= self.thresholds.get(sym, MIN_USD):
            self.trades.append({"ts": p["first"], "symbol": sym, "side": p["side"], "usd": p["usd"],
                                "price": p["usd"] / p["qty"], "qty": p["qty"], "fills": p["n"]})
            self._remember(sym, "trades", self.trades[-1])
            self._log(self.flow_log, (self.clock() * 1000, sym, p["side"] == "buy", p["usd"], p["usd"] / p["qty"]))
            self._call(self.on_event, "whale", self.trades[-1])

    def sweep(self, now_ms: float) -> None:
        for sym, p in list(self._pending.items()):
            if now_ms - p["last"] > 2 * MERGE_MS:
                self._flush(sym)

    def _liq(self, o: dict) -> None:
        qty = float(o.get("z") or o.get("q") or 0)
        px = float(o.get("ap") or o.get("p") or 0)
        if qty <= 0 or px <= 0:
            return
        # a SELL liquidation order closes a LONG position (and a BUY closes a short)
        self.liqs.append({"ts": int(o.get("T") or self.clock() * 1000), "symbol": o.get("s", ""),
                          "side": "long" if o.get("S") == "SELL" else "short", "usd": px * qty, "price": px, "qty": qty})
        self._remember(self.liqs[-1]["symbol"], "liqs", self.liqs[-1])
        self._log(self.liq_log, (self.clock() * 1000, o.get("S") == "SELL", px * qty, o.get("s", ""), px))
        self._call(self.on_event, "liq", self.liqs[-1])

    # ---- what the page shows -------------------------------------------------------------------
    def markers(self, symbol: str) -> dict:
        """This coin's whale orders and liquidations still in memory, oldest first: the chart's markers."""
        h = self.history.get(symbol, {})
        return {"symbol": symbol, "trades": list(h.get("trades", [])), "liqs": list(h.get("liqs", []))}

    @staticmethod
    def clamp_window(window_s) -> float:
        """A timeframe in seconds, kept between 1 second and 1 week; anything unusable means the default hour."""
        try:
            w = float(window_s)
        except (TypeError, ValueError):
            return FLOW_WINDOW_S
        return min(max(w, 1.0), MAX_WINDOW_S) if math.isfinite(w) else FLOW_WINDOW_S

    def _flow(self, window_s: float) -> tuple[list[dict], dict]:
        """Whale buying vs selling per coin, and liquidations, over the last window_s seconds (newest log entries first)."""
        since = self.clock() * 1000 - window_s * 1000
        flow: dict[str, dict] = {}
        for ts, sym, is_buy, usd, _px in reversed(self.flow_log):
            if ts < since:
                break
            f = flow.setdefault(sym, {"symbol": sym, "buy": 0.0, "sell": 0.0, "n": 0})
            f["buy" if is_buy else "sell"] += usd
            f["n"] += 1
        rows = sorted(flow.values(), key=lambda f: -abs(f["buy"] - f["sell"]))[:TOP_N]
        for f in rows:
            f["net"] = f["buy"] - f["sell"]
        liq = {"long": 0.0, "short": 0.0}
        for ts, is_long, usd, _sym, _px in reversed(self.liq_log):
            if ts < since:
                break
            liq["long" if is_long else "short"] += usd
        return rows, liq

    def view(self, window_s: float = FLOW_WINDOW_S) -> dict:
        w = self.clamp_window(window_s)
        rows, liq = self._flow(w)
        return {"status": self.status, "error": self.error, "coins": len(self.thresholds), "min_usd": MIN_USD,
                "thresholds": dict(sorted(self.thresholds.items(), key=lambda kv: -kv[1])[:10]),
                "trades": list(self.trades)[::-1][:150], "liqs": list(self.liqs)[::-1][:150], "flow": rows,
                "window_s": w, "covered_s": min(w, self.clock() - self._born),   # < window_s: the bot has not watched that long yet
                "liq": liq, "liq_1h": self._flow(FLOW_WINDOW_S)[1] if w != FLOW_WINDOW_S else liq,
                "signals": self.signals(w)}

    # ---- a trade idea from what the whales did -----------------------------------------------------
    def signals(self, window_s: float = FLOW_WINDOW_S) -> list[dict]:
        """Per coin, a long or short idea read from the whale orders and liquidations of the last window_s seconds.

        A heuristic, not a forecast, and the bot never trades on it:
        * pressure = (whale buys + shorts liquidated - whale sells - longs liquidated) / all of it, -1 .. +1
          (a liquidated short is a forced BUY, a liquidated long a forced SELL). LONG at +SIG_PRESSURE or more,
          SHORT at -SIG_PRESSURE or less, otherwise WAIT.
        * entry: the whales' own average price on the leading side (their VWAP), or the mark if that is better,
          i.e. buy a pullback to where the whales bought (sell a bounce to where they sold).
        * stop: SIG_STOP_SIGMA times the spread of the whales' prices beyond the entry (floor SIG_MIN_RISK).
        * target: the other side's VWAP when it lies at least SIG_MIN_RR R away, else SIG_RR R.
        """
        since = self.clock() * 1000 - self.clamp_window(window_s) * 1000
        per: dict[str, dict] = {}
        for ts, sym, is_buy, usd, px in reversed(self.flow_log):
            if ts < since:
                break
            c = per.setdefault(sym, {"buy": 0.0, "sell": 0.0, "bq": 0.0, "sq": 0.0, "n": 0, "w": 0.0, "wx": 0.0,
                                     "wxx": 0.0, "last": px, "liq_long": 0.0, "liq_short": 0.0})
            if is_buy:
                c["buy"] += usd; c["bq"] += usd / px
            else:
                c["sell"] += usd; c["sq"] += usd / px
            c["n"] += 1
            c["w"] += usd; c["wx"] += usd * px; c["wxx"] += usd * px * px
        for ts, is_long, usd, sym, _px in reversed(self.liq_log):
            if ts < since:
                break
            if sym in per:
                per[sym]["liq_long" if is_long else "liq_short"] += usd
        out = []
        for sym, c in per.items():
            if c["n"] < SIG_MIN_ORDERS:
                continue
            mark = (self.marks.get(sym) or (c["last"], 0.0))[0]
            up = c["buy"] + c["liq_short"]
            down = c["sell"] + c["liq_long"]
            pressure = (up - down) / (up + down)
            buy_vwap = c["buy"] / c["bq"] if c["bq"] else None
            sell_vwap = c["sell"] / c["sq"] if c["sq"] else None
            mean = c["wx"] / c["w"]
            sigma = math.sqrt(max(c["wxx"] / c["w"] - mean * mean, 0.0))
            sigma = min(max(sigma, mark * SIG_MIN_RISK), mark * SIG_MAX_RISK)
            row = {"symbol": sym, "mark": mark, "pressure": round(pressure, 3), "n": c["n"], "buy": c["buy"],
                   "sell": c["sell"], "liq_long": c["liq_long"], "liq_short": c["liq_short"],
                   "buy_vwap": buy_vwap, "sell_vwap": sell_vwap, "bias": "wait",
                   "entry": None, "stop": None, "target": None, "rr": None}
            if abs(pressure) >= SIG_PRESSURE:
                long = pressure > 0
                anchor = buy_vwap if long else sell_vwap
                entry = (min(mark, anchor) if long else max(mark, anchor)) if anchor else mark
                risk = SIG_STOP_SIGMA * sigma
                stop = entry - risk if long else entry + risk
                other = sell_vwap if long else buy_vwap
                far = other is not None and ((other - entry) if long else (entry - other)) >= SIG_MIN_RR * risk
                target = other if far else (entry + SIG_RR * risk if long else entry - SIG_RR * risk)
                if not long and target <= 0:
                    continue
                confirm = c["liq_short"] if long else c["liq_long"]
                conf = (abs(pressure) * 55 + min(c["n"], 20) / 20 * 30
                        + 15 * min(confirm / (up + down) / 0.1, 1.0))      # liquidations the same way: full at 10%
                row.update(bias="long" if long else "short", entry=entry, stop=stop, target=target,
                           rr=abs(target - entry) / risk, confidence=round(min(conf, 100.0)),
                           to_entry_pct=(entry / mark - 1) * 100)
            else:
                row["confidence"] = 0
            out.append(row)
        out.sort(key=lambda r: (r["bias"] == "wait", -r["confidence"], -(r["buy"] + r["sell"])))
        return out[:TOP_N]

    def levels(self, symbol: str, long: bool, window_s: float = FLOW_WINDOW_S) -> dict | None:
        """The whales' price levels on one coin over the window, for a trade on one side (arbbot/signals.py):
        anchor = the VWAP of the side that trade joins (buys for a long), other = the opposite side's VWAP,
        risk = SIG_STOP_SIGMA times the spread of all their prices (None when there are no whale orders)."""
        since = self.clock() * 1000 - self.clamp_window(window_s) * 1000
        usd = {True: 0.0, False: 0.0}
        qty = {True: 0.0, False: 0.0}
        w = wx = wxx = 0.0
        for ts, sym, is_buy, u, px in reversed(self.flow_log):
            if ts < since:
                break
            if sym != symbol:
                continue
            usd[is_buy] += u
            qty[is_buy] += u / px
            w += u; wx += u * px; wxx += u * px * px
        if not w:
            return None
        mean = wx / w
        vwap = {side: usd[side] / qty[side] if qty[side] else None for side in (True, False)}
        return {"anchor": vwap[long], "other": vwap[not long],
                "risk": SIG_STOP_SIGMA * math.sqrt(max(wxx / w - mean * mean, 0.0))}

    # ---- the connection --------------------------------------------------------------------------
    async def run(self) -> None:
        """Runs for the life of the bot and never raises: a dropped stream or an unreachable Binance only shows as
        "down" on the page while it reconnects with backoff."""
        backoff = 2.0
        while True:
            try:
                if not self.thresholds or self.clock() - self._ranked >= RERANK_S:
                    await self.rank()
                async with self.session.ws_connect(self.url(), heartbeat=20, receive_timeout=60) as ws:
                    self.status, self.error, backoff = "live", "", 2.0
                    log.info("whale watch: streaming big trades on %d coins and all liquidations", len(self.thresholds))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.handle(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        if self.clock() - self._ranked >= RERANK_S:
                            break                         # reconnect with a fresh list of coins
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # noqa: BLE001 - must never end the bot
                msg = f"{type(e).__name__}: {str(e)[:160]}"
                if msg != self.error:
                    log.warning("whale watch down: %s; retrying", msg)
                self.status, self.error = "down", msg
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)
