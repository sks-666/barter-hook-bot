"""Triggers read only closed candles, and the trade simulator charges what it says it charges."""
import random
import unittest

from swingkit import triggers, zones
from swingkit.indicators import atr
from swingkit.models import Series
from swingkit.triggers import Costs, simulate

H = 3_600_000


def make(rows) -> Series:
    return Series([i * H for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows],
                  [r[2] for r in rows], [r[3] for r in rows], [r[4] if len(r) > 4 else 1.0 for r in rows])


def walk(n: int, seed: int) -> Series:
    rnd, px, rows = random.Random(seed), 100.0, []
    for _ in range(n):
        o = px
        px *= 1 + rnd.gauss(0, 0.01)
        rows.append((o, max(o, px) * (1 + abs(rnd.gauss(0, 0.003))), min(o, px) * (1 - abs(rnd.gauss(0, 0.003))), px,
                     rnd.uniform(0.5, 2.0) * (4 if rnd.random() < 0.05 else 1)))
    return make(rows)


class NoLookAheadTests(unittest.TestCase):
    def test_signals_before_a_cut_do_not_change_when_later_candles_exist(self):
        full = walk(3000, seed=2)
        cut = 2000
        head = make(list(zip(full.o[:cut], full.h[:cut], full.l[:cut], full.c[:cut], full.v[:cut])))
        f_full, f_head = triggers.fire_all(full), triggers.fire_all(head)
        for name in triggers.TRIGGERS:
            with self.subTest(trigger=name):
                self.assertEqual(f_full[name][:cut], f_head[name])
                self.assertGreater(sum(f_head[name]), 3)                  # each trigger actually fires in the test data

    def test_zone_bands_before_a_cut_do_not_change(self):
        full = walk(1800, seed=4)
        cut = 1100
        head = make(list(zip(full.o[:cut], full.h[:cut], full.l[:cut], full.c[:cut], full.v[:cut])))
        bands = lambda s: (lambda a: zones.active_bands(s, a, zones.build(s, a)))(atr(s.h, s.l, s.c, 14))  # noqa: E731
        b_full, b_head = bands(full), bands(head)
        self.assertEqual([sorted(x) for x in b_full[:cut]], [sorted(x) for x in b_head])
        self.assertGreater(sum(len(x) for x in b_head), 100)

    def test_a_sweep_is_not_known_until_the_swing_low_is_confirmed(self):
        lows = [10, 9, 8, 7, 8, 9, 10, 10, 10, 6.5, 8]            # pivot low at 3 (7.0), known from candle 6; wick below at 9
        rows = [(l + 0.5, l + 1, l, l + 0.5) for l in lows]
        rows[9] = (9.0, 9.2, 6.5, 8.6)                              # sweeps 7.0 and closes back above it
        fired = triggers.fire_all(make(rows))["sweep"]
        self.assertEqual([i for i, f in enumerate(fired) if f], [9])


class SimulatorTests(unittest.TestCase):
    ZERO = Costs(0, 0, 0)

    def run_one(self, second, costs=ZERO, extra=()):
        s = make([(100, 100.5, 99.5, 100), second, *extra])
        a = [2.0] * len(s)                                         # risk = 1.5 ATR = 3 -> stop 97, target 106
        return simulate(s, a, 0, costs, max_hold=3)

    def test_target_is_plus_two_r_before_costs(self):
        r = self.run_one((100, 107, 99, 106))
        self.assertEqual((r.reason, round(r.r, 6)), ("target", 2.0))

    def test_stop_wins_when_one_candle_reaches_both(self):
        r = self.run_one((100, 107, 96, 100))
        self.assertEqual((r.reason, round(r.r, 6)), ("stop", -1.0))

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        r = self.run_one((100, 100.5, 99.5, 100), extra=((95, 96, 94, 95),))     # the next candle opens below the stop
        self.assertEqual(r.reason, "stop")
        self.assertAlmostEqual(r.r, -5 / 3, places=6)

    def test_fees_slippage_and_funding_come_off_every_trade(self):
        costs = Costs(fee_bps=5, slippage_bps=3, funding_bps_8h=1)
        r = self.run_one((100, 107, 99, 106), costs)
        ent = 100 * (1.0003)                                       # buys 3 bps worse
        risk = 3.0
        gross = ((ent + 2 * risk) - ent) / ent                     # the target is a limit: no exit slippage
        net = gross - 0.001 - 0.0001 * (1 / 8)
        self.assertAlmostEqual(r.r, net / (risk / ent), places=9)

    def test_a_time_exit_and_unfinished_data(self):
        flat = (100, 100.5, 99.5, 100)
        r = self.run_one(flat, extra=(flat, flat))
        self.assertEqual((r.reason, r.exit_i), ("time", 3))
        self.assertIsNone(self.run_one(flat))                      # the data ends before max_hold: no verdict

    def test_a_short_is_a_long_on_the_mirrored_series(self):
        s = make([(100, 100.5, 99.5, 100), (100, 100.5, 93, 94)]).mirrored()
        r = simulate(s, [2.0] * 2, 0, self.ZERO, max_hold=1)
        self.assertEqual(r.reason, "target")
        self.assertAlmostEqual(r.r, 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
