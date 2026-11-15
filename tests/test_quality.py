"""The quality check (arbbot/quality.py): indicators, regime, the four guardrails, structural stop and targets, the
pre-mortem and the economic calendar, on synthetic candles."""
import calendar
import math
import unittest

from arbbot import quality as q
from arbbot.macro import parse_calendar

MON_NOON = calendar.timegm((2026, 11, 16, 12, 0, 0))      # a Monday, 12:00 UTC: liquid hours
SUN_NOON = calendar.timegm((2026, 11, 15, 12, 0, 0))


def candles(closes, vol=100.0, wick=0.3, buy_share=0.5, t0=0, step=900_000):
    """Kline rows [open ms, open, high, low, close, base vol, quote vol, taker-buy quote vol]."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        v = vol[i] if isinstance(vol, list) else vol
        hi, lo = max(prev, c) + wick, min(prev, c) - wick
        out.append([t0 + i * step, prev, hi, lo, c, v, v * c, v * c * buy_share])
        prev = c
    return out


def trend(n, start, slope, wobble=1.5, period=9):
    return [start + slope * i + wobble * math.sin(i * 2 * math.pi / period) for i in range(n)]


def kw(**over):
    up = candles(trend(301, 100, 0.2), vol=[100.0] * 299 + [300.0, 100.0])   # the trigger candle on 3x volume
    base = dict(side=1, mark=up[-2][4], entry=up[-2][4], base_conf=70, ltf=up, mid=up, htf=up, leader_mid=up,
                is_leader=False, macro_score=None, funding=0.0001, spot=None, oi_pct=2.0, curve_score=0.2, events=[],
                now_s=MON_NOON, ivs={"ltf": "15m", "mid": "1h", "htf": "4h"})
    base.update(over)
    return base


class Indicators(unittest.TestCase):
    def test_rsi_adx_atr_ema(self):
        up = candles([100 + i for i in range(80)], wick=0.2)
        self.assertEqual(q.rsi([k[4] for k in up])[-1], 100.0)
        self.assertGreater(q.adx(up), 50)                              # a straight line up is all trend
        self.assertAlmostEqual(q.atr(up), 1.4, places=6)               # each candle: 1 move + 2 x 0.2 wick
        self.assertAlmostEqual(q.ema([2.0] * 10, 5)[-1], 2.0)
        self.assertEqual(q.trend_dir(up), 1)
        self.assertEqual(q.trend_dir(candles([200 - i for i in range(80)])), -1)

    def test_swings_bos_and_sweep(self):
        closes = [100, 101, 102, 103, 102, 101, 100, 101, 102, 104.5]   # swing high 103, then a close above it
        kl = candles(closes, wick=0.0)
        highs, lows = q.swings(kl)
        self.assertIn((3, 103), highs)
        self.assertEqual(q.bos(kl, 1), 103)
        self.assertIsNone(q.bos(kl, -1))
        kl = candles([105, 104, 103, 102, 103, 104, 105, 104, 103.5], wick=0.0)
        kl[-1][3] = 101.0                                              # wick under the swing low 102, close back above
        self.assertEqual(q.sweep(kl, 1), 102)

    def test_round_numbers_are_avoided(self):
        self.assertEqual(q.round_step(97_000), 500)
        self.assertEqual(q.round_step(3_500), 50)
        self.assertAlmostEqual(q.round_step(0.35), 0.005)
        self.assertEqual(q.off_round(97_010, True, 50), 96_950)        # a long's stop on 97,000: pushed under it
        self.assertEqual(q.off_round(96_800, True, 50), 96_800)        # not near one: unchanged

    def test_intervals_follow_the_timeframe(self):
        self.assertEqual(q.intervals(3600), {"ltf": "15m", "mid": "1h", "htf": "4h"})
        self.assertEqual(q.intervals(86_400), {"ltf": "4h", "mid": "1d", "htf": "1w"})


class Regime(unittest.TestCase):
    def test_trend_range_and_chop(self):
        self.assertEqual(q.regime(candles(trend(300, 100, 0.5, wobble=0.5)))["name"], "TRENDING_BULL")
        self.assertEqual(q.regime(candles(trend(300, 400, -0.5, wobble=0.5)))["name"], "TRENDING_BEAR")
        flat = candles(trend(300, 100, 0.0, wobble=2.0, period=11), wick=0.5)
        self.assertEqual(q.regime(flat)["name"], "RANGING_MEAN_REVERSION")
        wild = candles(trend(286, 100, 0.0, wobble=0.5, period=11) +
                       [100 + (8 if i % 2 else -8) for i in range(14)])     # ranges blow up with no direction
        self.assertEqual(q.regime(wild)["name"], "VOLATILE_CHOP")


class Guardrails(unittest.TestCase):
    def test_a_clean_trend_trade_passes_with_its_structure(self):
        r = q.assess(**kw())
        self.assertEqual(r["regime"]["name"], "TRENDING_BULL")
        self.assertEqual(r["verdict"], "trade", r["gates"])
        lv = r["levels"]
        self.assertGreaterEqual(lv["rr1"], q.TP1_R)
        self.assertGreaterEqual(lv["rr2"], q.TP2_R)
        self.assertLess(lv["stop"], kw()["entry"] - q.atr(q.closed(kw()["ltf"])))   # at least 1 ATR (+ buffer) away
        self.assertEqual(set(r["pillars"]), {"structure", "momentum", "volume", "derivs"})

    def test_regime_filter_refuses_mean_reversion_against_a_strong_trend(self):
        down = candles(trend(301, 400, -0.5, wobble=0.5))
        r = q.assess(**kw(ltf=down, mid=down, htf=down, leader_mid=None, entry=down[-2][4], mark=down[-2][4]))
        self.assertEqual(r["verdict"], "no_trade")
        self.assertTrue(any(g.startswith("[REGIME]") for g in r["gates"]), r["gates"])
        self.assertEqual(r["premortem"]["severity"], "cancel")        # and the pre-mortem names the trend

    def test_fakeout_needs_volume_on_the_breakout_candle(self):
        closes = trend(300, 100, 0.0, wobble=1.0, period=7) + [104.0, 104.5]   # breaks the 20-candle high
        thin = candles(closes, vol=[100.0] * 300 + [110.0, 100.0])
        r = q.assess(**kw(ltf=thin, entry=104.0, mark=104.0))
        self.assertEqual(r["setup"], "breakout")
        self.assertTrue(any(g.startswith("[FAKEOUT]") for g in r["gates"]), r["gates"])
        loud = candles(closes, vol=[100.0] * 300 + [300.0, 100.0])
        r = q.assess(**kw(ltf=loud, entry=104.0, mark=104.0))
        self.assertFalse(any(g.startswith("[FAKEOUT]") for g in r["gates"]), r["gates"])

    def test_funding_filter_needs_extreme_funding_and_falling_spot_volume(self):
        falling = candles([100.0] * 31, vol=[100.0] * 24 + [40.0] * 7)
        rising = candles([100.0] * 31, vol=[100.0] * 31)
        r = q.assess(**kw(funding=0.0005, spot=falling))
        self.assertTrue(any(g.startswith("[FUNDING]") for g in r["gates"]), r["gates"])
        r = q.assess(**kw(funding=0.0005, spot=rising))
        self.assertFalse(any(g.startswith("[FUNDING]") for g in r["gates"]))
        self.assertEqual(r["premortem"]["severity"], "downgrade")     # still crowded: confidence -15
        r = q.assess(**kw(funding=-0.0005, spot=falling, side=-1))     # shorts paying: the mirror for a short
        self.assertTrue(any(g.startswith("[FUNDING]") for g in r["gates"]))

    def test_session_penalty(self):
        mon = q.assess(**kw())
        self.assertFalse(q.session(MON_NOON, [])["penalty"])
        self.assertTrue(q.session(SUN_NOON, [])["penalty"])
        self.assertIn("dead zone", q.session(MON_NOON + 9.5 * 3600, [])["why"])     # 21:30 UTC
        near = [{"ts": (MON_NOON + 600) * 1000, "title": "CPI m/m", "country": "USD"}]
        s = q.session(MON_NOON, near)
        self.assertTrue(s["penalty"])
        self.assertIn("CPI m/m in 10 min", s["why"])
        self.assertFalse(q.session(MON_NOON, [{"ts": (MON_NOON + 3600) * 1000, "title": "x"}])["penalty"])
        self.assertFalse(q.session(MON_NOON, None)["calendar"])       # calendar down: said so, not penalised
        sun = q.assess(**kw(now_s=SUN_NOON))
        down = q.PREMORTEM_DOWNGRADE if sun["premortem"]["severity"] == "downgrade" else 0
        self.assertEqual(sun["confidence"], round((0.4 * 70 + 0.6 * sun["quality"]) * 0.8 - down))
        self.assertLess(sun["confidence"], mon["confidence"])

    def test_btc_against_an_alt_cancels_it(self):
        down = candles(trend(301, 400, -0.5, wobble=0.5))
        r = q.assess(**kw(leader_mid=down))
        self.assertEqual(r["verdict"], "no_trade")
        self.assertIn("BTC", r["premortem"]["text"])


class Levels(unittest.TestCase):
    def test_an_htf_level_too_close_aborts(self):
        up = candles(trend(301, 100, 0.2))
        entry = up[-2][4]
        htf = candles([entry - 5] * 10 + [entry + 0.8, entry + 1.0, entry + 0.8] + [entry - 5] * 10, wick=0.0)
        lv = q.levels(entry, True, q.closed(up), q.closed(up), htf, a=1.0)
        self.assertIn("caps the reward", lv["abort"])
        far = candles([entry - 5] * 10 + [entry + 30, entry + 40, entry + 30] + [entry - 5] * 10, wick=0.0)
        lv = q.levels(entry, True, q.closed(up), q.closed(up), far, a=1.0)
        self.assertEqual(lv["abort"], "")
        self.assertGreaterEqual(lv["rr1"], 1.8)

    def test_a_stop_too_far_is_refused_not_tightened(self):
        kl = candles([100.0] * 80, wick=0.0)
        kl[40][3] = 80.0                                               # the only swing low is 20% below
        lv = q.levels(100.0, True, kl, kl, kl, a=0.5)
        self.assertIn("structural stop", lv["abort"])


class Calendar(unittest.TestCase):
    def test_only_high_impact_us_releases(self):
        rows = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-13T08:30:00-05:00", "impact": "High"},
                {"title": "GDP", "country": "EUR", "date": "2026-11-13T05:00:00-05:00", "impact": "High"},
                {"title": "Retail", "country": "USD", "date": "2026-11-13T08:30:00-05:00", "impact": "Low"},
                {"title": "Bank holiday", "country": "USD", "date": "bad", "impact": "High"}]
        self.assertEqual(parse_calendar(rows), [{"ts": calendar.timegm((2026, 11, 13, 13, 30, 0)) * 1000,
                                                 "title": "CPI m/m", "country": "USD"}])


if __name__ == "__main__":
    unittest.main()
