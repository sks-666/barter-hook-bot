"""Terminal alerts (arbbot/price_alerts.py) fed by the whale watch's streams, and their /api/alerts route."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arbbot.price_alerts import COOLDOWN_S, AlertBook
from arbbot.server import Dashboard
from arbbot.whales import WhaleWatch

T0 = 1_800_000_000.0


class AlertBookTests(unittest.TestCase):
    def setUp(self):
        self.now = [T0]
        self.sent: list[str] = []
        self.path = Path(tempfile.mkdtemp()) / "alerts.json"
        self.book = AlertBook(self.path, send=self.sent.append, clock=lambda: self.now[0])

    def test_a_price_alert_fires_once_when_the_mark_crosses(self):
        r = self.book.add({"kind": "price", "symbol": "btcusdt", "op": ">", "value": 82000})
        self.assertEqual(r["text"], "BTCUSDT mark > 82,000.00")
        self.book.check_marks({"BTCUSDT": (81999.0, 0.0001)})
        self.assertEqual(self.sent, [])
        self.book.check_marks({"BTCUSDT": (82010.5, 0.0001)})
        self.book.check_marks({"BTCUSDT": (82100.0, 0.0001)})               # one-shot: not again
        self.assertEqual(self.sent, ["🔔 BTCUSDT mark 82,010.50 is above 82,000.00"])
        self.assertEqual(self.book.view()["fired"][0]["symbol"], "BTCUSDT")
        self.assertEqual(self.book.clear_fired(), 1)

    def test_funding_alerts_compare_in_percent(self):
        self.book.add({"kind": "funding", "symbol": "ETHUSDT", "op": "<", "value": -0.01})
        self.book.check_marks({"ETHUSDT": (3000.0, -0.00005)})                # -0.005%: not yet
        self.book.check_marks({"ETHUSDT": (3000.0, -0.0002)})                 # -0.02%
        self.assertEqual(len(self.sent), 1)
        self.assertIn("funding -0.0200% is below -0.01%", self.sent[0])

    def test_whale_and_liquidation_alerts_repeat_with_a_cooldown(self):
        self.book.add({"kind": "whale", "symbol": "", "value": 500_000})      # any coin
        self.book.add({"kind": "liq", "symbol": "BTCUSDT", "value": 100_000})
        big = {"symbol": "SOLUSDT", "side": "buy", "usd": 612_000.0, "price": 190.2}
        self.book.on_event("whale", dict(big, usd=400_000.0))                 # too small
        self.book.on_event("whale", big)
        self.book.on_event("whale", big)                                       # inside the cooldown: counted
        self.now[0] += COOLDOWN_S + 1
        self.book.on_event("whale", big)
        self.assertEqual(self.sent[0], "🐋 SOLUSDT whale BUY $612K @ 190.20")
        self.assertIn("(+1 more in the last 60s)", self.sent[1])
        self.book.on_event("liq", {"symbol": "ETHUSDT", "side": "long", "usd": 2e6, "price": 3000.0})   # other coin
        self.book.on_event("liq", {"symbol": "BTCUSDT", "side": "long", "usd": 1.2e6, "price": 79800.0})
        self.assertEqual(self.sent[2], "💥 BTCUSDT LONG liquidated $1.20M @ 79,800.00")

    def test_bad_rules_are_refused_with_a_reason(self):
        for bad, why in (({"kind": "moon"}, "kind"), ({"kind": "price", "symbol": "BTC/USDT", "value": 1}, "symbol"),
                         ({"kind": "price", "symbol": "BTCUSDT", "op": "=", "value": 1}, "op"),
                         ({"kind": "price", "symbol": "BTCUSDT", "value": -5}, "above 0"),
                         ({"kind": "whale", "value": 500}, "at least"), ({"kind": "price", "symbol": "BTCUSDT", "value": "x"}, "number")):
            with self.assertRaises(ValueError) as cm:
                self.book.add(bad)
            self.assertIn(why, str(cm.exception))

    def test_rules_survive_a_restart(self):
        self.book.add({"kind": "price", "symbol": "BTCUSDT", "op": "<", "value": 70000})
        again = AlertBook(self.path)
        self.assertEqual([r["text"] for r in again.rules], ["BTCUSDT mark < 70,000.00"])
        self.assertEqual(again.add({"kind": "whale", "value": 1e6})["id"], 2)
        self.assertTrue(again.remove(1))
        self.assertFalse(again.remove(99))


class StreamHookupTests(unittest.TestCase):
    def test_the_whale_watch_feeds_marks_whales_and_liquidations_to_the_alerts(self):
        sent: list[str] = []
        book = AlertBook(None, send=sent.append, clock=lambda: T0)
        w = WhaleWatch(None, None, clock=lambda: T0)
        w.thresholds = {"BTCUSDT": 100_000.0}
        w.on_marks, w.on_event = book.check_marks, book.on_event
        book.add({"kind": "price", "symbol": "BTCUSDT", "op": ">", "value": 80000})
        book.add({"kind": "whale", "symbol": "BTCUSDT", "value": 100_000})
        book.add({"kind": "liq", "value": 10_000})
        self.assertIn("!markPrice@arr@1s", w.url())
        w.handle({"stream": "!markPrice@arr@1s", "data": [{"e": "markPriceUpdate", "s": "BTCUSDT", "p": "80100", "r": "0.0001"}]})
        t = int(T0 * 1000)
        w.handle({"data": {"e": "aggTrade", "s": "BTCUSDT", "p": "80100", "q": "2", "T": t, "m": False}})
        w.sweep(t + 1000)
        w.handle({"data": {"e": "forceOrder", "o": {"s": "XRPUSDT", "S": "BUY", "z": "10000", "ap": "2", "T": t}}})
        self.assertEqual([m[0] for m in sent], ["🔔", "🐋", "💥"])
        self.assertEqual(w.markers("BTCUSDT")["trades"][0]["usd"], 160_200.0)   # the chart's markers
        self.assertEqual(w.markers("DOGEUSDT"), {"symbol": "DOGEUSDT", "trades": [], "liqs": []})

    def test_a_failing_listener_never_breaks_the_stream(self):
        w = WhaleWatch(None, None, clock=lambda: T0)

        def boom(marks):
            raise RuntimeError("bad alert")
        w.on_marks = boom
        with self.assertLogs("whales", "ERROR"):
            w.handle({"data": [{"e": "markPriceUpdate", "s": "BTCUSDT", "p": "1", "r": "0"}]})
        self.assertEqual(w.marks["BTCUSDT"], (1.0, 0.0))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, book):
        from aiohttp.test_utils import TestClient, TestServer
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        engine = SimpleNamespace(swing=None, snapshot=lambda: {}, alerts=SimpleNamespace(enabled=True))
        dash = Dashboard(engine, cfg, Path("web"), alert_book=book)
        server = TestServer(dash.make_app())
        await server.start_server()
        dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        dash.cfg.server.port = server.port
        self.host = {"Host": f"127.0.0.1:{server.port}"}
        self.client = TestClient(server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_add_list_and_remove_alerts(self):
        await self.start(AlertBook(None))
        r = await self.client.post("/api/alerts", json={"action": "add", "rule": {"kind": "price", "symbol": "BTCUSDT",
                                                                               "op": ">", "value": 90000}}, headers=self.host)
        self.assertEqual(r.status, 200)
        listed = await (await self.client.get("/api/alerts", headers=self.host)).json()
        self.assertEqual((len(listed["rules"]), listed["telegram"]), (1, True))
        r = await self.client.post("/api/alerts", json={"action": "add", "rule": {"kind": "price", "symbol": "x"}},
                                   headers=self.host)
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/alerts", json={"action": "remove", "id": 1}, headers=self.host)
        self.assertEqual((await r.json())["state"]["rules"], [])

    async def test_another_site_cannot_set_alerts(self):
        await self.start(AlertBook(None))
        r = await self.client.post("/api/alerts", json={"action": "add", "rule": {"kind": "whale", "value": 1e6}},
                                   headers=dict(self.host, Origin="http://evil.example"))
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/alerts", data="action=add", headers=self.host)       # not JSON: no preflight
        self.assertEqual(r.status, 415)

    async def test_off_without_the_whale_watch(self):
        await self.start(None)
        self.assertEqual((await self.client.get("/api/alerts", headers=self.host)).status, 404)


if __name__ == "__main__":
    unittest.main()
