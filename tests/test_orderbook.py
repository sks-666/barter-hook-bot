"""The order book read (arbbot/orderbook.py): imbalance and walls from one book, the per-timeframe verdict from the
book's imbalance, taker delta, absorption and walls, and BookWatch recording only the coins the page looks at."""
import asyncio
import unittest

from arbbot import orderbook as ob
from arbbot.orderbook import BookWatch, book_stats, frame_read, walls


def book(bid_q=1.0, ask_q=1.0, mid=100.0, n=100, tick=0.01, wall=None):
    """n levels a side, tick apart; `wall`: (side, level index, quantity) replaced."""
    bids = [[round(mid - tick * (i + 1), 4), bid_q] for i in range(n)]
    asks = [[round(mid + tick * (i + 1), 4), ask_q] for i in range(n)]
    if wall:
        side, i, q = wall
        (bids if side == "bid" else asks)[i][1] = q
    return bids, asks


def kl(delta_share, price_pct=0.0, quote=1000.0):
    """Two candles whose taker buys - sells is delta_share of the volume and whose price moved price_pct."""
    buy = quote * (1 + delta_share) / 2
    return [[0, 100.0, 101, 99, 100.0, 10, quote, buy],
            [1, 100.0, 101, 99, 100.0 * (1 + price_pct / 100), 10, quote, buy]]


class Stats(unittest.TestCase):
    def test_imbalance_and_totals(self):
        s = book_stats(*book(bid_q=3.0, ask_q=1.0))
        self.assertAlmostEqual(s["mid"], 100.0)
        self.assertAlmostEqual(s["imb"], 0.5, delta=0.02)              # (3 - 1) / (3 + 1), price-weighted
        self.assertGreater(s["bid_usd"], s["ask_usd"])
        self.assertAlmostEqual(book_stats(*book())["imb"], 0.0, delta=0.02)
        self.assertIsNone(book_stats([], [[1, 1]]))

    def test_walls(self):
        bids, asks = book(wall=("bid", 20, 40.0))
        w = walls(bids, 100.0, "bid")
        self.assertEqual(len(w), 1)
        self.assertAlmostEqual(w[0]["price"], 99.79)
        self.assertAlmostEqual(w[0]["dist_pct"], -0.21, places=2)
        self.assertEqual(walls(asks, 100.0, "ask"), [])               # an even side has none
        self.assertEqual(book_stats(bids, asks)["walls"][0]["side"], "bid")


class Frames(unittest.TestCase):
    def test_heavy_bids_and_taker_buying_read_up(self):
        f = frame_read("15m", 900, [(t, 100, 0.4) for t in range(100, 1000, 10)], kl(0.25, price_pct=0.3), None, 1000)
        self.assertEqual((f["verdict"], f["score"]), ("up", 100))
        self.assertIn("bids 40% heavier", f["why"][0])

    def test_heavy_asks_and_taker_selling_read_down(self):
        f = frame_read("1h", 3600, [(t, 100, -0.3) for t in range(0, 3600, 10)], kl(-0.2, price_pct=-0.3), None, 3600)
        self.assertEqual(f["verdict"], "down")
        self.assertLess(f["score"], -50)

    def test_mixed_reads_balanced_and_coverage_is_said(self):
        f = frame_read("4h", 14_400, [(t, 100, 0.2) for t in range(13_800, 14_400, 10)], kl(-0.12), None, 14_400)
        self.assertEqual(f["verdict"], "balanced")
        self.assertIn("recorded 4% of the 4h", f["why"][0])
        self.assertEqual(f["coverage"], 0.04)

    def test_absorption(self):
        sold_held = frame_read("5m", 300, [], kl(-0.3, price_pct=0.2), None, 300)
        self.assertTrue(any(w.startswith("absorption: sellers") for w in sold_held["why"]))
        self.assertGreater(sold_held["score"], -100)                  # lifted by the absorption
        bought_flat = frame_read("5m", 300, [], kl(0.3, price_pct=-0.1), None, 300)
        self.assertTrue(any(w.startswith("absorption: buyers") for w in bought_flat["why"]))

    def test_a_wall_near_the_price_tilts_it(self):
        live = book_stats(*book(wall=("ask", 10, 50.0)))
        f = frame_read("5m", 300, [(t, 100, 0.0) for t in range(0, 300, 10)], None, live, 300)
        self.assertEqual(f["score"], -10)
        self.assertIn("resistance wall", f["why"][-1])

    def test_nothing_yet(self):
        f = frame_read("5m", 300, [], None, None, 0)
        self.assertEqual((f["verdict"], f["score"], f["why"]), ("balanced", 0, ["no book reads or candles yet"]))


class Market:
    def __init__(self):
        self.depth_calls, self.kl_calls = [], []

    @staticmethod
    def check_symbol(s):
        if not s.endswith("USDT"):
            raise ValueError("symbol must look like BTCUSDT")
        return s.upper()

    async def depth(self, sym, limit):
        self.depth_calls.append((sym, limit))
        bids, asks = book(bid_q=2.0)
        return {"bids": bids, "asks": asks}

    async def klines(self, sym, tf):
        self.kl_calls.append((sym, tf))
        return kl(0.1)


class Watch(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.now = [1_000_000.0]
        self.m = Market()
        return BookWatch(self.m, clock=lambda: self.now[0])

    async def test_view_reads_the_book_and_answers_every_timeframe(self):
        w = self.make()
        v = await w.view("BTCUSDT", "15m")
        self.assertEqual(self.m.depth_calls[0], ("BTCUSDT", 500))     # mainnet book, 500 levels
        self.assertEqual([f["tf"] for f in v["frames"]], ["5m", "15m", "1h", "4h"])
        self.assertEqual(v["reads"], 1)
        self.assertEqual(v["series"][0][0], int(self.now[0] * 1000) // 900_000 * 900_000)
        self.assertGreater(v["live"]["imb"], 0)
        w._task.cancel()
        with self.assertRaises(ValueError):
            await w.view("BTCUSDT", "2h")

    async def test_series_averages_per_candle(self):
        w = self.make()
        for i, imb in enumerate((0.2, 0.4, -0.6)):
            w.tapes.setdefault("X", ob.deque()).append((3600.0 * (i // 2) + i, 1, imb, 0))
        self.assertEqual(w.series("X", "1h"), [[0, 0.3, 2], [3_600_000, -0.6, 1]])

    async def test_only_the_coins_asked_for_lately_are_read_and_the_loop_stops_when_none(self):
        w = self.make()
        for s in ("AUSDT", "BUSDT", "CUSDT"):
            w.wanted[s] = self.now[0]
            self.now[0] += 1
        self.assertEqual(w.watching(), ["CUSDT", "BUSDT"])           # MAX_COINS, newest first
        self.now[0] += ob.WATCH_S + 10
        self.assertEqual(w.watching(), [])
        ob.SAMPLE_S, keep = 0.0, ob.SAMPLE_S
        try:
            await asyncio.wait_for(w.run(), 1)                        # returns at once: nobody is looking
        finally:
            ob.SAMPLE_S = keep
        self.assertEqual(self.m.depth_calls, [])

    async def test_old_reads_are_dropped(self):
        w = self.make()
        await w.sample("BTCUSDT")
        self.now[0] += ob.KEEP_S + 1
        await w.sample("BTCUSDT")
        self.assertEqual(len(w.tapes["BTCUSDT"]), 1)

    async def test_a_failed_read_is_reported(self):
        w = self.make()

        async def boom(sym, limit):
            raise RuntimeError("418 banned")
        self.m.depth = boom
        v = await w.view("BTCUSDT", "1h")
        w._task.cancel()
        self.assertIn("418 banned", v["error"])
        self.assertIsNone(v["live"])


if __name__ == "__main__":
    unittest.main()
