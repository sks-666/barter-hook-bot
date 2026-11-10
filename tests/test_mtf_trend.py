"""The multi-timeframe helpers and trend labels must never use a candle that had not closed."""
import random
import unittest

from swingkit import mtf, trend
from swingkit.indicators import adx
from swingkit.models import Series

DAY = mtf.DAY_MS


def walk(n: int, seed: int = 1, start_ts: int = 4 * DAY) -> Series:           # starts on a Monday 00:00 UTC
    rnd, px, rows = random.Random(seed), 100.0, []
    for i in range(n):
        o = px
        px *= 1 + rnd.gauss(0.001, 0.03)
        rows.append((start_ts + i * DAY, o, max(o, px) * 1.01, min(o, px) * 0.99, px, 1.0))
    return Series(*[list(c) for c in zip(*rows)])


class ResampleAlignTests(unittest.TestCase):
    def test_weekly_candles_are_complete_weeks_only(self):
        s = walk(30)                                                          # 4 full weeks + 2 days
        w = mtf.resample(s, "1w", DAY)
        self.assertEqual(len(w), 4)
        self.assertEqual(w.o[0], s.o[0])
        self.assertEqual(w.c[0], s.c[6])
        self.assertEqual(w.h[0], max(s.h[:7]))
        self.assertEqual(w.l[1], min(s.l[7:14]))

    def test_a_week_with_a_missing_day_is_dropped(self):
        s = walk(21)
        for col in (s.ts, s.o, s.h, s.l, s.c, s.v):
            del col[9]
        self.assertEqual(len(mtf.resample(s, "1w", DAY)), 2)

    def test_align_never_returns_a_candle_still_forming(self):
        s = walk(28)
        w = mtf.resample(s, "1w", DAY)
        idx = mtf.align(w.ts, mtf.BAR_MS["1w"], s.ts, DAY)
        self.assertEqual(idx[:6], [-1] * 6)                                   # days 0-5 of week 0: no weekly candle closed yet
        self.assertEqual(idx[6], 0)                                           # the 7th day's close closes week 0
        self.assertEqual(idx[13], 1)
        for i, j in enumerate(idx):
            if j >= 0:
                self.assertLessEqual(w.ts[j] + mtf.BAR_MS["1w"], s.ts[i] + DAY)

    def test_4h_candles_see_the_daily_candle_only_after_it_closes(self):
        d = walk(5)
        ltf = [d.ts[1] + k * 14_400_000 for k in range(6)]                    # the six 4h candles of day 1
        idx = mtf.align(d.ts, DAY, ltf, 14_400_000)
        self.assertEqual(idx, [0, 0, 0, 0, 0, 1])                             # day 1 itself closes with the last 4h candle


class TrendNoLookAheadTests(unittest.TestCase):
    def test_a_label_never_changes_when_later_candles_change(self):
        full = walk(700, seed=3)
        cut = 450
        head = Series(*[col[:cut] for col in (full.ts, full.o, full.h, full.l, full.c, full.v)])
        for rule in trend.RULES:
            with self.subTest(rule=rule):
                a, b = trend.label(rule, full)[:cut], trend.label(rule, head)
                self.assertEqual(a, b)

    def test_warmup_is_blank_not_a_guess(self):
        s = walk(150)
        self.assertEqual(set(trend.t1(s)[:199]) | set(trend.t2(s)[:199]), {""})

    def test_a_steady_rise_is_up_and_a_steady_fall_is_down(self):
        n = 400
        up = Series(list(range(n)), [100 + i for i in range(n)], [101 + i for i in range(n)], [99 + i for i in range(n)],
                    [100.5 + i for i in range(n)], [1.0] * n)
        down = up.mirrored()
        self.assertEqual(trend.t1(up)[-1], "up")
        self.assertEqual(trend.t2(up)[-1], "up")
        self.assertEqual(trend.t1(down)[-1], "down")
        self.assertEqual(trend.t2(down)[-1], "down")

    def test_adx_is_high_in_a_trend_and_low_in_chop(self):
        n = 200
        trendy = [100 + i for i in range(n)]
        chop = [100 + (3 if i % 2 else -3) for i in range(n)]
        hi = adx([x + 1 for x in trendy], [x - 1 for x in trendy], trendy)[-1]
        lo = adx([x + 1 for x in chop], [x - 1 for x in chop], chop)[-1]
        self.assertGreater(hi, 40)
        self.assertLess(lo, 20)


if __name__ == "__main__":
    unittest.main()
