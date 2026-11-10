"""The exit simulator: parity with the step 3 simulator, and each new exit behaves as documented."""
import math
import random
import unittest

from swingkit import exits
from swingkit.exits import Context, Spec, XCosts, run
from swingkit.indicators import atr
from swingkit.models import Series
from swingkit.triggers import Costs, simulate

H = 3_600_000
ZERO = XCosts(0, 0, 0, 0)
NAN = math.nan


def make(rows) -> Series:
    return Series([i * H for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows],
                  [r[2] for r in rows], [r[3] for r in rows], [1.0] * len(rows))


def walk(n: int, seed: int) -> Series:
    rnd, px, rows = random.Random(seed), 100.0, []
    for _ in range(n):
        o = px
        px *= 1 + rnd.gauss(0, 0.01)
        rows.append((o, max(o, px) * (1 + abs(rnd.gauss(0, 0.003))), min(o, px) * (1 - abs(rnd.gauss(0, 0.003))), px))
    return make(rows)


def ctx(n: int, last_pl=None, ema50=None) -> Context:
    return Context(ema50 or [NAN] * n, last_pl or [NAN] * n)


class ParityTests(unittest.TestCase):
    def test_atr_stop_with_fixed_target_reproduces_the_step3_simulator(self):
        s = walk(3000, seed=9)
        a = atr(s.h, s.l, s.c, 14)
        c = exits.context(s)
        base = Costs(5, 3, 1)
        hits = 0
        for i in random.Random(1).sample(range(20, 2900), 400):
            old = simulate(s, a, i, base)
            new = run(s, a, c, i, Spec(("atr", 1.5), ("rr", 2.0), 48), XCosts(5, 5, 3, 1))
            self.assertEqual(old is None, new is None)
            if old:
                hits += 1
                self.assertEqual((old.entry_i, old.exit_i, old.reason), (new.entry_i, new.exit_i, new.reason))
                self.assertAlmostEqual(old.r, new.r, places=9)
                self.assertAlmostEqual(old.bps, new.bps, places=7)
        self.assertGreater(hits, 300)


class ManagementTests(unittest.TestCase):
    def go(self, rows, spec, c=None, costs=ZERO, delay=0):
        s = make([(100, 100.5, 99.5, 100)] + rows)
        return run(s, [2.0] * len(s), c or ctx(len(s)), 0, spec, costs, delay)       # risk = 1.5 x 2 = 3: stop 97, +1R 103

    def test_partial_takes_half_at_1r_then_the_rest_at_the_target(self):
        r = self.go([(100, 104, 100.5, 103.5), (103, 110, 103, 109)], Spec(("atr", 1.5), ("partial", 2.0), 2))
        self.assertEqual(r.reason, "target")
        self.assertAlmostEqual(r.r, 1.5, places=9)                                   # 0.5 x 1R + 0.5 x 2R

    def test_partial_that_falls_back_the_same_candle_is_counted_as_break_even(self):
        r = self.go([(100, 104, 99.5, 103)], Spec(("atr", 1.5), ("partial", 2.0), 1))
        self.assertEqual(r.reason, "break_even")
        self.assertAlmostEqual(r.r, 0.5, places=9)                                   # half at +1R, half at 0

    def test_partial_stopped_at_break_even_after_the_scale_out(self):
        r = self.go([(100, 104, 100.5, 103.5), (103, 103.5, 99.0, 100)], Spec(("atr", 1.5), ("partial", 3.0), 2))
        self.assertEqual(r.reason, "break_even")
        self.assertAlmostEqual(r.r, 0.5, places=9)

    def test_trailing_stop_follows_a_confirmed_swing_low_upward_only(self):
        rows = [(100, 101, 99.9, 100.8), (100.8, 102, 100.6, 101.5), (101.5, 104, 100.4, 102)]
        c = ctx(4, last_pl=[NAN, 101.0, 104.0, 104.0])
        r = self.go(rows, Spec(("atr", 1.5), ("trail",), 3), c)
        # after candle 1 the stop rises to 101 - 0.5 = 100.5 (below the close); candle 3 trades down to 100.4 and stops there
        self.assertEqual(r.reason, "stop")
        self.assertAlmostEqual(r.r, 0.5 / 3, places=9)

    def test_ema_exit_leaves_at_the_next_open(self):
        rows = [(100, 101, 99.5, 99.0), (98.5, 99, 98, 98.8), (98.8, 99, 98.5, 98.8)]
        r = self.go(rows, Spec(("atr", 1.5), ("ema",), 3), ctx(4, ema50=[100.0] * 4))
        self.assertEqual((r.reason, r.exit_i), ("ema", 2))
        self.assertAlmostEqual(r.r, (98.5 - 100) / 3, places=9)

    def test_swing_stop_sits_below_the_confirmed_low_and_is_clamped(self):
        c = ctx(3, last_pl=[96.0, 96.0, 96.0])                                      
        r = self.go([(100, 100.5, 95.4, 96)], Spec(("swing",), ("rr", 2.0), 1), c)
        self.assertEqual(r.reason, "stop")
        self.assertAlmostEqual(r.r, -1.0, places=9)
        far = ctx(3, last_pl=[50.0, 50.0, 50.0])                                     # absurdly far: clamped to 4 ATR = 8 below
        r = self.go([(100, 100.5, 91.9, 92)], Spec(("swing",), ("rr", 2.0), 1), far)
        self.assertEqual(r.reason, "stop")
        self.assertAlmostEqual(r.r, -1.0, places=9)

    def test_a_delayed_entry_is_at_a_later_open(self):
        r = self.go([(100, 100.5, 99.5, 100), (110, 120, 109, 118)], Spec(("atr", 1.5), ("rr", 2.0), 1), delay=1)
        self.assertEqual(r.entry_i, 2)
        self.assertEqual(r.reason, "target")                                          # entered at 110: target 110 + 2 x 3 = 116

    def test_a_limit_target_pays_the_maker_fee_and_no_slippage(self):
        costs = XCosts(taker_fee_bps=5, maker_fee_bps=2, slippage_bps=0, funding_bps_8h=0)
        r = self.go([(100, 107, 99, 106)], Spec(("atr", 1.5), ("rr", 2.0), 1), costs=costs)
        self.assertAlmostEqual(r.r, (0.06 - 0.0005 - 0.0002) / 0.03, places=9)

    def test_unfinished_data_gives_no_verdict(self):
        self.assertIsNone(self.go([(100, 100.5, 99.5, 100)], Spec(("atr", 1.5), ("rr", 2.0), 5)))


if __name__ == "__main__":
    unittest.main()
