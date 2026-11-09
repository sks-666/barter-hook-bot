"""The whale watch (arbbot/whales.py): big trades merged from Binance's aggTrade stream, liquidations, whale flow."""
import asyncio
import unittest

from arbbot.server import Dashboard
from arbbot.whales import MERGE_MS, MIN_USD, WhaleWatch

NOW = 1_800_000_000.0


def agg(sym, price, qty, t_ms, sell=False):
    return {"stream": f"{sym.lower()}@aggTrade",
            "data": {"e": "aggTrade", "s": sym, "p": str(price), "q": str(qty), "T": t_ms, "m": sell}}


class Rest:
    async def get(self, venue, path, params=None, timeout=None):
        assert path == "/fapi/v1/ticker/24hr"
        return [{"symbol": "BTCUSDT", "quoteVolume": "15e9"}, {"symbol": "DOGEUSDT", "quoteVolume": "1e8"},
                {"symbol": "BTCUSDT_261225", "quoteVolume": "9e9"}, {"symbol": "THINUSDT", "quoteVolume": "1"}]


class WhaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [NOW]
        self.w = WhaleWatch(None, Rest(), clock=lambda: self.now[0], top_n=2)
        await self.w.rank()

    async def test_thresholds_scale_with_volume_and_never_go_below_the_floor(self):
        self.assertEqual(set(self.w.thresholds), {"BTCUSDT", "DOGEUSDT"})          # top 2 USDT perpetuals
        self.assertAlmostEqual(self.w.thresholds["BTCUSDT"], 150_000.0)
        self.assertEqual(self.w.thresholds["DOGEUSDT"], MIN_USD)
        self.assertIn("!forceOrder@arr", self.w.url())
        self.assertTrue(self.w.url().startswith("wss://fstream.binance.com/market/stream?"))   # the root sends nothing
        self.assertIn("btcusdt@aggTrade", self.w.url())

    def test_a_sweep_over_several_levels_is_one_whale_order(self):
        t = int(NOW * 1000)
        for i, px in enumerate((80_000, 80_001, 80_002)):                    # 3 x 0.7 BTC = $168K, one taker order
            self.w.handle(agg("BTCUSDT", px, 0.7, t + i * 10))
        self.w.handle(agg("BTCUSDT", 80_000, 0.01, t + 50, sell=True))         # the other side ends it
        self.assertEqual(len(self.w.trades), 1)
        o = self.w.trades[0]
        self.assertEqual((o["symbol"], o["side"], o["fills"]), ("BTCUSDT", "buy", 3))
        self.assertAlmostEqual(o["usd"], 0.7 * (80_000 + 80_001 + 80_002))

    def test_small_trades_and_slow_trickles_are_not_whales(self):
        t = int(NOW * 1000)
        self.w.handle(agg("BTCUSDT", 80_000, 1.0, t))                          # $80K: below BTC's $150K
        self.w.handle(agg("BTCUSDT", 80_000, 1.0, t + MERGE_MS + 1))           # too late to be the same order
        self.w.sweep(t + 10 * MERGE_MS)
        self.assertEqual(len(self.w.trades), 0)
        self.w.handle(agg("DOGEUSDT", 0.2, 300_000, t))                        # $60K on DOGE: above its $50K floor
        self.w.sweep(t + 10 * MERGE_MS)
        self.assertEqual([x["symbol"] for x in self.w.trades], ["DOGEUSDT"])

    def test_liquidations_and_the_hourly_flow(self):
        t = int(NOW * 1000)
        self.w.handle({"data": {"e": "forceOrder", "o": {"s": "ETHUSDT", "S": "SELL", "q": "10", "z": "10",
                                                          "ap": "3000", "p": "2990", "T": t}}})
        self.w.handle({"data": {"e": "forceOrder", "o": {"s": "XRPUSDT", "S": "BUY", "q": "1000", "z": "1000",
                                                          "ap": "2", "T": t}}})
        self.w.handle(agg("DOGEUSDT", 0.2, 500_000, t))                        # whale buys $100K
        self.w.handle(agg("DOGEUSDT", 0.2, 300_000, t + 500, sell=True))       # whale sells $60K
        self.w.sweep(t + 10_000)
        v = self.w.view()
        self.assertEqual([(x["symbol"], x["side"]) for x in v["liqs"]], [("XRPUSDT", "short"), ("ETHUSDT", "long")])
        self.assertEqual(v["liq_1h"], {"long": 30_000.0, "short": 2_000.0})
        f = v["flow"][0]
        self.assertEqual((f["symbol"], f["n"]), ("DOGEUSDT", 2))
        self.assertAlmostEqual(f["net"], 40_000.0)
        self.now[0] += 3601                                                    # an hour later: out of the flow window
        self.assertEqual(self.w.view()["flow"], [])

    def _whale(self, sym, usd, sell=False):
        """One whale order of `usd` on a $0.20 coin, seen at the fake clock's current time."""
        t = int(self.now[0] * 1000)
        self.w.handle(agg(sym, 0.2, usd / 0.2, t, sell=sell))
        self.w.sweep(t + 10_000)

    def _liq(self, sym, side, usd):
        self.w.handle({"data": {"e": "forceOrder", "o": {"s": sym, "S": side, "q": "1", "z": "1", "ap": str(usd),
                                                          "T": int(self.now[0] * 1000)}}})

    def test_flow_timeframes_from_one_second_to_one_week(self):
        base = self.now[0]
        for age, usd in ((600_000, 2_000_000), (86_000, 1_000_000), (10_000, 400_000), (1_000, 300_000), (100, 200_000), (20, 150_000),
                         (3, 120_000), (0.5, 90_000)):          # whale buys, `age` seconds before "now"
            self.now[0] = base - age
            self._whale("DOGEUSDT", usd)
        self.now[0] = base
        count = {w: sum(f["n"] for f in self.w.view(window_s=w)["flow"]) for w in (1, 5, 30, 300, 3600, 14_400, 86_400, 604_800)}
        self.assertEqual(count, {1: 1, 5: 2, 30: 3, 300: 4, 3600: 5, 14_400: 6, 86_400: 7, 604_800: 8})
        v = self.w.view(window_s=5)
        self.assertEqual(v["window_s"], 5)
        self.assertAlmostEqual(v["flow"][0]["buy"], 210_000.0)          # 120K + 90K

    def test_a_week_of_history_is_kept_and_older_is_dropped(self):
        base = self.now[0]
        self.now[0] = base - 8 * 86_400                                  # 8 days ago
        self._whale("DOGEUSDT", 100_000)
        self._liq("ETHUSDT", "SELL", 50_000)
        self.now[0] = base - 6 * 86_400                                  # 6 days ago: still inside the week
        self._whale("DOGEUSDT", 200_000, sell=True)
        self.now[0] = base
        self._whale("DOGEUSDT", 300_000)                                 # pruned the 8-day-old entry when this arrived
        self.assertEqual(len(self.w.flow_log), 2)
        self.assertEqual(len(self.w.liq_log), 1)                         # liquidations are pruned on the next liquidation
        v = self.w.view(window_s=604_800)
        self.assertEqual((v["flow"][0]["buy"], v["flow"][0]["sell"], v["flow"][0]["n"]), (300_000.0, 200_000.0, 2))
        self.assertEqual(self.w.view(window_s=86_400)["flow"][0]["n"], 1)  # the 6-day-old sell is outside 1 day

    def test_liquidations_follow_the_window_and_the_hour_key_stays(self):
        base = self.now[0]
        self.now[0] = base - 7_200
        self._liq("ETHUSDT", "SELL", 30_000)                             # a long, 2 h ago
        self.now[0] = base - 20
        self._liq("XRPUSDT", "BUY", 2_000)                               # a short, 20 s ago
        self.now[0] = base
        self.assertEqual(self.w.view(window_s=60)["liq"], {"long": 0.0, "short": 2_000.0})
        self.assertEqual(self.w.view(window_s=86_400)["liq"], {"long": 30_000.0, "short": 2_000.0})
        v = self.w.view(window_s=60)
        self.assertEqual(v["liq_1h"], {"long": 0.0, "short": 2_000.0})   # the hour total never depends on the window
        self.assertEqual(self.w.view(window_s=300)["liq_1h"], v["liq_1h"])

    def test_covered_says_how_long_the_bot_has_really_watched(self):
        self.now[0] += 90                                                # the watch has run for 90 s
        v = self.w.view(window_s=86_400)
        self.assertEqual((v["window_s"], v["covered_s"]), (86_400, 90.0))
        self.assertEqual(self.w.view(window_s=30)["covered_s"], 30)      # a short window is fully covered

    def test_a_silly_window_is_clamped_not_trusted(self):
        for given, want in ((0, 1.0), (-5, 1.0), (10 ** 9, 604_800.0), (604_800, 604_800.0), ("x", 3600.0), (None, 3600.0), (float("nan"), 3600.0),
                            (float("inf"), 3600.0), (15, 15.0)):
            self.assertEqual(self.w.view(window_s=given)["window_s"], want, given)

    async def test_a_dead_stream_never_ends_the_bot(self):
        class Boom:
            def ws_connect(self, *a, **k):
                raise OSError("network is down")
        w = WhaleWatch(Boom(), Rest())
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        self.assertEqual(w.status, "down")
        self.assertIn("network is down", w.error)
        self.assertFalse(task.done())
        task.cancel()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_whale_view_is_served_and_absent_when_off(self):
        from pathlib import Path
        from types import SimpleNamespace
        from aiohttp.test_utils import TestClient, TestServer
        for whales, status in ((SimpleNamespace(view=lambda: {"status": "live"}), 200), (None, 404)):
            cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
            dash = Dashboard(SimpleNamespace(swing=None, snapshot=lambda: {}), cfg, Path("web"), whales=whales)
            server = TestServer(dash.make_app())
            await server.start_server()
            dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
            dash.cfg.server.port = server.port
            async with TestClient(server) as client:
                r = await client.get("/api/market/whales", headers={"Host": f"127.0.0.1:{server.port}"})
                self.assertEqual(r.status, status)

    async def test_the_window_is_passed_through_and_a_bad_one_is_refused(self):
        from pathlib import Path
        from types import SimpleNamespace
        from aiohttp.test_utils import TestClient, TestServer
        whales = SimpleNamespace(view=lambda window_s=3600.0: {"status": "live", "window_s": window_s})
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        dash = Dashboard(SimpleNamespace(swing=None, snapshot=lambda: {}), cfg, Path("web"), whales=whales)
        server = TestServer(dash.make_app())
        await server.start_server()
        dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        dash.cfg.server.port = server.port
        host = {"Host": f"127.0.0.1:{server.port}"}
        async with TestClient(server) as client:
            r = await client.get("/api/market/whales?window=14400", headers=host)
            self.assertEqual((r.status, (await r.json())["window_s"]), (200, 14400.0))
            self.assertEqual((await (await client.get("/api/market/whales", headers=host)).json())["window_s"], 3600.0)
            self.assertEqual((await client.get("/api/market/whales?window=abc", headers=host)).status, 400)


if __name__ == "__main__":
    unittest.main()
