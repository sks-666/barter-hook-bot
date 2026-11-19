"""The liquidity heatmap (arbbot/heatmap.py): the band height, the local order book's Binance sync rules, columns and
their running per-bucket sums, the answer's rows and incremental buckets, and which coins get recorded."""
import array
import asyncio
import base64
import struct
import unittest

from arbbot import heatmap as hm
from arbbot.heatmap import Heatmap, LocalBook, Recorder, nice_step


def snap(uid=100, mid=100.0):
    return {"bids": [[mid - 0.5, 2.0], [mid - 1.0, 1.0]], "asks": [[mid + 0.5, 1.0], [mid + 1.0, 3.0]], "last_update_id": uid}


def ev(U, u, pu, b=(), a=()):
    return {"U": U, "u": u, "pu": pu, "b": [[str(p), str(q)] for p, q in b], "a": [[str(p), str(q)] for p, q in a]}


class Book(unittest.TestCase):
    def test_band_height(self):
        self.assertEqual(nice_step(63_000), 10)
        self.assertEqual(nice_step(3_700), 0.5)
        self.assertAlmostEqual(nice_step(0.5), 0.0001)

    def test_sync_rules(self):
        b = LocalBook()
        self.assertFalse(b.apply(ev(1, 2, 0)))                         # no snapshot yet
        b.load(snap(uid=100))
        self.assertTrue(b.apply(ev(90, 95, 89, b=[(99.5, 9)])))        # older than the snapshot: dropped
        self.assertEqual(b.bids[99.5], 2.0)
        self.assertTrue(b.apply(ev(98, 103, 97, b=[(99.5, 5)], a=[(100.5, 0)])))   # straddles it: applied
        self.assertEqual((b.bids[99.5], 100.5 in b.asks, b.synced), (5.0, False, True))
        self.assertTrue(b.apply(ev(104, 110, 103, a=[(100.7, 4)])))    # pu = the last u
        self.assertFalse(b.apply(ev(115, 120, 112)))                   # a missed event: resync
        g = LocalBook()
        g.load(snap(uid=100))
        self.assertFalse(g.apply(ev(105, 110, 104)))                   # a gap after the snapshot

    def test_bands_and_mid(self):
        b = LocalBook()
        b.load(snap())
        self.assertEqual(b.mid(), 100.0)
        both = b.bands(1.0, 98, 4)                                    # bands 98, 99, 100, 101
        self.assertEqual(list(both), [0.0, 99.0 * 1 + 99.5 * 2, 100.5, 101.0 * 3])
        self.assertEqual(list(b.bands(1.0, 98, 4, "ask")), [0.0, 0.0, 100.5, 303.0])

    def test_far_levels_are_pruned(self):
        b = LocalBook()
        b.load({"bids": [[50.0, 1.0], [99.0, 1.0]], "asks": [[101.0, 1.0], [200.0, 1.0]], "last_update_id": 1})
        b.prune(100.0)
        self.assertEqual((list(b.bids), list(b.asks)), ([99.0], [101.0]))


def floats(col: bytes) -> tuple:
    return struct.unpack(f"<{len(col) // 2}e", col)


class Columns(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.now = [10_000.0]
        r = Recorder("BTCUSDT", None, None, clock=lambda: self.now[0])
        r.book.load(snap())
        r.book.synced = True
        return r

    async def test_record_and_buckets(self):
        r = self.make()
        r.record()
        self.assertEqual(r.step, 0.02)                                  # nice_step(100)
        t, lo, col, strength = r.cols[0]
        self.assertEqual(strength, r.book.strength(100.0))
        self.assertEqual(strength, 1.0)                               # only the 99.5 bid is within 0.5%
        self.assertEqual(len(col), 2 * (int(2 * hm.RANGE * 100 / 0.02) + 1))   # half floats: 2 bytes a band
        total = (99.5 * 2 + 99 + 100.5 + 101 * 3) / hm.K              # in $K
        self.assertAlmostEqual(sum(floats(col)), total, places=3)
        roll = await r.rollup(60)
        self.now[0] += 5
        r.record()                                                    # the same minute: added to the bucket
        self.now[0] += 60
        r.record()
        self.assertEqual(sorted(k[2] for k in roll.values()), [1, 2])
        n = len(col) // 2
        times, data, st = await r.grid(0, 1e9, 60, lo, n)
        self.assertAlmostEqual(st[0], strength, places=4)
        self.assertEqual(times, [9960.0, 10020.0])
        self.assertAlmostEqual(sum(floats(data[:2 * n])), total, places=3)    # a bucket answers its average
        raw_t, raw, _ = await r.grid(0, 1e9, hm.SAMPLE_S, lo, n)        # the finest: the columns themselves
        self.assertEqual(len(raw_t), 3)
        self.assertEqual(raw[:2 * n], col)

    async def test_a_drifting_price_widens_the_bucket(self):
        r = self.make()
        r.record()
        roll = await r.rollup(300)
        r.book.load(snap(mid=101.0))
        r.book.synced = True
        self.now[0] += 5
        r.record()
        lo, arr, k, _, _ = next(iter(roll.values()))
        self.assertEqual(k, 2)
        (l0, c0), (l1, c1) = [(c[1], len(c[2]) // 2) for c in r.cols]
        self.assertEqual((lo, len(arr)), (min(l0, l1), max(l0 + c0, l1 + c1) - min(l0, l1)))

    async def test_old_columns_and_buckets_go(self):
        r = self.make()
        r.record()
        await r.rollup(60)
        self.now[0] += hm.KEEP_S + 120
        r.record()
        self.assertEqual(len(r.cols), 1)
        self.assertEqual(len(r.rolls[60]), 1)

    def test_resolution_snaps_up(self):
        self.assertEqual([hm.snap_res(x) for x in (1, 5, 6, 61, 99999)], [5, 5, 10, 120, 1800])


class Market:
    def __init__(self):
        self.kl = [[(1000 + i) * 300_000, 100.0, 101.0, 99.0, 100.0, 1, 100, 50] for i in range(50)]

    @staticmethod
    def check_symbol(s):
        if not s.endswith("USDT"):
            raise ValueError("symbol must look like BTCUSDT")
        return s

    async def klines(self, sym, iv):
        return self.kl

    async def depth(self, sym, limit):
        return snap()


class Session:
    def ws_connect(self, url, **kw):
        raise ConnectionError("offline")


class View(unittest.IsolatedAsyncioTestCase):
    async def test_answer_rows_and_incremental_columns(self):
        m = Market()
        now = [m.kl[-1][0] / 1000 + 10]
        h = Heatmap(m, Session(), clock=lambda: now[0])
        v = await h.view("BTCUSDT", "5m")
        self.assertIsNone(v["grid"])                                  # nothing recorded yet
        r = h.rec["BTCUSDT"]
        r.task.cancel()
        r.book.load(snap())
        r.book.synced = True
        for _ in range(3):
            r.record()
            now[0] += 5
        t0 = now[0] - 600
        v = await h.view("BTCUSDT", "5m", t0=t0, t1=now[0], res=1, plo=99.0, phi=101.0)
        g = v["grid"]
        self.assertEqual(v["res"], hm.SAMPLE_S)                       # one column per snapshot
        self.assertEqual(g["lo_bin"] % 64, 0)
        self.assertEqual(len(g["times"]), 3)
        self.assertEqual(len(base64.b64decode(g["data"])), 2 * g["rows"] * 3)
        self.assertEqual((len(v["book"]["bids"]), v["book"]["best_bid"], v["book"]["best_ask"]), (g["rows"], 99.5, 100.5))
        part = await h.view("BTCUSDT", "5m", t0=t0, t1=now[0], res=1, plo=99.0, phi=101.0, after=g["times"][-1])
        self.assertTrue(part["grid"]["partial"])
        self.assertEqual(part["grid"]["times"], [g["times"][-1]])
        wide = await h.view("BTCUSDT", "5m", t0=now[0] - 86_000, t1=now[0], res=1)
        self.assertGreaterEqual(wide["res"], 86_000 / hm.MAX_COLS)    # too many columns: coarser
        with self.assertRaises(ValueError):
            await h.view("BTCUSDT", "2h")
        for x in h.rec.values():
            x.task.cancel()

    async def test_only_the_latest_coins_are_recorded(self):
        now = [0.0]
        h = Heatmap(Market(), Session(), clock=lambda: now[0])
        for s in ("AUSDT", "BUSDT", "CUSDT", "DUSDT"):
            h.want(s)
            now[0] += 1
        self.assertEqual(sorted(h.rec), ["BUSDT", "CUSDT", "DUSDT"])  # MAX_COINS
        now[0] += hm.WATCH_S + 10
        h.want("EUSDT")
        self.assertEqual(list(h.rec), ["EUSDT"])
        await asyncio.sleep(0)
        for x in h.rec.values():
            x.task.cancel()


if __name__ == "__main__":
    unittest.main()
