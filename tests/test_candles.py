"""Candlestick patterns (arbbot/quality.candle_pattern) and the Candle reversal signal type: each pattern on hand-made
candles, a reversal only counting at a support / resistance, a pattern closed through no longer counting, the stop going
past the pattern, and classification order."""
import unittest

from arbbot import quality as q
from arbbot import signal_types as st
from arbbot.signal_types import TypeBook
from tests.test_quality import kw


def bar(o, h, l, c, t=0):
    return [t, o, h, l, c, 100.0, 100.0 * c, 50.0 * c]


def base(n=50):
    """A quiet range around 100: candles of range 1.2 (ATR about 1.2), lows at 99.4, highs at 100.6."""
    return [bar(99.6, 100.6, 99.4, 100.4) if i % 2 else bar(100.4, 100.6, 99.4, 99.6) for i in range(n)]


def find(kl, side=1):
    return q.candle_pattern(kl, side, q.atr(kl))


class Patterns(unittest.TestCase):
    def test_bullish_engulfing_at_the_range_low(self):
        kl = base() + [bar(100.2, 100.3, 99.3, 99.5), bar(99.5, 100.6, 99.3, 100.4)]
        p = find(kl)
        self.assertEqual((p["key"], p["name"], p["kind"], p["ago"]), ("engulfing", "bullish engulfing", "reversal", 0))
        self.assertTrue(p["at_level"])
        self.assertEqual(p["extreme"], 99.3)                           # the low of both candles

    def test_hammer_at_the_range_low_and_shooting_star_at_the_high(self):
        p = find(base() + [bar(99.8, 100.0, 98.6, 99.9)])
        self.assertEqual((p["key"], p["name"], p["at_level"]), ("pin_bar", "hammer (pin bar)", True))
        s = find(base() + [bar(100.2, 101.4, 100.1, 100.1)], side=-1)
        self.assertEqual((s["key"], s["name"], s["at_level"], s["extreme"]),
                         ("pin_bar", "shooting star (pin bar)", True, 101.4))

    def test_morning_star(self):
        kl = base() + [bar(100.4, 100.5, 99.2, 99.3), bar(99.3, 99.5, 99.0, 99.4), bar(99.4, 100.4, 99.3, 100.2)]
        p = find(kl)
        self.assertEqual((p["key"], p["name"], p["at_level"], p["extreme"]), ("star", "morning star", True, 99.0))

    def test_a_reversal_away_from_any_level_is_not_at_level(self):
        rise = [bar(100 + i, 101.2 + i, 99.9 + i, 101 + i) for i in range(10)]
        kl = base() + rise + [bar(110.8, 111.0, 109.4, 110.9)]          # a hammer high above the range
        p = find(kl)
        self.assertEqual(p["key"], "pin_bar")
        self.assertFalse(p["at_level"])

    def test_three_white_soldiers_are_continuation_never_at_level(self):
        kl = base() + [bar(99.6, 100.5, 99.5, 100.4), bar(100.1, 101.3, 100.0, 101.2), bar(100.9, 102.1, 100.8, 102.0)]
        p = find(kl)
        self.assertEqual((p["key"], p["kind"], p["at_level"]), ("three_soldiers", "continuation", False))
        self.assertEqual(find(kl, side=-1), None)

    def test_inside_bar_breakout(self):
        kl = base() + [bar(99.6, 101.0, 99.0, 100.5), bar(99.9, 100.7, 99.8, 100.3), bar(100.3, 101.4, 100.2, 101.2)]
        self.assertEqual(find(kl)["key"], "inside_break")

    def test_a_pattern_closed_through_since_no_longer_counts(self):
        kl = base() + [bar(100.2, 100.3, 99.3, 99.5), bar(99.5, 100.6, 99.3, 100.4)]
        self.assertEqual(find(kl + [bar(100.4, 100.5, 99.5, 99.6)])["ago"], 1)   # still above its low: fresh
        self.assertIsNone(find(kl + [bar(100.4, 100.5, 98.9, 99.0)]))           # closed under its low: failed
        self.assertIsNone(find(base()))                                        # nothing in a quiet range

    def test_the_stop_goes_past_the_pattern(self):
        kl = base(80)
        a = q.atr(kl)
        free = q.levels(100.0, True, kl, kl, kl, a, 0.5)
        held = q.levels(100.0, True, kl, kl, kl, a, 0.5, beyond=98.0)
        self.assertGreater(free["stop"], 98.0)
        self.assertLess(held["stop"], 98.0)                             # past the pattern's low, plus the buffer


class CandleType(unittest.TestCase):
    def test_classified_after_trend_pullback_before_the_catch_all(self):
        types = TypeBook(None).resolved()
        f = {"candle_at_level": True, "htf": "against", "breakout": False, "sweep": False, "funding": 0.0}
        self.assertEqual(st.classify(f, types)["key"], "candle_reversal")
        self.assertEqual(st.classify(dict(f, htf="with"), types)["key"], "trend_pullback")    # with the trend first
        self.assertEqual(st.classify(dict(f, candle_at_level=False), types)["key"], "mean_reversion")

    def test_its_requirements(self):
        t = TypeBook(None).get("candle_reversal")
        self.assertEqual(st.unmet({"rvol": 1.2, "candle_against": False}, t), [])
        why = st.unmet({"rvol": 0.6, "candle_against": True}, t)
        self.assertEqual(len(why), 2)
        self.assertIn("not: a candlestick pattern against it", why[1])

    def test_a_custom_type_can_pick_one_pattern(self):
        t = st.validate({"name": "Hammers", "when": [{"k": "candle_pattern", "op": "is", "v": "pin_bar"}]})
        self.assertTrue(st.check({"candle_pattern": "pin_bar"}, t["when"][0]))
        with self.assertRaisesRegex(ValueError, "Candlestick pattern its way"):
            st.validate({"name": "Bad", "when": [{"k": "candle_pattern", "op": "is", "v": "doji"}]})

    def test_assess_reports_the_candles_and_their_features(self):
        r = q.assess(**kw())
        self.assertIn("candles", r)
        for k in ("candle", "candle_at_level", "candle_pattern", "candle_against"):
            self.assertIn(k, r["features"])
        self.assertIn(r["features"]["candle_pattern"], st.FIELDS["candle_pattern"][2])


if __name__ == "__main__":
    unittest.main()
