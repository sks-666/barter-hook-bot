"""The statistics of the protocol. All pure functions, no I/O.

Sharpe ratios here are PER PERIOD (daily) unless a name says `ann`: the deflated Sharpe ratio needs the daily Sharpe with
the number of DAILY observations; mixing an annualised Sharpe with a daily count overstates significance enormously.
Crypto trades every day, so the annualisation factor is sqrt(365)."""
import math
import random
from statistics import NormalDist

DAY_MS = 86_400_000
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def mean(x: list[float]) -> float:
    return sum(x) / len(x) if x else 0.0


def std(x: list[float]) -> float:
    """Sample standard deviation (n - 1)."""
    if len(x) < 2:
        return 0.0
    m = mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def sharpe(r: list[float]) -> float:
    """Per-period Sharpe ratio (risk-free rate 0)."""
    sd = std(r)
    return mean(r) / sd if sd > 0 else 0.0


def sharpe_ann(r: list[float]) -> float:
    return sharpe(r) * math.sqrt(365)


def skewness(r: list[float]) -> float:
    n = len(r)
    if n < 3:
        return 0.0
    m = mean(r)
    s2 = sum((v - m) ** 2 for v in r) / n
    return sum((v - m) ** 3 for v in r) / n / s2 ** 1.5 if s2 > 0 else 0.0


def kurtosis(r: list[float]) -> float:
    """NON-excess kurtosis (a normal distribution is 3), as the deflated Sharpe formula expects."""
    n = len(r)
    if n < 4:
        return 3.0
    m = mean(r)
    s2 = sum((v - m) ** 2 for v in r) / n
    return sum((v - m) ** 4 for v in r) / n / s2 ** 2 if s2 > 0 else 3.0


def compounded(r: list[float]) -> float:
    out = 1.0
    for v in r:
        out *= 1 + v
    return out - 1


def max_drawdown(curve: list[float]) -> float:
    """Largest peak-to-trough fall of an equity curve, as a fraction (eq. 6)."""
    peak, worst = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def dated_daily_returns(equity: list[tuple[int, float]], start_ts: int, base: float) -> list[tuple[int, float]]:
    """(UTC day number, return) of an equity series, one per UTC day: each day's LAST equity against the day before's (the
    first against `base`). Only points at or after start_ts count."""
    last: dict[int, float] = {}
    for ts, eq in equity:
        if ts >= start_ts:
            last[ts // DAY_MS] = eq
    out, prev = [], base
    for day in sorted(last):
        out.append((day, last[day] / prev - 1 if prev > 0 else 0.0))
        prev = last[day]
    return out


def daily_returns(equity: list[tuple[int, float]], start_ts: int, base: float) -> list[float]:
    return [r for _, r in dated_daily_returns(equity, start_ts, base)]


def expected_max_sr(n_trials: int, var_sr: float) -> float:
    """SR0: the Sharpe ratio the BEST of n_trials no-edge strategies is expected to show (exact form, not eq. 7)."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    g = EULER_GAMMA
    return math.sqrt(var_sr) * ((1 - g) * _N.inv_cdf(1 - 1 / n_trials) + g * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(sr: float, t: int, skew: float, kurt: float, sr0: float) -> float:
    """Probability the true Sharpe ratio beats SR0 (eq. 8). `sr` is per period and `t` the number of periods."""
    if t <= 1:
        return 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return 0.0
    return _N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def streak_limit(p: float, n: int = 50, alpha: float = 0.05) -> float:
    """Eq. 20: the losing streak that a healthy strategy (win rate p) shows by chance less than once in 1/alpha windows
    of n trades. inf when it cannot lose; 1 when it cannot win."""
    if p >= 1:
        return math.inf
    if p <= 0:
        return 1
    x = alpha / (n * p)
    if x >= 1:
        return 1
    return max(1, math.ceil(math.log(x) / math.log(1 - p)))


def kelly(p: float, b: float) -> float:
    """Eq. 15: the Kelly fraction for win rate p and payoff ratio b (average win / average loss)."""
    return p - (1 - p) / b if b > 0 else -1.0


def size_fraction(p: float, b: float, f_max: float, n_trades: int, min_trades: int = 30) -> float:
    """Eq. 16: a quarter of Kelly, capped at f_max. Before min_trades the inputs are too thin: half of f_max. A
    strategy with no edge on its own numbers (f* <= 0) gets 0."""
    if n_trades < min_trades:
        return f_max / 2
    fs = kelly(p, b)
    return 0.0 if fs <= 0 else min(fs / 4, f_max)


def consistency_z(paper: list[float], bt_mean: float, bt_std: float) -> float | None:
    """Eq. 19: how far the paper trades' mean return is from the backtest's, in standard errors."""
    n = len(paper)
    if n < 2 or bt_std <= 0:
        return None
    return (mean(paper) - bt_mean) / (bt_std / math.sqrt(n))


def bootstrap_drift(oos: list[float], live: list[float], paths: int = 10_000, seed: int = 1) -> dict | None:
    """Draw `paths` resamples of the out-of-sample trades with as many trades as the live record has, and compare the
    live cumulative return with their 5th percentile. breach=True: worse than the strategy's own history explains."""
    n = len(live)
    if n == 0 or not oos:
        return None
    rng = random.Random(seed)
    sums = sorted(sum(rng.choices(oos, k=n)) for _ in range(paths))
    p5 = sums[int(0.05 * paths)]
    live_cum = sum(live)
    return dict(live=live_cum, p5=p5, breach=live_cum < p5, n=n)
