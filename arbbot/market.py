"""Read-only Binance USD-M market data for the local dashboard's market panel (chart, order book, trades, movers, info).

The page never talks to Binance itself: it asks the local server, which forwards a FIXED set of public requests (no API key,
no signed endpoint, symbols and intervals checked against a whitelist) and caches each answer for a second or more, so a
dashboard left open adds a small, bounded load next to the arbitrage feed. Several open tabs share the same cached answers.
"""
import asyncio
import re
import time

from .binance_client import BinanceClient

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
# the steps Binance publishes its trader statistics in (long/short ratios, taker volume, open interest history); it keeps
# the last 30 days of them, and has nothing finer than 5 minutes or longer than a day
SENT_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w")
SENT_ROWS = 24
DAY_MS, WEEK_MS = 86_400_000, 604_800_000


def week_start(ts_ms: int) -> int:
    """Monday 00:00 UTC of the week holding ts_ms (Binance's weekly candles start then; 1970-01-01 was a Thursday)."""
    return ts_ms - ((ts_ms // DAY_MS + 3) % 7) * DAY_MS - ts_ms % DAY_MS
MOVERS_MIN_VOLUME = 20e6   # USDT of 24h volume: ignore coins too thin to matter
MOVERS_N = 10

# kind -> seconds an answer is reused. Binance weights (per call): klines 2, depth(20) 2, trades 5, 24hr(all) 40,
# exchangeInfo 1. "data" makes five /futures/data calls.
TTL = {"klines": 2.0, "depth": 1.0, "trades": 1.5, "info": 5.0, "data": 30.0, "movers": 15.0, "symbols": 60.0}


class MarketData:
    def __init__(self, client: BinanceClient, clock=time.monotonic, stats_client: BinanceClient | None = None):
        self.client, self.clock = client, clock
        # The /futures/data statistics exist on mainnet only (the testnet redirects them to its website), so a testnet bot
        # reads them from a key-less public mainnet client.
        self.stats_client = stats_client
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}

    @staticmethod
    def check_symbol(symbol: str) -> str:
        symbol = (symbol or "").strip().upper()
        if not SYMBOL_RE.match(symbol):
            raise ValueError("symbol must look like BTCUSDT")
        return symbol

    async def _cached(self, key: tuple, load):
        """The cached answer while it is fresh; otherwise one request, shared by every caller asking meanwhile."""
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < TTL[key[0]]:
            return hit[1]
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await load()
            self._cache[key] = (self.clock(), value)
            if len(self._cache) > 500:                    # many symbols browsed: drop the oldest answers
                for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:250]:
                    del self._cache[k]
            fut.set_result(value)
            return value
        except asyncio.CancelledError:                    # the page went away mid-request: waiters just try again
            fut.cancel()
            raise
        except Exception as e:
            fut.set_exception(e)
            fut.exception()                               # mark retrieved: no "never retrieved" warning
            raise
        finally:
            del self._inflight[key]

    def _get(self, path: str, params: dict | None = None):
        return self.client.get("fapi", path, params, timeout=8.0)

    async def klines(self, symbol: str, interval: str) -> list[list]:
        symbol = self.check_symbol(symbol)
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(INTERVALS)}")

        async def load():
            rows = await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": 300})
            # [open time, open, high, low, close, base volume, quote volume, taker-BUY quote volume (for buy/sell delta)]
            return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]), float(r[10])]
                    for r in rows]
        return await self._cached(("klines", symbol, interval), load)

    async def depth(self, symbol: str, limit: int = 20) -> dict:
        """Top `limit` levels a side: 20 (weight 2) for the ladder, or 100 (weight 5) to find the walls."""
        symbol = self.check_symbol(symbol)
        if limit not in (20, 100):
            raise ValueError("limit must be 20 or 100")

        async def load():
            d = await self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
            side = lambda rows: [[float(p), float(q)] for p, q in rows]  # noqa: E731
            return {"bids": side(d["bids"]), "asks": side(d["asks"]), "ts": d.get("E") or d.get("T")}
        return await self._cached(("depth", symbol, limit), load)

    async def trades(self, symbol: str) -> list[dict]:
        symbol = self.check_symbol(symbol)

        async def load():
            rows = await self._get("/fapi/v1/trades", {"symbol": symbol, "limit": 40})
            # isBuyerMaker: the seller hit the bid, i.e. an aggressive SELL (shown red)
            return [{"price": float(r["price"]), "usd": float(r["quoteQty"]), "ts": int(r["time"]),
                     "sell": bool(r["isBuyerMaker"])} for r in reversed(rows)]
        return await self._cached(("trades", symbol), load)

    async def info(self, symbol: str) -> dict:
        symbol = self.check_symbol(symbol)

        async def load():
            t, p, oi = await asyncio.gather(
                self._get("/fapi/v1/ticker/24hr", {"symbol": symbol}),
                self._get("/fapi/v1/premiumIndex", {"symbol": symbol}),
                self._get("/fapi/v1/openInterest", {"symbol": symbol}))
            last = float(t["lastPrice"])
            return {"symbol": symbol, "last": last, "change_pct": float(t["priceChangePercent"]),
                    "high": float(t["highPrice"]), "low": float(t["lowPrice"]), "volume": float(t["volume"]),
                    "quote_volume": float(t["quoteVolume"]), "mark": float(p["markPrice"]), "index": float(p["indexPrice"]),
                    "funding_rate": float(p["lastFundingRate"]), "next_funding": int(p["nextFundingTime"]),
                    "open_interest": float(oi["openInterest"]), "open_interest_usd": float(oi["openInterest"]) * last}
        return await self._cached(("info", symbol), load)

    async def trading_data(self, symbol: str, period: str = "1h") -> dict:
        """Sentiment over the last SENT_ROWS periods (one row per `period`, see SENT_PERIODS): all accounts long vs
        short, the TOP traders (the biggest accounts: Binance's top 20% by margin) long vs short by position size and by
        account, taker buy vs sell, and open interest.

        Binance has no weekly step, so "1w" is built from its daily rows (it keeps 31 days, so about 5 weeks): each
        week shows the shares and open interest at its last day, like a weekly candle's close, and the taker ratio of
        the week's summed buy and sell volume. The newest week is the one in progress."""
        symbol = self.check_symbol(symbol)
        if period not in SENT_PERIODS:
            raise ValueError(f"period must be one of {', '.join(SENT_PERIODS)}")

        stats = self.stats_client or self.client

        async def load():
            weekly = period == "1w"
            q = {"symbol": symbol, "period": "1d" if weekly else period, "limit": 31 if weekly else SENT_ROWS}
            get = lambda path: stats.get("fapi", path, q, timeout=8.0)  # noqa: E731
            ls, taker, oi, top_pos, top_acc = await asyncio.gather(
                get("/futures/data/globalLongShortAccountRatio"), get("/futures/data/takerlongshortRatio"),
                get("/futures/data/openInterestHist"), get("/futures/data/topLongShortPositionRatio"),
                get("/futures/data/topLongShortAccountRatio"))
            rows: dict[int, dict] = {}
            for r in ls:
                rows.setdefault(int(r["timestamp"]), {})["long_pct"] = float(r["longAccount"]) * 100
            for r in top_pos:                             # Binance names it longAccount here too: the long share of size
                rows.setdefault(int(r["timestamp"]), {})["top_pos_long_pct"] = float(r["longAccount"]) * 100
            for r in top_acc:
                rows.setdefault(int(r["timestamp"]), {})["top_acc_long_pct"] = float(r["longAccount"]) * 100
            for r in taker:
                row = rows.setdefault(int(r["timestamp"]), {})
                row["taker_ratio"] = float(r["buySellRatio"])
                if weekly:
                    row["_buy"], row["_sell"] = float(r.get("buyVol") or 0), float(r.get("sellVol") or 0)
            for r in oi:
                rows.setdefault(int(r["timestamp"]), {})["oi_usd"] = float(r["sumOpenInterestValue"])
            if weekly:
                rows = self._weeks(rows)
            return {"symbol": symbol, "source": getattr(stats, "env", "mainnet"), "period": period,
                    "rows": [dict(ts=ts, **v) for ts, v in sorted(rows.items(), reverse=True)]}
        return await self._cached(("data", symbol, period), load)

    @staticmethod
    def _weeks(days: dict[int, dict]) -> dict[int, dict]:
        """Daily rows -> weekly rows keyed by Monday 00:00 UTC (see trading_data)."""
        weeks: dict[int, dict] = {}
        for ts, d in sorted(days.items()):                  # oldest first: each week ends up with its last day's values
            w = weeks.setdefault(week_start(ts), {"_buy": 0.0, "_sell": 0.0})
            for k in ("long_pct", "top_pos_long_pct", "top_acc_long_pct", "oi_usd"):
                if k in d:
                    w[k] = d[k]
            w["_buy"] += d.get("_buy", 0.0)
            w["_sell"] += d.get("_sell", 0.0)
        for w in weeks.values():
            buy, sell = w.pop("_buy"), w.pop("_sell")
            if sell > 0:
                w["taker_ratio"] = buy / sell
        return weeks

    async def symbols(self) -> list[dict]:
        """Every USDT perpetual trading now, busiest first: what the terminal's search box suggests."""
        async def load():
            info, tickers = await asyncio.gather(self._get("/fapi/v1/exchangeInfo"), self._get("/fapi/v1/ticker/24hr"))
            t = {r["symbol"]: r for r in tickers}
            rows = []
            for s in info.get("symbols", []):
                sym = s.get("symbol", "")
                if (s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT"
                        or not SYMBOL_RE.match(sym)):
                    continue
                r = t.get(sym, {})
                rows.append({"symbol": sym, "base": s.get("baseAsset") or sym[:-4],
                             "last": float(r.get("lastPrice") or 0), "change_pct": float(r.get("priceChangePercent") or 0),
                             "quote_volume": float(r.get("quoteVolume") or 0)})
            rows.sort(key=lambda r: -r["quote_volume"])
            return rows
        return await self._cached(("symbols",), load)

    async def movers(self) -> dict:
        async def load():
            rows = await self._get("/fapi/v1/ticker/24hr")
            liquid = [{"symbol": r["symbol"], "last": float(r["lastPrice"]), "change_pct": float(r["priceChangePercent"]),
                       "quote_volume": float(r["quoteVolume"])} for r in rows
                      if SYMBOL_RE.match(r["symbol"]) and float(r["quoteVolume"]) >= MOVERS_MIN_VOLUME]
            liquid.sort(key=lambda r: r["change_pct"])
            return {"gainers": liquid[::-1][:MOVERS_N], "losers": liquid[:MOVERS_N],
                    "volume": sorted(liquid, key=lambda r: -r["quote_volume"])[:MOVERS_N]}
        return await self._cached(("movers",), load)
