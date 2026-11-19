"""The terminal's live feed (arbbot/livefeed.py): Binance events turned into the terminal's shapes, one Binance
connection per coin shared by its pages, updates batched per flush with each page's own candle interval, and idle
coins dropped."""
import asyncio
import json
import unittest

from arbbot import livefeed as lf
from arbbot.livefeed import LiveFeed, convert, streams


class Convert(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(convert({"e": "aggTrade", "p": "100.5", "q": "2", "T": 7, "m": True}),
                         ("t", {"price": 100.5, "usd": 201.0, "ts": 7, "sell": True}))
        k = {"t": 60_000, "i": "5m", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "q": "15", "Q": "9"}
        self.assertEqual(convert({"e": "kline", "k": k}), ("k:5m", [60_000, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 9.0]))
        kind, tk = convert({"e": "24hrTicker", "c": "3", "P": "-1.5", "h": "4", "l": "2", "v": "9", "q": "27"})
        self.assertEqual((kind, tk["last"], tk["change_pct"], tk["quote_volume"]), ("tk", 3.0, -1.5, 27.0))
        kind, mk = convert({"e": "markPriceUpdate", "p": "3.1", "i": "3.0", "r": "0.0001", "T": 99})
        self.assertEqual((kind, mk["funding_rate"], mk["next_funding"]), ("mk", 0.0001, 99))
        self.assertIsNone(convert({"e": "depthUpdate"}))

    def test_streams(self):
        s = streams("BTCUSDT")
        self.assertIn("btcusdt@aggTrade", s)
        self.assertIn("btcusdt@kline_1w", s)
        self.assertEqual(len(s), 3 + len(lf.INTERVALS))


class Page:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))


class Session:
    def __init__(self):
        self.urls = []

    def ws_connect(self, url, **kw):
        self.urls.append(url)
        raise ConnectionError("offline in tests")


class Feed(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [0.0]
        self.session = Session()
        self.feed = LiveFeed(self.session, "testnet", clock=lambda: self.now[0])
        lf.FLUSH_S, self.keep = 0.01, lf.FLUSH_S

    async def asyncTearDown(self):
        lf.FLUSH_S = self.keep
        for h in self.feed.hubs.values():
            h.task.cancel()
        if self.feed._flusher:
            self.feed._flusher.cancel()

    async def test_one_connection_per_coin_and_each_page_gets_its_interval(self):
        a, b = Page(), Page()
        self.feed.attach(a, "BTCUSDT", "5m")
        self.feed.attach(b, "BTCUSDT", "1h")
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed.hubs), 1)
        self.assertTrue(self.session.urls[0].startswith("wss://fstream.binancefuture.com/market/stream?streams=btcusdt@"))
        hub = self.feed.hubs["BTCUSDT"]
        hub.trades = [{"price": 1, "ts": 1}, {"price": 2, "ts": 2}]
        hub.latest = {"k:5m": [1], "k:1h": [2], "tk": {"last": 2}}
        await asyncio.sleep(0.05)
        self.assertEqual(a.sent[0]["k"], [1])
        self.assertEqual(b.sent[0]["k"], [2])
        self.assertEqual([t["price"] for t in a.sent[0]["t"]], [2, 1])        # newest first
        self.assertEqual(a.sent[0]["tk"], {"last": 2})
        self.assertEqual(len(a.sent), 1)                                      # nothing new: nothing sent

    async def test_switching_coin_moves_the_page_and_idle_coins_close(self):
        p = Page()
        self.feed.attach(p, "BTCUSDT", "1m")
        self.feed.attach(p, "ETHUSDT", "1m")
        self.assertEqual(self.feed.hubs["BTCUSDT"].pages, {})
        self.assertIn(p, self.feed.hubs["ETHUSDT"].pages)
        self.now[0] += lf.IDLE_S + 1
        await asyncio.sleep(0.05)
        self.assertNotIn("BTCUSDT", self.feed.hubs)
        self.assertIn("ETHUSDT", self.feed.hubs)

    async def test_serve_reads_the_pages_requests(self):
        class Msg:
            def __init__(self, data):
                self.type, self.data = 1, data

        class WS(Page):
            def __init__(self, msgs):
                super().__init__()
                self.msgs = msgs

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.msgs:
                    raise StopAsyncIteration
                return self.msgs.pop(0)
        ws = WS([Msg("not json"), Msg(json.dumps({"sym": "bad!"})), Msg(json.dumps({"sym": "solusdt", "iv": "15m"}))])
        attached = []
        self.feed.attach = lambda w, s, iv: attached.append((s, iv))
        await self.feed.serve(ws)
        self.assertEqual(attached, [("SOLUSDT", "15m")])


if __name__ == "__main__":
    unittest.main()
