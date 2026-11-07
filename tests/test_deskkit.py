"""deskkit: the statistics, walk-forward research, gates, graveyard and promotion ladder of the desk protocol."""
import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist

from deskkit import stats
from deskkit.gates import GateInput, evaluate
from deskkit.graveyard import Graveyard
from deskkit.ladder import Ladder
from deskkit.report import desk_view, save_research
from deskkit.research import (Delayed, make_windows, neighbours, research_strategy, run_window, slice_series, Ctx)
from deskkit.rules import DeskRules
from swingkit.backtest import CostCfg, Trade
from swingkit.journal import Journal
from swingkit.models import Candle, Series
from swingkit.risk import RiskCfg

DAY = 86_400_000
BAR = 14_400_000
T0 = 1_700_000_000_000 // BAR * BAR


def candles(seed: int, n: int, start: float = 100.0, crash_at: int | None = None, crash_len: int = 180) -> Series:
    """A 4h random walk with trending stretches (so the strategies find setups); optionally a 45% fall (a crash)."""
    rng, price, out, drift = random.Random(seed), start, [], 0.0
    for i in range(n):
        if i % 120 == 0:
            drift = rng.choice([-0.0015, 0.0, 0.0025, 0.004])
        if crash_at is not None and crash_at <= i < crash_at + crash_len:
            drift = -0.0033
        o = price
        price *= 1 + drift + rng.gauss(0, 0.012)
        out.append(Candle(T0 + i * BAR, o, max(o, price) * (1 + abs(rng.gauss(0, 0.003))),
                          min(o, price) * (1 - abs(rng.gauss(0, 0.003))), price, rng.uniform(1e4, 5e4)))
    return Series.from_candles(out)


def gentle(seed: int, n: int, start: float = 50_000.0) -> Series:
    """A genuinely calm series: a slow rise with tiny noise, so it can never contain a 30% fall."""
    rng, out = random.Random(seed), []
    for i in range(n):
        o, c = start * (1 + 0.0001 * i) * (1 + rng.gauss(0, 0.001)), start * (1 + 0.0001 * (i + 1)) * (1 + rng.gauss(0, 0.001))
        out.append(Candle(T0 + i * BAR, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 1e4))
    return Series.from_candles(out)


def small_rules(**kw) -> DeskRules:
    base = dict(train_months=2, test_months=2, step_months=2, warmup_bars=50, lookback_bars=150, min_oos_trades=5)
    return DeskRules(**{**base, **kw})


def market(n=2200, crash=True):
    data = {s: candles(i, n, 100 + 10 * i) for i, s in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT"))}
    btc = candles(99, n, 50_000.0, crash_at=1100) if crash else gentle(99, n)
    return data, btc


class StatsTests(unittest.TestCase):
    def test_moments_and_sharpe(self):
        r = [0.01, -0.01, 0.02, 0.0, 0.01]
        self.assertAlmostEqual(stats.mean(r), 0.006)
        self.assertAlmostEqual(stats.std(r), 0.0114017, places=6)
        self.assertAlmostEqual(stats.sharpe(r), 0.006 / 0.0114017, places=5)
        self.assertAlmostEqual(stats.sharpe_ann(r), stats.sharpe(r) * math.sqrt(365))        # crypto: 365, not 252
        self.assertEqual(stats.sharpe([0.01] * 5), 0.0)                                      # no variance: no ratio
        self.assertAlmostEqual(stats.kurtosis([1, -1] * 50), 1.0)                           # a coin flip: platykurtic
        self.assertAlmostEqual(stats.skewness([1, 2, 3, 4, 5]), 0.0)

    def test_deflated_sharpe_matches_the_formula(self):
        # daily SR 0.1 over 250 days, normal returns, no multiple testing (SR0 = 0)
        z = 0.1 * math.sqrt(249) / math.sqrt(1 + 2 / 4 * 0.1 ** 2)
        self.assertAlmostEqual(stats.deflated_sharpe(0.1, 250, 0.0, 3.0, 0.0), NormalDist().cdf(z), places=9)
        self.assertAlmostEqual(stats.deflated_sharpe(0.1, 250, 0.0, 3.0, 0.0), 0.9423, delta=0.002)
        # more trials -> a higher bar -> a lower DSR; fat tails and negative skew lower it too
        sr0 = stats.expected_max_sr(50, 0.0004)
        self.assertGreater(sr0, 0.0)
        self.assertLess(stats.deflated_sharpe(0.1, 250, 0.0, 3.0, sr0), stats.deflated_sharpe(0.1, 250, 0.0, 3.0, 0.0))
        self.assertLess(stats.deflated_sharpe(0.1, 250, -1.0, 9.0, 0.0), stats.deflated_sharpe(0.1, 250, 0.0, 3.0, 0.0))
        self.assertEqual(stats.expected_max_sr(1, 0.01), 0.0)                                # one trial: nothing to deflate
        self.assertGreater(stats.expected_max_sr(500, 0.0004), stats.expected_max_sr(5, 0.0004))
        self.assertEqual(stats.deflated_sharpe(0.1, 1, 0.0, 3.0, 0.0), 0.0)

    def test_losing_streak_limit_is_the_papers_example(self):
        self.assertEqual(stats.streak_limit(0.55), 8)                                        # "for p = 0.55 this gives 8"
        self.assertGreater(stats.streak_limit(0.30), stats.streak_limit(0.60))
        self.assertEqual(stats.streak_limit(0.0), 1)
        self.assertEqual(stats.streak_limit(1.0), math.inf)

    def test_kelly_and_sizing_follow_the_papers_example(self):
        self.assertAlmostEqual(stats.kelly(0.55, 1.5), 0.25)
        self.assertAlmostEqual(stats.size_fraction(0.55, 1.5, 0.01, 100), 0.01)               # quarter Kelly 6.25%, the cap binds
        self.assertAlmostEqual(stats.size_fraction(0.55, 1.5, 0.01, 10), 0.005)              # < 30 trades: half of the cap
        self.assertEqual(stats.size_fraction(0.30, 1.0, 0.01, 100), 0.0)                     # no edge on its own numbers

    def test_consistency_z(self):
        self.assertAlmostEqual(stats.consistency_z([0.5] * 25, 0.5, 1.0), 0.0)
        self.assertAlmostEqual(stats.consistency_z([-0.5] * 25, 0.5, 1.0), -5.0)             # (-1.0) / (1/5)
        self.assertIsNone(stats.consistency_z([0.1], 0.5, 1.0))

    def test_bootstrap_drift(self):
        oos = [1.0] * 30
        self.assertTrue(stats.bootstrap_drift(oos, [-1.0] * 12)["breach"])
        self.assertFalse(stats.bootstrap_drift(oos, [1.0] * 12)["breach"])
        self.assertIsNone(stats.bootstrap_drift(oos, []))
        mixed = [2.0, -1.0] * 15
        self.assertEqual(stats.bootstrap_drift(mixed, [0.5] * 10), stats.bootstrap_drift(mixed, [0.5] * 10))   # seeded

    def test_daily_returns_and_drawdown(self):
        eq = [(0, 100.0), (DAY // 2, 101.0), (DAY, 102.0), (DAY + 10, 104.04), (2 * DAY, 100.0)]
        got = stats.dated_daily_returns(eq, 0, 100.0)
        self.assertEqual([d for d, _ in got], [0, 1, 2])
        for (_, r), want in zip(got, (0.01, 104.04 / 101 - 1, 100 / 104.04 - 1)):
            self.assertAlmostEqual(r, want)
        day, r = stats.dated_daily_returns(eq, DAY, 100.0)[0]                                # only points from `start_ts` on
        self.assertEqual(day, 1)
        self.assertAlmostEqual(r, 0.0404)
        self.assertAlmostEqual(stats.max_drawdown([100, 120, 90, 130, 117]), 0.25)


class RulesAndGraveyardTests(unittest.TestCase):
    def test_rules_load_override_and_refuse_typos(self):
        d = Path(tempfile.mkdtemp())
        (d / "r.toml").write_text('[desk]\nversion = "v9"\nmin_oos_trades = 12\nd_plan_pct = 40\n[d_plan]\ntrend = 30\n')
        r = DeskRules.load(d / "r.toml")
        self.assertEqual((r.version, r.min_oos_trades, r.plan_dd("trend"), r.plan_dd("smc")), ("v9", 12, 30.0, 40.0))
        self.assertEqual(DeskRules.load(d / "missing.toml").version, "paper-v1")             # defaults without a file
        (d / "bad.toml").write_text("[desk]\ndsr_mni = 0.9\n")
        with self.assertRaises(ValueError):
            DeskRules.load(d / "bad.toml")                                                   # a typo never falls back silently
        self.assertEqual(DeskRules.load(Path("desk_rules.toml")).version, "paper-v1")        # the shipped file parses

    def test_every_configuration_tested_is_a_counted_trial_even_a_retest(self):
        g = Graveyard(":memory:")
        kw = dict(rules_version="v", family="trend", fp="abc", label="declared", params={}, symbols=["BTCUSDT"], interval="4h",
                  market="futures", skew=0.0, kurt=3.0, net_pct=1.0, oos_trades=10)
        self.assertEqual(g.count_and_variance("trend"), (0, 0.0))
        g.add(sr=0.10, n_obs=100, **kw)
        self.assertEqual(g.count_and_variance("trend"), (1, 0.0))                            # variance needs two trials
        g.add(sr=0.20, n_obs=100, **kw)                                                      # the SAME fingerprint, tested again
        n, var = g.count_and_variance("trend")
        self.assertEqual(n, 2)
        self.assertAlmostEqual(var, 0.005)
        self.assertEqual(g.seen("abc"), 2)
        self.assertEqual(g.count_and_variance("swing"), (0, 0.0))                            # families are separate
        self.assertEqual(g.families(), {"trend": 2})


class WindowTests(unittest.TestCase):
    def test_windows_roll_and_stay_inside_the_data(self):
        rules = DeskRules(train_months=12, test_months=3, step_months=3, warmup_bars=250)
        first, last = T0, T0 + 1095 * DAY
        ws = make_windows(first, last, BAR, rules)
        self.assertEqual(len(ws), 7)
        for w in ws:
            self.assertEqual((w.train_end, w.test_start), (w.test_start, w.train_end))
            self.assertLessEqual(w.test_end, last + BAR)
        self.assertEqual(ws[1].test_start - ws[0].test_start, int(3 * 30.4375 * DAY))
        self.assertGreaterEqual(ws[0].train_start, first + 250 * BAR)                        # warm-up candles are never judged
        self.assertEqual(make_windows(first, first + 200 * DAY, BAR, rules), [])

    def test_slice_neighbours_and_the_delay_wrapper(self):
        s = candles(1, 100)
        sl = slice_series(s, T0 + 10 * BAR, T0 + 20 * BAR)
        self.assertEqual((sl.ts[0], sl.ts[-1], len(sl)), (T0 + 10 * BAR, T0 + 19 * BAR, 10))
        nb = neighbours({"fast": 50, "atr_stop": 3.0, "flag": True, "name": "x"}, 0.2)
        self.assertEqual([(lab, p["fast"], p["atr_stop"]) for lab, p in nb],
                         [("fast 50 -> 40", 40, 3.0), ("fast 50 -> 60", 60, 3.0), ("atr_stop 3 -> 2.4", 50, 2.4), ("atr_stop 3 -> 3.6", 50, 3.6)])
        self.assertEqual([p["fast"] for _, p in neighbours({"fast": 1}, 0.2)], [])           # 1 x 0.8 and 1.2 both round to 1

        class Inner:
            name = "x"

            def entry(self, i):
                return f"setup@{i}"
        d = Delayed(Inner())
        self.assertEqual((d.entry(5), d.entry(0), d.name), ("setup@4", None, "x"))           # one bar later than the inner idea

    def test_a_window_never_sees_candles_after_its_end(self):
        data, btc = market(1500, crash=False)
        rules = small_rules()
        ctx = Ctx("trend", data, btc, None, rules, RiskCfg(), CostCfg(), BAR)
        a, b = T0 + 500 * BAR, T0 + 860 * BAR
        before = run_window(ctx, {}, a, b)
        for s in data.values():                                                              # rewrite the future after b
            for i in range(len(s)):
                if s.ts[i] >= b:
                    s.o[i] = s.h[i] = s.l[i] = s.c[i] = 1e-3
        after = run_window(ctx, {}, a, b)
        self.assertEqual((before.net_pct, len(before.trades), before.dated), (after.net_pct, len(after.trades), after.dated))
        self.assertTrue(all(t.entry_ts >= a for t in before.trades))                         # warm-up candles never trade


class ResearchTests(unittest.TestCase):
    def run_research(self, grave, data=None, btc=None, **kw):
        d, b = market() if data is None else (data, btc)
        return research_strategy("trend", d, b, None, small_rules(), grave, interval="4h", **kw)

    def test_a_full_research_run_reports_every_gate_and_counts_its_trials(self):
        g = Graveyard(":memory:")
        res = self.run_research(g)
        self.assertEqual([x["name"] for x in res["gates"]],
                         ["Trade count", "Out-of-sample", "Deflated Sharpe", "Walk-forward", "Crash window", "Latest year",
                          "Parameter plateau", "Noise test", "Capacity", "Frequency"])
        self.assertEqual(res["approved"], not res["failed"])
        n_cfg = 1 + len(neighbours(res["params"], 0.2))
        self.assertEqual(res["trials"], n_cfg)                                               # declared + every neighbour
        self.assertEqual(g.count_and_variance("trend")[0], n_cfg)
        self.assertEqual(res["seen_before"], 0)
        self.assertEqual(len(res["windows"]), res["stats"]["windows"])
        json.dumps(res)                                                                      # plain data: it is written to disk
        again = self.run_research(g)                                                         # a retest is a NEW trial
        self.assertEqual(again["trials"], 2 * n_cfg)
        self.assertEqual(again["seen_before"], 1)
        self.assertLessEqual(again["stats"]["dsr"], res["stats"]["dsr"] + 1e-9)             # more trials never help

    def test_the_crash_gate_needs_a_crash_in_a_test_window(self):
        with_crash = self.run_research(Graveyard(":memory:"))
        gate = next(x for x in with_crash["gates"] if x["name"] == "Crash window")
        self.assertIn("drawdown", gate["value"])
        self.assertTrue(any(w["crash"] for w in with_crash["windows"]))
        data, btc = market(crash=False)
        calm = self.run_research(Graveyard(":memory:"), data, btc)
        gate = next(x for x in calm["gates"] if x["name"] == "Crash window")
        self.assertEqual((gate["ok"], gate["value"]), (False, "no crash window"))            # cannot pass without one
        self.assertIn("Crash window", calm["failed"])
        self.assertFalse(calm["approved"])

    def test_fit_mode_adds_the_selection_procedure_as_a_trial_and_stays_honest(self):
        g = Graveyard(":memory:")
        res = self.run_research(g, fit=True)
        n_cfg = 1 + len(neighbours(res["params"], 0.2))
        self.assertEqual(res["trials"], n_cfg + 1)
        self.assertTrue(res["fit"])
        self.assertIn("walk-forward fit", [t["label"] for t in g.trials("trend")])

    def test_too_little_history_is_refused_in_words(self):
        res = research_strategy("trend", {"A": candles(1, 300)}, None, None, small_rules(train_months=12), Graveyard(":memory:"))
        self.assertFalse(res["approved"])
        self.assertIn("not enough history", res["error"])
        with self.assertRaises(ValueError):
            research_strategy("nope", {"A": candles(1, 300)}, None, None, small_rules(), Graveyard(":memory:"))


class GateTests(unittest.TestCase):
    def good(self, **kw):
        base = dict(strategy="trend", oos_trades=40, oos_days=100, oos_net_pct=25.0, dsr=0.97, n_trials=13, positive_windows=6,
                    windows=8, crash_windows=[(3, 20.0)], latest_year_pct=12.0, neighbours=[("a", 5.0), ("b", 3.0)],
                    noise={"1-bar delay": 4.0, "2x slippage": 6.0}, capacity_ratio_pct=0.2, capacity_symbol="BTCUSDT")
        return GateInput(**{**base, **kw})

    def test_all_pass_and_each_gate_can_fail_alone(self):
        rules = DeskRules()
        self.assertTrue(all(g.ok for g in evaluate(self.good(), rules)))
        failed = [g.name for g in evaluate(self.good(oos_trades=12, oos_days=20), rules) if not g.ok]
        self.assertEqual(failed, ["Trade count"])                                            # few trades, but at a healthy rate
        for field, bad, gate in (("oos_net_pct", -1.0, "Out-of-sample"), ("dsr", 0.90, "Deflated Sharpe"),
                                 ("positive_windows", 5, "Walk-forward"), ("crash_windows", [(1, 40.0)], "Crash window"),
                                 ("latest_year_pct", -2.0, "Latest year"), ("neighbours", [("a", 5.0), ("b", -1.0)], "Parameter plateau"),
                                 ("noise", {"1-bar delay": -1.0, "2x slippage": 3.0}, "Noise test"), ("capacity_ratio_pct", 2.5, "Capacity"),
                                 ("oos_days", 400, "Frequency")):
            failed = [g.name for g in evaluate(self.good(**{field: bad}), rules) if not g.ok]
            self.assertEqual(failed, [gate], field)

    def test_the_crash_limit_scales_with_the_planned_drawdown(self):
        rules = DeskRules(d_plan={"trend": 10.0})
        g = next(x for x in evaluate(self.good(crash_windows=[(1, 20.0)]), rules) if x.name == "Crash window")
        self.assertFalse(g.ok)                                                               # 20% > 1.5 x 10%
        self.assertIn("15.0%", g.threshold)

    def test_a_strategy_with_nothing_to_test_fails_closed(self):
        rules = DeskRules()
        failed = [g.name for g in evaluate(self.good(neighbours=[], noise={}, capacity_ratio_pct=None, crash_windows=[],
                                                     latest_year_pct=None, windows=0, positive_windows=0), rules) if not g.ok]
        self.assertEqual(set(failed), {"Parameter plateau", "Noise test", "Capacity", "Crash window", "Latest year", "Walk-forward"})


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def jt(strategy, closed_days_ago, r, now, opened_days_ago=None):
    closed = now - closed_days_ago * DAY
    return dict(strategy=strategy, closed_at=closed, opened_at=now - (opened_days_ago or closed_days_ago + 1) * DAY, r_multiple=r,
                pnl_usd=r * 100.0)


def research_for(name="trend", approved=True, win=0.5, mean_r=0.3, std_r=1.0, r=None):
    r = r or [2.0, -1.0] * 20
    return {"strategies": {name: {"approved": approved, "baseline": dict(win_rate=win, mean_r=mean_r, std_r=std_r, n=40, r=r)}}}


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.now = int(self.clock() * 1000)
        self.dir = Path(tempfile.mkdtemp())
        self.lad = Ladder(self.dir / "ladder.json", DeskRules(), self.clock)

    def trades(self, paper=(), live=()):
        return {"paper": list(paper), "live": list(live)}

    def paper_run(self, n, days_span=20, r=0.3):
        return [jt("trend", days_span - i * days_span / n, r if i % 2 else -0.5, self.now, days_span + 1) for i in range(n)]

    def test_rung_one_advances_by_itself_only_with_trades_days_and_passed_gates(self):
        t = self.trades(self.paper_run(25))
        quiet = dict(mean_r=-0.1, std_r=0.4, r=[0.3, -0.5] * 20)                            # what these paper trades look like
        a = self.lad.check(["trend"], t, research_for(approved=False, **quiet))[0]
        self.assertEqual((a["rung"], a["eligible"]), (1, False))
        self.assertFalse(a["checks"][0]["ok"])                                               # gates not passed
        a = self.lad.check(["trend"], self.trades(self.paper_run(5)), research_for(**quiet))[0]
        self.assertEqual(a["rung"], 1)                                                       # too few trades
        a = self.lad.check(["trend"], t, research_for(**quiet))[0]
        self.assertEqual((a["rung"], a["rung_name"]), (2, "Match"))
        self.assertEqual(Ladder(self.dir / "ladder.json").state["strategies"]["trend"]["rung"], 2)       # and it is saved

    def test_the_consistency_test_sends_a_failing_strategy_back_to_research(self):
        e = self.lad.ensure("trend", self.now - 30 * DAY)
        e["rung"], e["rung_start"] = 2, self.now - 30 * DAY
        bad = [jt("trend", 25 - i, -1.0, self.now) for i in range(22)]
        a = self.lad.check(["trend"], self.trades(bad), research_for(mean_r=0.5, std_r=1.0, win=0.4, r=[1.0] * 20))[0]
        self.assertLessEqual(a["z"], -2)
        self.assertEqual(a["status"], "needs_research")

    def test_every_step_towards_real_capital_needs_the_operator(self):
        e = self.lad.ensure("trend", self.now - 60 * DAY)
        e["rung"], e["rung_start"] = 2, self.now - 40 * DAY
        ok = [jt("trend", 30 - i, 0.3 if i % 2 else -0.2, self.now) for i in range(25)]
        res = research_for(mean_r=0.05, std_r=1.0)
        a = self.lad.check(["trend"], self.trades(ok), res)[0]
        self.assertEqual(a["rung"], 2)                                                       # eligible, but NOT moved by itself
        self.assertTrue(a["eligible"])
        self.assertEqual(self.lad.promote("trend", self.trades(ok), res, "looks fine")["rung"], 3)
        with self.assertRaises(ValueError) as cm:                                            # rung 3 has no live trades yet
            self.lad.promote("trend", self.trades(ok), res)
        self.assertIn("Live trades", str(cm.exception))
        live = [jt("trend", 10 - i * 0.4, 0.1 if i % 2 else -0.1, self.now) for i in range(21)]
        e["rung_start"] = self.now - 12 * DAY
        self.assertEqual(self.lad.promote("trend", self.trades(ok, live), res)["rung"], 4)

    def test_a_losing_streak_pauses_and_only_the_operator_resumes(self):
        e = self.lad.ensure("trend", self.now - 30 * DAY)
        res = research_for(win=0.55)
        losers = [jt("trend", 20 - i, -1.0, self.now) for i in range(9)]                     # L_max for p = 0.55 is 8
        a = self.lad.check(["trend"], self.trades(losers), res)[0]
        self.assertEqual((a["streak"], a["streak_limit"], a["status"]), (9, 8, "paused"))
        with self.assertRaises(ValueError):
            self.lad.promote("trend", self.trades(losers), res)
        self.assertEqual(self.lad.resume("trend", rung=2, note="reviewed")["status"], "active")
        self.assertEqual(e["rung"], 2)

    def test_drift_below_the_bootstrap_demotes_to_rung_one(self):
        e = self.lad.ensure("trend", self.now - 60 * DAY)
        e["rung"], e["rung_start"] = 3, self.now - 30 * DAY
        live = [jt("trend", 25 - 2 * i, -0.8 if i % 3 else 0.2, self.now) for i in range(12)]
        a = self.lad.check(["trend"], self.trades(live=live), research_for(win=0.6, r=[1.0] * 30))[0]
        self.assertTrue(a["drift"]["breach"])
        self.assertEqual((a["rung"], a["status"]), (1, "active"))                            # the strategy restarts at rung 1

    def test_a_strategy_too_rare_to_evaluate_is_retired(self):
        self.lad.ensure("trend", self.now - 70 * DAY)
        a = self.lad.check(["trend"], self.trades([jt("trend", 5, 0.1, self.now)]), research_for())[0]
        self.assertEqual(a["status"], "retired")

    def test_failures_and_the_readonly_view(self):
        e = self.lad.ensure("trend", self.now)
        e["rung"] = 4
        self.assertEqual(self.lad.fail("trend", "fill 0.9% from plan")["rung"], 3)           # rungs 3-5: one rung down
        e["rung"] = 2
        self.assertEqual(self.lad.fail("trend", "no stop on the exchange")["status"], "needs_research")   # rungs 1-2: research
        ro = Ladder(self.dir / "ro.json", DeskRules(), self.clock, readonly=True)
        ro.check(["trend"], self.trades(self.paper_run(25)), research_for(mean_r=-0.1, std_r=0.4, r=[0.3, -0.5] * 20))
        self.assertFalse((self.dir / "ro.json").exists())                                    # the web page never writes state


class ReportTests(unittest.TestCase):
    def test_the_desk_view_combines_research_ladder_and_journal(self):
        d = Path(tempfile.mkdtemp())
        j = Journal(d / "swing.db")
        tr = Trade("trend", "BTCUSDT", "long", T0, T0 + DAY, 100.0, 105.0, 95.0, None, 1.0, 5.0, 5.0, 0.1, 0.0, 1.0, 6, "in", "out")
        for _ in range(3):
            j.record(tr, mode="paper")
        res = research_for()
        res["strategies"]["trend"].update(gates=[], failed=[], stats={}, trials=3, params={})
        save_research(d / "desk", {"generated": 1, "rules_version": "paper-v1", "symbols": ["BTCUSDT"], "interval": "4h"}, res["strategies"])
        view = desk_view(d / "desk", [d / "swing.db"], DeskRules())
        by = {s["name"]: s for s in view["strategies"]}
        self.assertEqual(set(by), {"trend", "swing", "smc"})
        self.assertEqual(by["trend"]["ladder"]["trades"], 3)
        self.assertNotIn("r", by["trend"]["research"]["baseline"])                           # the long list is not sent to the page
        self.assertIsNone(by["swing"]["research"])
        self.assertFalse((d / "desk" / "ladder.json").exists())                              # a page view changes nothing
        save_research(d / "desk", {}, {"swing": {"approved": False, "baseline": {}}})        # merging keeps the other strategies
        self.assertEqual(set(json.loads((d / "desk" / "research.json").read_text())["strategies"]), {"trend", "swing"})


class CliAndRouteTests(unittest.IsolatedAsyncioTestCase):
    def cli(self, *argv):
        import contextlib
        import io

        from deskkit.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_research_status_ladder_and_graveyard_end_to_end_offline(self):
        from swingkit.data import cache_path, save_csv
        d = Path(tempfile.mkdtemp())
        data, btc = market(1600)
        for sym, s in {**data, "BTCUSDT": btc}.items():                                      # a candle cache, no network
            save_csv(cache_path(d / "klines", "futures", sym, "4h"),
                     [Candle(s.ts[i], s.o[i], s.h[i], s.l[i], s.c[i], s.v[i]) for i in range(len(s))])
        (d / "rules.toml").write_text('[desk]\nversion = "t1"\ntrain_months = 2\ntest_months = 2\nstep_months = 2\n'
                                      'warmup_bars = 50\nlookback_bars = 150\nmin_oos_trades = 5\n')
        base = ["--rules", str(d / "rules.toml"), "--desk-dir", str(d / "desk"), "--journal-db", str(d / "none.db")]
        code, out, err = self.cli(*base, "research", "--strategy", "trend", "--symbols", "AAAUSDT,BBBUSDT,CCCUSDT",
                                  "--offline", "--cache-dir", str(d / "klines"), "--days", "400", "--flat-funding")
        self.assertEqual(code, 0, err)                                                       # finishing is success, approved or not
        self.assertIn("=== trend", out)
        self.assertIn("Deflated Sharpe", out)
        self.assertIn("rules t1", out)
        saved = json.loads((d / "desk" / "research.json").read_text())
        self.assertEqual(saved["rules_version"], "t1")
        self.assertIn("trend", saved["strategies"])
        code, out, _ = self.cli(*base, "graveyard")
        self.assertIn("trend", out)
        code, out, _ = self.cli(*base, "status")
        self.assertEqual(code, 0)
        self.assertIn("rung 1 Paper", out)
        self.assertFalse((d / "desk" / "ladder.json").exists())                              # `status` only reads
        code, out, _ = self.cli(*base, "ladder", "check")
        self.assertEqual(code, 0)
        self.assertTrue((d / "desk" / "ladder.json").exists())
        code, _, err = self.cli(*base, "ladder", "promote", "trend")
        self.assertEqual(code, 2)                                                            # nothing earned yet: refused
        self.assertIn("not eligible", err)
        code, out, _ = self.cli(*base, "ladder", "fail", "trend", "--reason", "test breach")
        self.assertIn("needs_research", out)
        code, out, _ = self.cli(*base, "ladder", "resume", "trend", "--rung", "2")
        self.assertIn("rung 2", out)

    def test_a_misspelt_rule_stops_the_command(self):
        d = Path(tempfile.mkdtemp())
        (d / "bad.toml").write_text("[desk]\ndsr_minimum = 0.5\n")
        code, _, err = self.cli("--rules", str(d / "bad.toml"), "--desk-dir", str(d), "status")
        self.assertEqual(code, 2)
        self.assertIn("unknown desk rule", err)

    async def test_the_desk_route_serves_the_view_and_survives_a_broken_one(self):
        from types import SimpleNamespace

        from aiohttp.test_utils import TestClient, TestServer

        from arbbot.server import Dashboard
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        for desk, status in ((lambda: {"strategies": []}, 200), (None, 404), (lambda: 1 / 0, 500)):
            dash = Dashboard(SimpleNamespace(swing=None, snapshot=lambda: {}), cfg, Path("web"), market=object(), desk=desk)
            server = TestServer(dash.make_app())
            await server.start_server()
            dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
            dash.cfg.server.port = server.port
            async with TestClient(server) as client:
                r = await client.get("/api/market/desk", headers={"Host": f"127.0.0.1:{server.port}"})
                self.assertEqual(r.status, status)
                if status == 200:
                    self.assertEqual(await r.json(), {"strategies": []})

    def test_infinity_never_reaches_the_page(self):                                          # desk_view runs everything through this
        from deskkit.report import _finite
        self.assertEqual(_finite({"a": [math.inf, 1.0, {"b": math.nan}]}), {"a": [None, 1.0, {"b": None}]})


if __name__ == "__main__":
    unittest.main()
