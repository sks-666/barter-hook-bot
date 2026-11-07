"""Walk-forward research: does a swingkit strategy have an edge that survives costs, noise and the multiple-testing problem?

For each strategy the DECLARED configuration (the rules as written) is run on rolling out-of-sample windows (default: fit on
12 months, judge on the next 3, roll by 3). With fit=True the best of the declared setting and its +-20% neighbours is chosen
on each window's training data and then judged, untouched, on the window after it. Either way every configuration tried is
written to the graveyard, so the deflated Sharpe ratio counts all of them.

Honesty rules, each tested: a test window is run on candles BEFORE its end only (no look-ahead), entries start at the window's
first candle (earlier candles only warm the indicators), each window starts from the planned capital, costs and funding are
those of the swingkit backtester, and a window with no trades is flat, not positive."""
import math
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from statistics import median

from swingkit.backtest import CostCfg, Trade, run_backtest
from swingkit.models import Series
from swingkit.risk import RiskCfg
from swingkit.strategies import REGISTRY, make

from . import stats
from .gates import GateInput, evaluate, gates_to_dicts
from .graveyard import Graveyard, fingerprint
from .rules import DeskRules

DAY_MS = stats.DAY_MS
MONTH_MS = int(30.4375 * DAY_MS)
HOUR_MS = 3_600_000


@dataclass
class Window:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def make_windows(first_ts: int, last_ts: int, bar_ms: int, rules: DeskRules) -> list[Window]:
    """Rolling windows over [first_ts, last_ts]. The first `warmup_bars` candles only warm the indicators up. A window is
    kept only when its whole test period is inside the data."""
    t = first_ts + rules.warmup_bars * bar_ms
    train, test, step = rules.train_months * MONTH_MS, rules.test_months * MONTH_MS, rules.step_months * MONTH_MS
    out = []
    while t + train + test <= last_ts + bar_ms:
        out.append(Window(t, t + train, t + train, t + train + test))
        t += step
    return out


def slice_series(s: Series, a: int, b: int) -> Series:
    i, j = bisect_left(s.ts, a), bisect_left(s.ts, b)
    return Series(s.ts[i:j], s.o[i:j], s.h[i:j], s.l[i:j], s.c[i:j], s.v[i:j])


class Delayed:
    """One more bar of latency: the idea decided at the close of candle i-1 is acted on at the close of candle i (and so
    fills one open later than the backtester normally would)."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def entry(self, i: int):
        return self.inner.entry(i - 1) if i > 0 else None


@dataclass
class WindowRun:
    net_pct: float = 0.0
    mdd_pct: float = 0.0
    dated: list[tuple[int, float]] = field(default_factory=list)      # (UTC day, daily return)
    trades: list[Trade] = field(default_factory=list)


@dataclass
class Ctx:
    name: str
    data: dict[str, Series]
    btc: Series | None
    funding: dict | None
    rules: DeskRules
    risk: RiskCfg
    costs: CostCfg
    bar_ms: int
    allow_short: bool = True


def run_window(ctx: Ctx, params: dict, a: int, b: int, *, delay: bool = False, slip_mult: float = 1.0) -> WindowRun:
    """The strategy with `params` on candles before b, entries from a: one window's result."""
    lo = a - ctx.rules.lookback_bars * ctx.bar_ms
    sl = {sym: slice_series(s, lo, b) for sym, s in ctx.data.items()}
    sl = {sym: s for sym, s in sl.items() if len(s) and s.ts[-1] >= a}
    if not sl:
        return WindowRun()
    costs = ctx.costs if slip_mult == 1.0 else CostCfg(ctx.costs.fee_bps, ctx.costs.slippage_bps * slip_mult,
                                                        ctx.costs.funding_bps_8h)
    make_one = lambda: make(ctx.name, allow_short=ctx.allow_short, **params)  # noqa: E731
    factory = (lambda: Delayed(make_one())) if delay else make_one
    res = run_backtest(sl, factory, ctx.risk, costs, ctx.rules.capital, ctx.bar_ms, ctx.funding, close_at_end=True, start_ts=a)
    eq = [(ts, e) for ts, e in res.equity if ts >= a]
    final = eq[-1][1] if eq else ctx.rules.capital
    return WindowRun(net_pct=(final / ctx.rules.capital - 1) * 100,
                     mdd_pct=stats.max_drawdown([ctx.rules.capital] + [e for _, e in eq]) * 100,
                     dated=stats.dated_daily_returns(res.equity, a, ctx.rules.capital), trades=res.trades)


def neighbours(params: dict, band: float) -> list[tuple[str, dict]]:
    """The declared setting's neighbours: each numeric parameter moved by +-band (the others unchanged)."""
    out, seen = [], {tuple(sorted(params.items()))}
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        for f in (1 - band, 1 + band):
            nv = max(1, round(v * f)) if isinstance(v, int) else float(f"{v * f:.4g}")
            cand = {**params, k: nv}
            key = tuple(sorted(cand.items()))
            if nv != v and key not in seen:
                seen.add(key)
                out.append((f"{k} {v:g} -> {nv:g}", cand))
    return out


def oos_stats(runs: list[WindowRun]) -> dict:
    dated = [d for w in runs for d in w.dated]
    daily = [r for _, r in dated]
    trades = [t for w in runs for t in w.trades]
    curve, v = [1.0], 1.0
    for r in daily:
        v *= 1 + r
        curve.append(v)
    return dict(sr=stats.sharpe(daily), sr_ann=stats.sharpe_ann(daily), n_obs=len(daily), skew=stats.skewness(daily),
                kurt=stats.kurtosis(daily), net_pct=stats.compounded(daily) * 100, trades=len(trades),
                mdd_pct=stats.max_drawdown(curve) * 100, positive=sum(1 for w in runs if w.net_pct > 0), windows=len(runs),
                dated=dated, trade_list=trades)


def _btc_crash(btc: Series | None, a: int, b: int, fall: float) -> bool:
    if btc is None:
        return False
    s = slice_series(btc, a, b)
    peak, worst = 0.0, 0.0
    for c in s.c:
        peak = max(peak, c)
        if peak > 0:
            worst = max(worst, (peak - c) / peak)
    return worst >= fall


def _capacity(trades: list[Trade], data: dict[str, Series], bar_ms: int) -> tuple[float | None, str]:
    """The largest planned position as a share of its coin's median hourly traded value, in %."""
    hourly = {}
    for sym, s in data.items():
        vals = [v * c for v, c in zip(s.v, s.c) if v > 0]
        if vals:
            hourly[sym] = median(vals) / (bar_ms / HOUR_MS)
    worst, who = None, ""
    for t in trades:
        h = hourly.get(t.symbol)
        if h:
            r = t.qty * t.entry_px / h * 100
            if worst is None or r > worst:
                worst, who = r, t.symbol
    return worst, who


def research_strategy(name: str, data: dict[str, Series], btc: Series | None, funding: dict | None, rules: DeskRules,
                      grave: Graveyard, *, params: dict | None = None, fit: bool = False, costs: CostCfg | None = None,
                      risk: RiskCfg | None = None, bar_ms: int = 14_400_000, interval: str = "4h", market: str = "futures",
                      allow_short: bool = True, log=None) -> dict:
    """Run every gate for one strategy. Returns the result record (also what the terminal shows)."""
    log = log or (lambda msg: None)
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; choose from {', '.join(REGISTRY)}")
    costs, risk = costs or CostCfg(), risk or RiskCfg(risk_pct=rules.risk_pct)
    ctx = Ctx(name, data, btc, funding, rules, risk, costs, bar_ms, allow_short)
    first = min(s.ts[0] for s in data.values() if len(s))
    last = max(s.ts[-1] for s in data.values() if len(s))
    windows = make_windows(first, last, bar_ms, rules)
    declared = {**REGISTRY[name].defaults, **(params or {})}
    base = dict(strategy=name, params=declared, fit=fit, rules_version=rules.version, generated=int(time.time() * 1000))
    need = rules.train_months + rules.test_months
    if not windows:
        return dict(base, approved=False, error=f"not enough history: a walk-forward needs at least {need} months "
                    f"(+{rules.warmup_bars} warm-up candles) of candles, the data has {(last - first) / MONTH_MS:.1f}",
                    gates=[], failed=["history"])
    configs = [("declared", declared)] + neighbours(declared, rules.plateau_band)
    symbols = sorted(data)
    fps = {label: fingerprint(strategy=name, params=p, symbols=symbols, interval=interval, market=market, fee=costs.fee_bps,
                              slip=costs.slippage_bps, fit=fit if label == "declared" else False,
                              windows=[rules.train_months, rules.test_months, rules.step_months]) for label, p in configs}
    seen_before = grave.seen(fps["declared"])
    log(f"{name}: {len(windows)} windows x {len(configs)} settings" + (" + training fits" if fit else ""))

    test: dict[str, list[WindowRun]] = {}
    train: dict[str, list[WindowRun]] = {}
    for label, p in configs:
        test[label] = [run_window(ctx, p, w.test_start, w.test_end) for w in windows]
        if fit:
            train[label] = [run_window(ctx, p, w.train_start, w.train_end) for w in windows]
    chosen = []                                                       # the setting used on each test window
    for i in range(len(windows)):
        pick = "declared"
        if fit:
            pick = max((label for label, _ in configs), key=lambda l: (train[l][i].net_pct, l == "declared"))
        chosen.append(pick)
    params_by_label = dict(configs)
    final_runs = [test[chosen[i]][i] for i in range(len(windows))]
    final = oos_stats(final_runs)

    ids = {}
    for label, p in configs:                                          # every configuration tried is a trial, kept forever
        s = oos_stats(test[label])
        ids[label] = grave.add(rules_version=rules.version, family=name, fp=fps[label], label=label, params=p, symbols=symbols,
                               interval=interval, market=market, sr=s["sr"], n_obs=s["n_obs"], skew=s["skew"],
                               kurt=s["kurt"], net_pct=s["net_pct"], oos_trades=s["trades"])
    if fit:                                                           # the selection procedure itself is one more trial
        ids["fit"] = grave.add(rules_version=rules.version, family=name, fp=fingerprint(strategy=name, fit=True, symbols=symbols),
                               label="walk-forward fit", params=declared, symbols=symbols, interval=interval, market=market,
                               sr=final["sr"], n_obs=final["n_obs"], skew=final["skew"], kurt=final["kurt"],
                               net_pct=final["net_pct"], oos_trades=final["trades"])
    n_trials, var_sr = grave.count_and_variance(name)
    sr0 = stats.expected_max_sr(n_trials, var_sr)
    dsr = stats.deflated_sharpe(final["sr"], final["n_obs"], final["skew"], final["kurt"], sr0)

    log(f"{name}: noise tests")
    noise = {}
    for label, kw in (("1-bar delay", dict(delay=True)), ("2x slippage", dict(slip_mult=2.0))):
        runs = [run_window(ctx, params_by_label[chosen[i]], w.test_start, w.test_end, **kw) for i, w in enumerate(windows)]
        noise[label] = oos_stats(runs)["net_pct"]

    crash = [(i, test[chosen[i]][i].mdd_pct) for i, w in enumerate(windows) if _btc_crash(btc, w.test_start, w.test_end, rules.crash_fall)]
    dated = final["dated"]
    latest = None
    if dated:
        cut = dated[-1][0] - 365
        latest = stats.compounded([r for d, r in dated if d > cut]) * 100
    cap, cap_sym = _capacity(final["trade_list"], data, bar_ms)
    oos_days = len(windows) * rules.test_months * 30.4375
    nb = [(label, oos_stats(test[label])["net_pct"]) for label, _ in configs if label != "declared"]
    gates = evaluate(GateInput(name, final["trades"], oos_days, final["net_pct"], dsr, n_trials, final["positive"], len(windows),
                               crash, latest, nb, noise, cap, cap_sym), rules)
    failed = [g.name for g in gates if not g.ok]
    approved = not failed
    grave.set_outcome(ids["fit" if fit else "declared"], "approved" if approved else "rejected: " + ", ".join(failed))

    r = [t.r_multiple for t in final["trade_list"]]
    wins, losses = [x for x in r if x > 0], [x for x in r if x <= 0]
    p_win = len(wins) / len(r) if r else 0.0
    payoff = (stats.mean(wins) / abs(stats.mean(losses))) if wins and losses and stats.mean(losses) else 0.0
    f_max = rules.risk_pct / 100
    return dict(base, approved=approved, failed=failed, gates=gates_to_dicts(gates), trials=n_trials, seen_before=seen_before,
                stats=dict(sr_ann=final["sr_ann"], dsr=dsr, sr0=sr0, net_pct=final["net_pct"], trades=final["trades"],
                           mdd_pct=final["mdd_pct"], oos_days=oos_days, windows=len(windows), positive_windows=final["positive"]),
                windows=[dict(start=w.test_start, end=w.test_end, net_pct=final_runs[i].net_pct, mdd_pct=final_runs[i].mdd_pct,
                              trades=len(final_runs[i].trades), crash=any(i == c for c, _ in crash), params=params_by_label[chosen[i]]
                              if chosen[i] != "declared" else None) for i, w in enumerate(windows)],
                baseline=dict(win_rate=p_win, payoff=payoff, mean_r=stats.mean(r), std_r=stats.std(r), n=len(r), r=r),
                sizing=dict(f_max=f_max, f=stats.size_fraction(p_win, payoff, f_max, len(r)), kelly=stats.kelly(p_win, payoff) if payoff else None))
