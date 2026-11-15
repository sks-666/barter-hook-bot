"""The market panel's read-only Binance relay (arbbot/market.py) and its /api/market routes."""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from arbbot.binance_client import BinanceError
from arbbot.market import TTL, MarketData
from arbbot.server import Dashboard

KLINE = [1_700_000_000_000, "100.0", "101.5", "99.5", "101.0", "12.5", 1_700_003_599_999, "1262.5", 10, "6", "606", "0"]


class FakeBinance:
    """Answers the public endpoints the relay uses and records every call."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail = None

    async def get(self, venue, path, params=None, timeout=None):
        assert venue == "fapi"
        self.calls.append((path, dict(params or {})))
        await asyncio.sleep(0)
        if self.fail:
            raise self.fail
        p = params or {}
        if path == "/fapi/v1/klines":
            return [KLINE] * 3
        if path == "/fapi/v1/depth":
            return {"bids": [["100.0", "2"], ["99.9", "1"]], "asks": [["100.1", "3"]], "E": 1}
        if path == "/fapi/v1/trades":
            return [{"price": "100.0", "quoteQty": "200.0", "time": 1, "isBuyerMaker": True},
                    {"price": "100.1", "quoteQty": "50.0", "time": 2, "isBuyerMaker": False}]
        if path == "/fapi/v1/ticker/24hr" and "symbol" in p:
            return {"lastPrice": "100", "priceChangePercent": "1.5", "highPrice": "105", "lowPrice": "95", "volume": "1000",
                    "quoteVolume": "100000"}
        if path == "/fapi/v1/ticker/24hr":
            return [{"symbol": s, "lastPrice": "1", "priceChangePercent": str(c), "quoteVolume": str(v)} for s, c, v in
                    [("AAAUSDT", 12, 5e7), ("BBBUSDT", -8, 9e7), ("CCCUSDT", 3, 3e8), ("THINUSDT", 90, 1e5),
                     ("BTCUSDT_261225", 50, 9e9), ("DDDUSDC", 40, 9e9)]]
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": s, "baseAsset": s[:-4], "quoteAsset": "USDT", "contractType": c, "status": st}
                                for s, c, st in [("AAAUSDT", "PERPETUAL", "TRADING"), ("NVDAUSDT", "TRADIFI_PERPETUAL", "TRADING"),
                                                 ("OLDUSDT", "PERPETUAL", "SETTLING"),
                                                 ("BTCUSDT_261225", "CURRENT_QUARTER", "TRADING")]]}
        if path == "/fapi/v1/premiumIndex":
            return {"markPrice": "100.05", "indexPrice": "100.02", "lastFundingRate": "0.0001", "nextFundingTime": 9}
        if path == "/fapi/v1/openInterest":
            return {"openInterest": "50"}
        if path == "/futures/data/globalLongShortAccountRatio":
            return [{"timestamp": 10, "longAccount": "0.6"}, {"timestamp": 20, "longAccount": "0.55"}]
        if path == "/futures/data/takerlongshortRatio":
            return [{"timestamp": 20, "buySellRatio": "1.2"}]
        if path in ("/futures/data/topLongShortPositionRatio", "/futures/data/topLongShortAccountRatio"):
            return [{"timestamp": 20, "longAccount": "0.7" if "Position" in path else "0.65"}]
        if path == "/futures/data/openInterestHist":
            return [{"timestamp": 20, "sumOpenInterestValue": "5000000"}]
        raise AssertionError(f"unexpected {path}")


class MarketDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = [0.0]
        self.api = FakeBinance()
        self.m = MarketData(self.api, clock=lambda: self.now[0])

    async def test_shapes(self):
        k = await self.m.klines("btcusdt", "1h")
        self.assertEqual(k[0], [1_700_000_000_000, 100.0, 101.5, 99.5, 101.0, 12.5, 1262.5, 606.0])  # + taker buy quote
        self.assertEqual(self.api.calls[0], ("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 300}))
        d = await self.m.depth("BTCUSDT")
        self.assertEqual((d["bids"][0], d["asks"][0]), ([100.0, 2.0], [100.1, 3.0]))
        t = await self.m.trades("BTCUSDT")
        self.assertEqual([(x["price"], x["sell"]) for x in t], [(100.1, False), (100.0, True)])   # newest first
        i = await self.m.info("BTCUSDT")
        self.assertEqual((i["mark"], i["funding_rate"], i["open_interest_usd"]), (100.05, 0.0001, 5000.0))
        data = await self.m.trading_data("BTCUSDT")
        self.assertEqual(data["rows"][0]["ts"], 20)
        self.assertAlmostEqual(data["rows"][0]["long_pct"], 55.0)
        self.assertEqual((data["rows"][0]["taker_ratio"], data["rows"][0]["oi_usd"]), (1.2, 5e6))
        self.assertAlmostEqual(data["rows"][0]["top_pos_long_pct"], 70.0)      # the top traders: whales
        self.assertAlmostEqual(data["rows"][0]["top_acc_long_pct"], 65.0)
        self.assertEqual(data["rows"][1], {"ts": 10, "long_pct": 60.0})
        self.assertEqual(data["period"], "1h")
        sent = [c for c in self.api.calls if c[0].startswith("/futures/data/")]
        self.assertTrue(sent and all(c[1] == {"symbol": "BTCUSDT", "period": "1h", "limit": 24} for c in sent))

    async def test_trading_data_takes_binance_steps_only(self):
        d = await self.m.trading_data("BTCUSDT", "4h")
        self.assertEqual(d["period"], "4h")
        self.assertTrue(all(c[1]["period"] == "4h" for c in self.api.calls if c[0].startswith("/futures/data/")))
        n = len(self.api.calls)
        await self.m.trading_data("BTCUSDT", "4h")                         # cached per period
        self.assertEqual(len(self.api.calls), n)
        await self.m.trading_data("BTCUSDT", "15m")                        # another period is another request
        self.assertGreater(len(self.api.calls), n)
        for bad in ("1s", "1m", "2w", "3h", "", "1H"):                     # Binance has no such step
            n = len(self.api.calls)
            with self.assertRaises(ValueError):
                await self.m.trading_data("BTCUSDT", bad)
            self.assertEqual(len(self.api.calls), n)

    async def test_weekly_rows_are_built_from_daily_ones(self):
        DAY = 86_400_000
        mon = 1_793_577_600_000                                             # Mon 2026-11-02 00:00 UTC
        days = [mon - 2 * DAY, mon - DAY, mon, mon + DAY, mon + 5 * DAY]    # Sat, Sun | Mon, Tue, Sat (this week)
        asked = []

        class Stats:
            async def get(self, venue, path, params=None, timeout=None):
                asked.append(dict(params))
                if path.endswith("takerlongshortRatio"):
                    return [{"timestamp": t, "buySellRatio": "9", "buyVol": str(10 * (i + 1)), "sellVol": "10"}
                            for i, t in enumerate(days)]
                if path.endswith("openInterestHist"):
                    return [{"timestamp": t, "sumOpenInterestValue": str(1000 + i)} for i, t in enumerate(days)]
                return [{"timestamp": t, "longAccount": str(0.5 + i / 100)} for i, t in enumerate(days)]

        d = await MarketData(self.api, clock=lambda: 0.0, stats_client=Stats()).trading_data("BTCUSDT", "1w")
        self.assertTrue(all(q["period"] == "1d" and q["limit"] == 31 for q in asked))   # Binance has no 1w step
        self.assertEqual([r["ts"] for r in d["rows"]], [mon, mon - 7 * DAY])           # newest week first
        this, last = d["rows"]
        self.assertAlmostEqual(this["long_pct"], 54.0)                                 # the week's last day (Sat)
        self.assertEqual(this["oi_usd"], 1004.0)
        self.assertAlmostEqual(this["taker_ratio"], (30 + 40 + 50) / 30)                # summed volumes, not the "9"
        self.assertAlmostEqual(last["long_pct"], 51.0)                                 # Sunday closed last week
        self.assertAlmostEqual(last["taker_ratio"], (10 + 20) / 20)
        self.assertFalse(any(k.startswith("_") for r in d["rows"] for k in r))         # no working fields leak out

    async def test_movers_keep_liquid_usdt_perpetuals_only(self):
        mv = await self.m.movers()
        self.assertEqual([r["symbol"] for r in mv["gainers"]], ["AAAUSDT", "CCCUSDT", "BBBUSDT"])
        self.assertEqual(mv["losers"][0]["symbol"], "BBBUSDT")
        self.assertEqual(mv["volume"][0]["symbol"], "CCCUSDT")

    async def test_symbols_include_stock_and_metal_perpetuals_but_not_settling_or_dated(self):
        self.assertEqual(sorted(r["symbol"] for r in await self.m.symbols()), ["AAAUSDT", "NVDAUSDT"])

    async def test_a_coin_the_exchange_does_not_list_gets_a_plain_message(self):
        self.api.env = "testnet"
        self.api.fail = BinanceError(400, -1121, "Invalid symbol.", "/fapi/v1/depth")
        with self.assertRaises(ValueError) as ctx:
            await self.m.depth("SPCXUSDT", 100)
        self.assertIn("SPCXUSDT is not listed on the Binance testnet futures exchange", str(ctx.exception))
        self.assertIn("mainnet only", str(ctx.exception))
        self.api.fail = BinanceError(429, -1003, "Too many requests.", "/fapi/v1/depth")
        with self.assertRaises(BinanceError):                     # any other Binance error passes through as it was
            await self.m.depth("BTCUSDT")

    async def test_bad_input_never_reaches_binance(self):
        for bad in ("", "BTC/USDT", "BTCUSDC", "../x", "A" * 40 + "USDT"):
            with self.assertRaises(ValueError):
                await self.m.depth(bad)
        with self.assertRaises(ValueError):
            await self.m.klines("BTCUSDT", "7m")
        self.assertEqual(self.api.calls, [])

    async def test_answers_are_cached_and_concurrent_asks_share_one_request(self):
        await asyncio.gather(*(self.m.depth("BTCUSDT") for _ in range(5)))
        self.assertEqual(len(self.api.calls), 1)
        await self.m.depth("BTCUSDT")
        self.assertEqual(len(self.api.calls), 1)                  # still fresh
        self.now[0] += TTL["depth"] + 0.01
        await self.m.depth("BTCUSDT")
        self.assertEqual(len(self.api.calls), 2)                  # expired: asked again
        await self.m.depth("ETHUSDT")
        self.assertEqual(len(self.api.calls), 3)                  # another market is another answer

    async def test_a_failure_is_not_cached(self):
        self.api.fail = RuntimeError("down")
        with self.assertRaises(RuntimeError):
            await self.m.trades("BTCUSDT")
        self.api.fail = None
        self.assertEqual(len(await self.m.trades("BTCUSDT")), 2)


DAY = 86_400_000
NOW_MS = 1_794_000_000_000


class CurveBinance:
    """The endpoints of the futures curve and the history export."""

    def __init__(self, quarterlies=True, listed_ms=None):
        self.calls: list[tuple[str, dict]] = []
        self.quarterlies, self.listed_ms = quarterlies, listed_ms

    async def get(self, venue, path, params=None, timeout=None):
        assert venue == "fapi"
        p = dict(params or {})
        self.calls.append((path, p))
        if path == "/fapi/v1/exchangeInfo":
            syms = [{"symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "deliveryDate": 4133404800000}]
            if self.quarterlies:
                syms += [{"symbol": "BTCUSDT_261225", "pair": "BTCUSDT", "contractType": "CURRENT_QUARTER", "status": "TRADING",
                          "deliveryDate": NOW_MS + 50 * DAY},
                         {"symbol": "BTCUSDT_270326", "pair": "BTCUSDT", "contractType": "NEXT_QUARTER", "status": "TRADING",
                          "deliveryDate": NOW_MS + 140 * DAY},
                         {"symbol": "BTCUSDT_260925", "pair": "BTCUSDT", "contractType": "CURRENT_QUARTER", "status": "SETTLING",
                          "deliveryDate": NOW_MS - DAY}]
            syms.append({"symbol": "ETHUSDT", "pair": "ETHUSDT", "contractType": "PERPETUAL", "status": "TRADING"})
            return {"symbols": syms}
        if path == "/fapi/v1/premiumIndex":
            mark = {"BTCUSDT": "100.0", "BTCUSDT_261225": "101.0", "BTCUSDT_270326": "102.0"}[p["symbol"]]
            return {"markPrice": mark, "indexPrice": "100.0", "lastFundingRate": "0.0001" if p["symbol"] == "BTCUSDT" else "0"}
        if path == "/fapi/v1/fundingRate":
            return [{"fundingTime": NOW_MS - i * 4 * 3_600_000, "fundingRate": "0.0001"} for i in range(5)]   # every 4h
        if path == "/futures/data/basis":
            b = {"PERPETUAL": "0.1", "CURRENT_QUARTER": "1", "NEXT_QUARTER": "2"}[p["contractType"]]
            return [{"timestamp": NOW_MS - i * DAY, "basis": b, "indexPrice": "100"} for i in (1, 0)]
        if path == "/fapi/v1/klines":
            step, start = DAY, max(p["startTime"], self.listed_ms or 0)
            n = min(p["limit"], max(0, (NOW_MS - start) // step + 1))
            return [[start + i * step, "1", "2", "0.5", "1.5", "10", start + i * step + step - 1, "15", 3, "4", "6", "0"]
                    for i in range(n)]
        raise AssertionError(f"unexpected {path}")


class CurveAndHistoryTests(unittest.IsolatedAsyncioTestCase):
    def market(self, api):
        return MarketData(api, clock=lambda: 0.0, wall=lambda: NOW_MS / 1000)

    async def test_the_curve_prices_each_contract_against_the_index(self):
        api = CurveBinance()
        c = await self.market(api).curve("btcusdt", "1d")
        perp, cq, nq = c["points"]
        self.assertEqual([p["type"] for p in c["points"]], ["PERPETUAL", "CURRENT_QUARTER", "NEXT_QUARTER"])
        self.assertEqual(cq["symbol"], "BTCUSDT_261225")                         # the settling one is left out
        self.assertAlmostEqual(cq["basis_pct"], 1.0)
        self.assertAlmostEqual(cq["days"], 50.0)
        self.assertAlmostEqual(cq["ann_pct"], 1.0 * 365 / 50)                    # basis per year to its expiry
        self.assertAlmostEqual(nq["ann_pct"], 2.0 * 365 / 140)
        self.assertEqual(c["funding_interval_h"], 4.0)                           # read from the funding times
        self.assertAlmostEqual(perp["ann_pct"], 0.01 * 6 * 365)                   # 0.01% x 6 fundings a day x 365
        self.assertEqual([r["ts"] for r in c["history"]["NEXT_QUARTER"]], [NOW_MS - DAY, NOW_MS])   # oldest first
        self.assertAlmostEqual(c["history"]["NEXT_QUARTER"][0]["basis_pct"], 2.0)
        self.assertEqual(len(c["funding"]), 5)
        n = len(api.calls)
        await self.market(api).curve("BTCUSDT", "1d")                            # a new MarketData: no cache
        m = self.market(api)
        await m.curve("BTCUSDT", "4h")
        k = len(api.calls)
        await m.curve("BTCUSDT", "4h")
        self.assertEqual(len(api.calls), k)                                     # cached
        self.assertGreater(n, 0)

    async def test_a_coin_without_quarterlies_has_the_perpetual_only(self):
        c = await self.market(CurveBinance(quarterlies=False)).curve("BTCUSDT")
        self.assertEqual([p["type"] for p in c["points"]], ["PERPETUAL"])
        self.assertEqual(list(c["history"]), ["PERPETUAL"])

    async def test_bad_curve_input_never_reaches_binance(self):
        api = CurveBinance()
        for sym, period in (("BTC/USDT", "1d"), ("BTCUSDT", "1w"), ("BTCUSDT", "3h")):
            with self.assertRaises(ValueError):
                await self.market(api).curve(sym, period)
        self.assertEqual(api.calls, [])
        with self.assertRaises(ValueError):                                     # no perpetual: says so
            await self.market(api).curve("SOLUSDT")

    async def test_history_pages_through_years_and_stops_at_a_coins_listing(self):
        api = CurveBinance()
        h = await self.market(api).history(["BTCUSDT"], "1d", 5)
        rows = h["BTCUSDT"]
        self.assertEqual(len(rows), 5 * 365 + 1)
        self.assertEqual([c[1]["limit"] for c in api.calls], [1000, 1000])        # two pages of 1000
        self.assertEqual(rows[1][0] - rows[0][0], DAY)
        self.assertEqual(rows[0][1:], [1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 6.0])
        young = CurveBinance(listed_ms=NOW_MS - 9 * DAY)
        self.assertEqual(len((await self.market(young).history(["ETHUSDT"], "1d", 5))["ETHUSDT"]), 10)

    async def test_history_refuses_oversized_or_bad_exports(self):
        api = CurveBinance()
        m = self.market(api)
        for args in ((["BTCUSDT"], "1h", 5), ([], "1d", 1), (["BTCUSDT"], "1d", 6), (["BTCUSDT"], "1d", 0),
                     (["BTCUSDT"], "5m", 1), ([f"C{i}USDT" for i in range(7)], "1d", 1), (["nope"], "1d", 1)):
            with self.assertRaises(ValueError):
                await m.history(*args)
        self.assertEqual(api.calls, [])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeBinance()
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        self.dash = Dashboard(SimpleNamespace(swing=None), cfg, Path("web"), market=MarketData(self.api))
        server = TestServer(self.dash.make_app())
        await server.start_server()
        self.dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        self.client = TestClient(server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_routes(self):
        r = await self.client.get("/api/market/klines?symbol=BTCUSDT&interval=4h")
        self.assertEqual((r.status, len(await r.json())), (200, 3))
        r = await self.client.get("/api/market/movers")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/market/depth?symbol=nope")
        self.assertEqual(r.status, 400)
        r = await self.client.get("/api/market/whatever?symbol=BTCUSDT")
        self.assertEqual(r.status, 404)
        self.api.fail = RuntimeError("binance down")
        r = await self.client.get("/api/market/trades?symbol=ETHUSDT")
        self.assertEqual((r.status, (await r.json())["error"]), (502, "RuntimeError: binance down"))

    async def test_the_export_is_one_csv_file_for_a_basket(self):
        self.dash.market = MarketData(CurveBinance(), wall=lambda: NOW_MS / 1000)
        r = await self.client.get("/api/market/history?symbols=BTCUSDT,ETHUSDT&interval=1d&years=1")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"].split(";")[0], "text/csv")
        self.assertIn('filename="BTC-ETH_1d_1y_binance-usdm-perp.csv"', r.headers["Content-Disposition"])
        lines = (await r.text()).strip().split("\n")
        self.assertEqual(lines[0], "symbol,date_utc,open_time_ms,open,high,low,close,volume_base,volume_usdt,taker_buy_usdt")
        self.assertEqual(len(lines), 1 + 2 * 366)
        self.assertTrue(lines[1].startswith("BTCUSDT,") and lines[-1].startswith("ETHUSDT,"))
        r = await self.client.get("/api/market/history?symbols=BTCUSDT&interval=1h&years=5")
        self.assertEqual(r.status, 400)
        self.assertIn("too many rows", (await r.json())["error"])
        r = await self.client.get("/api/market/history?symbols=BTCUSDT&years=x")
        self.assertEqual(r.status, 400)
        r = await self.client.get("/api/market/curve?symbol=BTCUSDT&period=4h")
        self.assertEqual((r.status, len((await r.json())["points"])), (200, 3))

    async def test_cross_site_pages_are_refused(self):
        r = await self.client.get("/api/market/movers", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)

    async def test_without_a_relay_it_says_so(self):
        self.dash.market = None
        r = await self.client.get("/api/market/movers")
        self.assertEqual(r.status, 404)

    async def test_the_page_carries_the_panel_and_the_hosted_copy_does_not(self):
        root = Path(__file__).resolve().parent.parent
        page = (root / "web" / "index.html").read_text(encoding="utf-8")
        hosted = (root / "vercel-dashboard" / "views" / "dashboard.html").read_text(encoding="utf-8")
        for part in ('id="mk-canvas"', 'id="bk-asks"', 'id="mk-trades"', 'id="mk-movers"', "/api/market/"):
            self.assertIn(part, page)
            self.assertNotIn(part, hosted)
