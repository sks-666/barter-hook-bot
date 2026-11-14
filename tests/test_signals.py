"""The combined signal (arbbot/signals.py): one timeframe drives every source; five readings make one trade idea."""
import unittest

from arbbot.market import CURVE_PERIODS, SENT_PERIODS, SENT_ROWS
from arbbot.signals import (SignalDesk, atr, score_crowd, score_curve, score_macro, score_news, score_whales,
                            step_for, weights)
from arbbot.whales import WhaleWatch

NOW = 1_800_000_000.0
DAY = 86_400_000


def agg(sym, price, qty, t_ms, sell=False):
    return {"data": {"e": "aggTrade", "s": sym, "p": str(price), "q": str(qty), "T": t_ms, "m": sell}}


class Timeframes(unittest.TestCase):
    def test_each_timeframe_reads_the_sources_in_a_step_that_fits_it(self):
        self.assertEqual(step_for(3600, SENT_PERIODS, SENT_ROWS), ("5m", 12))     # 1h: 5m rows, 12 back
        self.assertEqual(step_for(14_400, SENT_PERIODS, SENT_ROWS), ("30m", 8))   # 4h
        self.assertEqual(step_for(86_400, SENT_PERIODS, SENT_ROWS), ("4h", 6))    # 1d
        self.assertEqual(step_for(604_800, SENT_PERIODS, SENT_ROWS), ("1d", 7))   # 1w
        self.assertEqual(step_for(604_800, CURVE_PERIODS, 60), ("1d", 7))
        self.assertEqual(step_for(30, SENT_PERIODS, SENT_ROWS), ("5m", 1))        # seconds: the smallest step, 1 back

    def test_macro_only_counts_on_longer_timeframes(self):
        self.assertEqual(weights(300)["macro"], 0.0)
        self.assertLess(weights(3600)["macro"], weights(86_400)["macro"])
        self.assertEqual(weights(604_800)["macro"], 0.2)


class Readings(unittest.TestCase):
    def test_whales(self):
        self.assertIsNone(score_whales(None)[0])                          # no reading, not a neutral one
        s, why = score_whales({"pressure": -0.8, "n": 10, "buy": 1e5, "sell": 9e5, "liq_long": 0, "liq_short": 0})
        self.assertAlmostEqual(s, -0.8)
        self.assertIn("sells $900.0K", why)
        self.assertAlmostEqual(score_whales({"pressure": 1, "n": 3, "buy": 1, "sell": 0, "liq_long": 0, "liq_short": 0})[0], 0.6)

    def test_crowd_whales_longer_than_usual_and_buying_is_bullish(self):
        rows = [{"ts": 2, "long_pct": 60, "top_pos_long_pct": 70, "taker_ratio": 1.2, "oi_usd": 110},
                {"ts": 1, "long_pct": 62, "top_pos_long_pct": 66, "taker_ratio": 1.1, "oi_usd": 100},
                {"ts": 0, "long_pct": 70, "top_pos_long_pct": 60, "taker_ratio": 1.0, "oi_usd": 100}]
        s, why = score_crowd({"rows": rows}, 1)
        self.assertGreater(s, 0.5)
        self.assertIn("open interest +10.0%", why)
        bearish = [{"long_pct": 80, "top_pos_long_pct": 60, "taker_ratio": 0.8},    # whales cutting longs, crowd buying
                   {"long_pct": 75, "top_pos_long_pct": 66, "taker_ratio": 0.9},
                   {"long_pct": 72, "top_pos_long_pct": 68, "taker_ratio": 1.0}]
        self.assertLess(score_crowd({"rows": bearish}, 1)[0], -0.5)
        # always ~10 pts less long than the crowd is just normal: no bearish reading from the gap alone
        usual = [{"long_pct": 76, "top_pos_long_pct": 66, "taker_ratio": 1.0} for _ in range(5)]
        self.assertAlmostEqual(score_crowd({"rows": usual}, 2)[0], 0.0)

    def test_curve_rising_premium_is_bullish_and_crowded_funding_is_bearish(self):
        perp = {"type": "PERPETUAL", "ann_pct": 11.0}
        up = {"points": [perp], "history": {"PERPETUAL": [{"basis_pct": 0.0}, {"basis_pct": 0.05}]}}
        self.assertAlmostEqual(score_curve(up, 1)[0], 0.6)
        hot = {"points": [dict(perp, ann_pct=60.0)], "history": {"PERPETUAL": []}}
        self.assertAlmostEqual(score_curve(hot, 1)[0], -0.4)

    def test_macro_risk_on_is_bullish(self):
        rows = lambda a, b: {"rows": [[0, a], [DAY, b]]}               # noqa: E731
        risk_on = {"series": {"^GSPC": rows(100, 102), "^VIX": rows(20, 18), "DX-Y.NYB": rows(100, 99), "^TNX": rows(4.5, 4.35)}}
        s, why = score_macro(risk_on, 86_400)
        self.assertAlmostEqual(s, 1.0)
        self.assertIn("S&P +2.00%", why)
        with self.assertRaises(ValueError):
            score_macro({"series": {}}, 86_400)

    def test_news_reads_the_coin_and_the_market_within_the_timeframe(self):
        now = NOW * 1000
        items = [{"ts": now - 600_000, "title": "Solana rallies as ETF inflows surge", "coins": ["SOL"], "kind": "crypto"},
                 {"ts": now - 900_000, "title": "Crypto exchange hacked, funds drained", "coins": [], "kind": "crypto"},
                 {"ts": now - 7 * 3600_000, "title": "Solana crashes", "coins": ["SOL"], "kind": "crypto"},  # too old for 1h
                 {"ts": now - 60_000, "title": "Recipe of the day", "coins": [], "kind": "macro"}]               # not relevant
        s, why = score_news({"items": items}, "SOL", 3600, now)
        self.assertGreater(s, 0)
        self.assertEqual(why, "2 headlines: 1 bullish, 1 bearish")
        self.assertIsNone(score_news({"items": []}, "SOL", 3600, now)[0])

    def test_atr(self):
        k = [[0, 0, 11, 9, 10]] * 15
        self.assertAlmostEqual(atr(k), 2.0)
        self.assertIsNone(atr([]))


class FakeMarket:
    def __init__(self, bullish=True):
        self.b = bullish
        self.calls = []

    async def trading_data(self, sym, period):
        self.calls.append(("data", sym, period))
        top = 70 if self.b else 40
        return {"rows": [{"long_pct": 55, "top_pos_long_pct": top, "taker_ratio": 1.2 if self.b else 0.8, "oi_usd": 1},
                         {"long_pct": 55, "top_pos_long_pct": 60 if self.b else 50, "taker_ratio": 1.0, "oi_usd": 1}]}

    async def curve(self, sym, period):
        self.calls.append(("curve", sym, period))
        return {"points": [{"type": "PERPETUAL", "ann_pct": 5.0}], "history": {"PERPETUAL": [{"basis_pct": 0}, {"basis_pct": 0.03}]}}

    async def klines(self, sym, interval):
        self.calls.append(("klines", sym, interval))
        return [[0, 0, 0.202, 0.198, 0.2]] * 20                         # ATR 0.004


class Macro:
    async def macro(self):
        raise RuntimeError("FRED is down")                           # a source that is down just drops out

    async def news(self):
        return {"items": []}


class Desk(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [NOW]
        self.w = WhaleWatch(None, None, clock=lambda: self.now[0])
        self.w.thresholds = {"DOGEUSDT": 50_000.0}
        for i, px in enumerate((0.199, 0.200, 0.201, 0.200)):        # whales buy $400K around 0.20
            t = int(self.now[0] * 1000)
            self.w.handle(agg("DOGEUSDT", px, 100_000 / px, t))
            self.w.sweep(t + 10_000)
            self.now[0] += 1
        self.w.marks["DOGEUSDT"] = (0.2015, 0.0)

    async def test_everything_agrees_long_on_the_timeframes_own_steps(self):
        m = FakeMarket()
        v = await SignalDesk(self.w, m, Macro(), clock=lambda: self.now[0], wall=lambda: self.now[0]).view(86_400)
        self.assertEqual(v["steps"], {"crowd": "4h x6", "curve": "4h x6", "atr": "4h"})
        self.assertIn(("data", "DOGEUSDT", "4h"), m.calls)
        r = v["rows"][0]
        self.assertEqual((r["symbol"], r["bias"]), ("DOGEUSDT", "long"))
        f = {x["key"]: x for x in r["factors"]}
        self.assertIsNone(f["macro"]["score"])                         # FRED down: macro drops out, the rest still count
        self.assertGreater(f["whales"]["score"], 0)
        self.assertGreater(f["crowd"]["score"], 0)
        self.assertLessEqual(r["entry"], 0.2015)                       # a pullback toward where the whales bought
        self.assertAlmostEqual(r["entry"] - r["stop"], 0.004)          # the 4h ATR is wider than the whales' spread
        self.assertGreater(r["target"], r["entry"])
        self.assertGreater(r["confidence"], 50)

    async def test_the_crowd_and_curve_can_overrule_the_whales_and_the_loaded_coin_is_added(self):
        v = await SignalDesk(self.w, FakeMarket(bullish=False), Macro(), clock=lambda: self.now[0],
                             wall=lambda: self.now[0]).view(3600, "BTCUSDT")
        rows = {r["symbol"]: r for r in v["rows"]}
        self.assertEqual(set(rows), {"DOGEUSDT", "BTCUSDT"})
        self.assertEqual((rows["BTCUSDT"]["factors"][0]["why"], rows["BTCUSDT"]["factors"][0]["weight"]),
                         ("fewer than 3 whale orders", 0.0))
        self.assertLess(rows["DOGEUSDT"]["score"], weights(3600)["whales"] / sum(weights(3600).values()) + 0.01)


if __name__ == "__main__":
    unittest.main()
