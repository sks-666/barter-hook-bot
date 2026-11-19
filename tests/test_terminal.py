"""/terminal: the terminal view. Its one control is the testnet auto-trader's timeframe, so it carries the control token
(like the dashboard at /), and --lan locks it like every page."""
import unittest
from pathlib import Path

from tests.test_lan_access import KEY, LanTests

from arbbot.server import Dashboard


class TerminalTests(LanTests):
    async def test_this_mac_gets_the_terminal_with_the_control_token_for_the_auto_trader_timeframe(self):
        await self.start(cls=Dashboard)
        for path in ("/terminal", "/terminal/"):
            r = await self.get(path, host=f"127.0.0.1:{self.port}")
            body = await r.text()
            self.assertEqual(r.status, 200, path)
            self.assertIn("ARB TERMINAL", body)
            self.assertIn(f'const TOKEN = "{self.dash.token}"', body)
            self.assertNotIn("__TOKEN__", body)

    def test_its_only_control_is_the_auto_trader_timeframe(self):
        """It never pauses or flattens: the token is only ever sent to the account switch (which of the mainnet sub /
        main accounts the ticket and Trade button use), the Telegram signal alerts (🔔), the signal trader (its settings, and the Trade button that
        opens a signal through it) and the signal types."""
        import re
        html = Path("web/terminal.html").read_text(encoding="utf-8")
        with_token = re.findall(r'fetch\(([^,]+),\s*\{[^}]*"X-Token"', html)
        self.assertEqual(with_token, ['"/api/accounts"', '"/api/signal-alerts"', '"/api/signal-trader"', '"/api/signal-types"'])
        self.assertEqual(html.count('"X-Token"'), 4)
        self.assertIn("autoPost({ take: sym, tf: S.sgw, plan: p })", html)  # the one order it can send

    async def test_a_phone_needs_the_key(self):
        await self.start()
        self.assertEqual((await self.get("/terminal")).status, 401)
        self.assertEqual((await self.get("/terminal", cookies={"arb_lan_key": KEY})).status, 200)

    def test_the_dashboard_links_to_it_but_the_hosted_copy_drops_the_menu(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        nav = html[html.index("<!--NAV-->"):html.index("<!--/NAV-->")]
        self.assertIn('href="/terminal"', nav)


class CoinListTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_search_box_gets_every_trading_usdt_perpetual_busiest_first(self):
        from arbbot.market import MarketData

        class Api:
            calls = 0

            async def get(self, venue, path, params=None, timeout=None):
                Api.calls += 1
                if path == "/fapi/v1/exchangeInfo":
                    def s(sym, base, ct="PERPETUAL", st="TRADING", quote="USDT"):
                        return {"symbol": sym, "baseAsset": base, "contractType": ct, "status": st, "quoteAsset": quote}
                    return {"symbols": [s("BTCUSDT", "BTC"), s("ETHUSDT", "ETH"), s("NEWUSDT", "NEW"),
                                        s("BTCUSDT_261225", "BTC", ct="CURRENT_QUARTER"), s("OLDUSDT", "OLD", st="SETTLING"),
                                        s("ETHUSDC", "ETH", quote="USDC")]}
                return [{"symbol": "BTCUSDT", "lastPrice": "80000", "priceChangePercent": "1.5", "quoteVolume": "9e9"},
                        {"symbol": "ETHUSDT", "lastPrice": "3000", "priceChangePercent": "-2", "quoteVolume": "4e9"}]

        m = MarketData(Api(), clock=lambda: 0.0)
        rows = await m.symbols()
        self.assertEqual([r["symbol"] for r in rows], ["BTCUSDT", "ETHUSDT", "NEWUSDT"])   # no ticker yet: last
        self.assertEqual((rows[1]["base"], rows[1]["last"], rows[1]["change_pct"]), ("ETH", 3000.0, -2.0))
        await m.symbols()
        self.assertEqual(Api.calls, 2)                                 # cached: one exchangeInfo + one ticker call

    async def test_sentiment_comes_from_mainnet_on_a_testnet_bot(self):
        """The testnet has no /futures/data statistics: it redirects them to its website (an HTTP 202 HTML page)."""
        from arbbot.market import MarketData

        class Testnet:
            env = "testnet"

            async def get(self, venue, path, params=None, timeout=None):
                raise AssertionError(f"the testnet was asked for {path}")

        class Mainnet:
            env = "mainnet"
            paths = []

            async def get(self, venue, path, params=None, timeout=None):
                Mainnet.paths.append(path)
                if path.endswith("globalLongShortAccountRatio"):
                    return [{"timestamp": 20, "longAccount": "0.58"}]
                if path.endswith("takerlongshortRatio"):
                    return [{"timestamp": 20, "buySellRatio": "1.1"}]
                if path.startswith("/futures/data/top"):          # the top traders (whales)
                    return [{"timestamp": 20, "longAccount": "0.66"}]
                return [{"timestamp": 20, "sumOpenInterestValue": "7e9"}]

        d = await MarketData(Testnet(), clock=lambda: 0.0, stats_client=Mainnet()).trading_data("BTCUSDT")
        self.assertEqual(len(Mainnet.paths), 5)
        self.assertEqual(d["source"], "mainnet")
        row = d["rows"][0]
        self.assertAlmostEqual(row["long_pct"], 58.0)
        self.assertAlmostEqual(row["top_pos_long_pct"], 66.0)
        self.assertAlmostEqual(row["top_acc_long_pct"], 66.0)

    async def test_it_is_served_under_api_market(self):
        from types import SimpleNamespace
        from aiohttp.test_utils import TestClient, TestServer

        class Market:
            async def symbols(self):
                return [{"symbol": "BTCUSDT"}]
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        dash = Dashboard(SimpleNamespace(swing=None, snapshot=lambda: {}), cfg, Path("web"), market=Market())
        server = TestServer(dash.make_app())
        await server.start_server()
        dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        dash.cfg.server.port = server.port
        async with TestClient(server) as client:
            r = await client.get("/api/market/symbols", headers={"Host": f"127.0.0.1:{server.port}"})
            self.assertEqual((r.status, await r.json()), (200, [{"symbol": "BTCUSDT"}]))


del LanTests   # run only the terminal tests here, not the inherited LAN tests a second time

if __name__ == "__main__":
    unittest.main()
