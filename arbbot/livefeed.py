"""Live market data for the terminal: the loaded coin's trades, forming candle, 24h ticker and mark price pushed to the
page over a WebSocket (/ws/live) as Binance sends them, instead of the page asking every few seconds.

One Binance connection per coin (shared by every page showing it), on the bot's own exchange (mainnet or testnet, the
same one the chart's REST data comes from), on the /market path: aggTrade, every kline interval, the 24h ticker and the
mark price every second. Updates go out to the pages at most every FLUSH_S (a burst of trades is one message). A coin
nobody shows any more is dropped after IDLE_S. Read-only; no REST weight at all.
"""
import asyncio
import json
import logging
import time

log = logging.getLogger("livefeed")

HOSTS = {"mainnet": "wss://fstream.binance.com", "testnet": "wss://fstream.binancefuture.com"}
INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
FLUSH_S = 0.1                # pages get at most 10 updates a second
IDLE_S = 30.0                # a coin with no page for this long: its Binance connection closes
MAX_TRADES = 40              # trades per update (the tape shows 40)


def streams(sym: str) -> list[str]:
    s = sym.lower()
    return [f"{s}@aggTrade", f"{s}@ticker", f"{s}@markPrice@1s"] + [f"{s}@kline_{iv}" for iv in INTERVALS]


def convert(msg: dict) -> tuple[str, object] | None:
    """One Binance event -> (kind, value) in the terminal's own shapes (see arbbot/market.py), or None."""
    e = msg.get("e")
    if e == "aggTrade":
        p, q = float(msg["p"]), float(msg["q"])
        return "t", {"price": p, "usd": round(p * q, 2), "ts": int(msg["T"]), "sell": bool(msg["m"])}
    if e == "kline":
        k = msg["k"]
        return "k:" + k["i"], [int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]),
                               float(k["q"]), float(k["Q"])]
    if e == "24hrTicker":
        return "tk", {"last": float(msg["c"]), "change_pct": float(msg["P"]), "high": float(msg["h"]),
                      "low": float(msg["l"]), "volume": float(msg["v"]), "quote_volume": float(msg["q"])}
    if e == "markPriceUpdate":
        return "mk", {"mark": float(msg["p"]), "index": float(msg["i"]), "funding_rate": float(msg["r"]),
                      "next_funding": int(msg["T"])}
    return None


class Hub:
    """One coin: the Binance connection and what has arrived since the last flush."""

    def __init__(self, sym: str, url: str, session):
        self.sym, self.url, self.session = sym, url, session
        self.pages: dict = {}                    # web socket -> its candle interval
        self.trades: list[dict] = []
        self.latest: dict[str, object] = {}      # "k:5m" / "tk" / "mk" -> the newest value not yet sent
        self.idle_since: float | None = None
        self.ok, self.task = False, None

    async def run(self) -> None:
        wait = 1.0
        while True:
            try:
                async with self.session.ws_connect(self.url, heartbeat=20, receive_timeout=60) as ws:
                    self.ok, wait = True, 1.0
                    async for m in ws:
                        if m.type != 1:                  # aiohttp TEXT; CLOSE / ERROR end the connection
                            if m.type in (8, 256, 257, 258):
                                break
                            continue
                        got = convert(json.loads(m.data).get("data") or {})
                        if got is None:
                            continue
                        kind, value = got
                        if kind == "t":
                            self.trades.append(value)
                            del self.trades[:-MAX_TRADES]
                        else:
                            self.latest[kind] = value
            except asyncio.CancelledError:
                self.ok = False
                raise
            except Exception as e:                       # noqa: BLE001 - the page keeps its 5 s polling meanwhile
                log.info("live %s: %s: %s", self.sym, type(e).__name__, e)
            self.ok = False
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30.0)

    def take(self) -> tuple[list, dict]:
        trades, latest = self.trades, self.latest
        self.trades, self.latest = [], {}
        return trades, latest


class LiveFeed:
    """The hubs, the pages attached to them, and the flush loop."""

    def __init__(self, session, env: str = "mainnet", clock=time.monotonic):
        self.session, self.clock = session, clock
        self.base = HOSTS.get(env, HOSTS["mainnet"]) + "/market/stream?streams="
        self.hubs: dict[str, Hub] = {}
        self._flusher: asyncio.Task | None = None

    def attach(self, ws, sym: str, interval: str) -> None:
        self.detach(ws)
        hub = self.hubs.get(sym)
        if hub is None:
            hub = self.hubs[sym] = Hub(sym, self.base + "/".join(streams(sym)), self.session)
            hub.task = asyncio.create_task(hub.run())
        hub.pages[ws] = interval if interval in INTERVALS else "1h"
        hub.idle_since = None
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self.flush_loop())

    def detach(self, ws) -> None:
        for hub in self.hubs.values():
            if hub.pages.pop(ws, None) is not None and not hub.pages:
                hub.idle_since = self.clock()

    async def flush_loop(self) -> None:
        while self.hubs:
            await asyncio.sleep(FLUSH_S)
            now = self.clock()
            for sym, hub in list(self.hubs.items()):
                if not hub.pages:
                    if hub.idle_since is not None and now - hub.idle_since > IDLE_S:
                        hub.task.cancel()
                        del self.hubs[sym]
                    continue
                trades, latest = hub.take()
                if not trades and not latest:
                    continue
                # how late the newest trade is as it leaves the bot (Binance's trade time -> now, the bot's clock)
                lag = round(time.time() * 1000 - trades[-1]["ts"]) if trades else None
                for ws, iv in list(hub.pages.items()):
                    msg = {"sym": sym, "iv": iv}
                    if trades:
                        msg["t"] = trades[::-1]          # newest first, like /api/market/trades
                        msg["lag"] = lag
                    if "k:" + iv in latest:
                        msg["k"] = latest["k:" + iv]
                    for k in ("tk", "mk"):
                        if k in latest:
                            msg[k] = latest[k]
                    if len(msg) == 2:
                        continue
                    try:
                        await ws.send_str(json.dumps(msg))
                    except Exception:                    # noqa: BLE001 - a closed page: forget it
                        hub.pages.pop(ws, None)

    async def serve(self, ws) -> None:
        """One page: it sends {"sym": "BTCUSDT", "iv": "5m"} whenever its coin or interval changes."""
        try:
            async for m in ws:
                if m.type != 1:
                    if m.type in (8, 256, 257, 258):
                        break
                    continue
                try:
                    q = json.loads(m.data)
                except ValueError:
                    continue
                sym = str(q.get("sym") or "").upper()
                if sym.endswith("USDT") and sym.isalnum() and len(sym) <= 20:
                    self.attach(ws, sym, str(q.get("iv") or "1h"))
        finally:
            self.detach(ws)
