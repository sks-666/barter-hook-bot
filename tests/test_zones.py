"""Zones are known only when they could have been known, and touches/outcomes read only closed candles."""
import random
import unittest

from swingkit import zones
from swingkit.indicators import atr
from swingkit.models import Series

BAR = 14_400_000


def make(rows) -> Series:
    """rows: (open, high, low, close)"""
    return Series([i * BAR for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows],
                  [r[2] for r in rows], [r[3] for r in rows], [1.0] * len(rows))


def walk(n: int, seed: int) -> Series:
    rnd, px, rows = random.Random(seed), 100.0, []
    for _ in range(n):
        o = px
        px *= 1 + rnd.gauss(0, 0.012)
        rows.append((o, max(o, px) * (1 + abs(rnd.gauss(0, 0.004))), min(o, px) * (1 - abs(rnd.gauss(0, 0.004))), px))
    return make(rows)


def events(s: Series) -> set:
    a = atr(s.h, s.l, s.c, 14)
    return {(t.kind, t.born, t.i, t.n) for z in zones.build(s, a) for t in zones.touches(s, a, z)}


class NoLookAheadTests(unittest.TestCase):
    def test_touches_before_a_cut_do_not_change_when_later_candles_exist(self):
        full = walk(1500, seed=5)
        cut = 900
        head = make(list(zip(full.o[:cut], full.h[:cut], full.l[:cut], full.c[:cut])))
        in_full = {e for e in events(full) if e[2] < cut}
        self.assertGreater(len(in_full), 20)                                       # the test would be empty otherwise
        self.assertEqual(in_full, events(head))
        kinds = {e[0] for e in in_full}
        self.assertGreaterEqual(len(kinds), 5)                                     # most zone kinds actually occur

    def test_a_swing_low_zone_is_known_only_after_its_pivot_is_confirmed(self):
        lows = [10, 9, 8, 7, 8, 9, 10, 11, 10, 9, 8, 7.05, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
        s = make([(l + 0.5, l + 1.0, l, l + 0.5) for l in lows])
        a = [1.0] * len(s)
        zs = zones.pivot_zones(s, a, k=3)
        self.assertEqual(len(zs), 1)
        self.assertEqual(zs[0].born, 11 + 3)                                       # pivot at 11, confirmed 3 candles later
        self.assertAlmostEqual(zs[0].lo, 7.0)


class ZoneShapeTests(unittest.TestCase):
    def test_fair_value_gap(self):
        s = make([(10, 11, 9, 10)] * 3 + [(10, 12, 10, 12), (12, 14, 11.5, 13.5), (13.5, 15, 13, 14)])
        a = [1.0] * len(s)
        z = [x for x in zones.fvg_zones(s, a)]
        self.assertEqual([(x.born, x.lo, x.hi) for x in z], [(4, 11, 11.5), (5, 12, 13)])

    def test_order_block_is_the_last_down_candle_before_the_move(self):
        rows = [(10, 10.5, 9.5, 10)] * 6 + [(10, 10.2, 9.0, 9.2), (9.2, 12.5, 9.1, 12.4)]
        s = make(rows)
        z = zones.order_block_zones(s, [1.0] * len(s))
        self.assertEqual([(x.born, x.lo, x.hi) for x in z], [(7, 9.0, 10.2)])


class TouchAndOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.zone = zones.Zone("fvg", 0, 100.0, 101.0)

    def test_touch_needs_arrival_from_above_and_a_close_in_or_above_the_zone(self):
        rows = [(105, 106, 104, 105)] * 3 + [(105, 105, 100.5, 102)] + [(102, 104, 101.5, 103)] + [(103, 103, 99.0, 99.5)]
        s = make(rows)
        a = [1.0] * len(s)
        self.assertEqual([(t.i, t.n) for t in zones.touches(s, a, self.zone)], [(3, 1)])   # candle 5 closes below: no touch

    def test_a_zone_is_re_armed_only_after_price_leaves_by_an_atr(self):
        rows = [(105, 106, 104, 105)] * 2 + [(105, 105, 100.5, 102), (101.8, 101.9, 100.5, 101.5), (101.5, 101.8, 100.4, 101.2),
                                              (101.2, 104, 101, 103.5), (103.5, 103.6, 100.5, 101.0)]
        s = make(rows)
        got = [(t.i, t.n) for t in zones.touches(s, [1.0] * len(s), self.zone)]
        self.assertEqual(got, [(2, 1), (6, 2)])                                              # 3 and 4 are the same visit

    def test_a_close_far_below_kills_a_static_zone(self):
        rows = [(105, 106, 104, 105)] * 2 + [(105, 105, 98, 98.0)] + [(98, 106, 97, 105), (105, 105, 100.5, 102)]
        s = make(rows)
        self.assertEqual(zones.touches(s, [1.0] * len(s), self.zone), [])

    def test_outcome_up_down_none_and_unsettled(self):
        base = [(100, 100.5, 99.5, 100)]
        a = lambda s: [1.0] * len(s)  # noqa: E731
        up = make(base + [(100, 101.2, 99.8, 101)])
        down = make(base + [(100, 100.4, 98.8, 99)])
        both = make(base + [(100, 101.5, 98.5, 100)])
        flat = make(base + [(100, 100.5, 99.5, 100)] * 40)
        short = make(base + [(100, 100.5, 99.5, 100)] * 3)
        self.assertEqual(zones.outcome(up, a(up), 0), "up")
        self.assertEqual(zones.outcome(down, a(down), 0), "down")
        self.assertEqual(zones.outcome(both, a(both), 0), "down")                            # both in one candle: conservative
        self.assertEqual(zones.outcome(flat, a(flat), 0), "none")
        self.assertIsNone(zones.outcome(short, a(short), 0))                                  # data ended: not a "none"


if __name__ == "__main__":
    unittest.main()
