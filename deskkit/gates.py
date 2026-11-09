"""Table III: the gates a strategy must pass to be called profitable. Every gate reports its value and threshold, so a
failure says by how much."""
from dataclasses import asdict, dataclass

from .rules import DeskRules


@dataclass
class Gate:
    name: str
    ok: bool
    value: str
    threshold: str
    note: str = ""


@dataclass
class GateInput:
    strategy: str
    oos_trades: int
    oos_days: float
    oos_net_pct: float
    dsr: float
    n_trials: int
    positive_windows: int
    windows: int
    crash_windows: list[tuple[int, float]]       # (window index, drawdown %) for windows that contain a BTC crash
    latest_year_pct: float | None
    neighbours: list[tuple[str, float]]          # (setting, its out-of-sample net %)
    noise: dict[str, float]                      # {"1-bar delay": net %, "2x slippage": net %}
    capacity_ratio_pct: float | None             # largest planned size / median hourly traded value, in %
    capacity_symbol: str = ""


def evaluate(g: GateInput, rules: DeskRules) -> list[Gate]:
    out: list[Gate] = []
    out.append(Gate("Trade count", g.oos_trades >= rules.min_oos_trades, f"{g.oos_trades}", f">= {rules.min_oos_trades} out-of-sample"))
    out.append(Gate("Out-of-sample", g.oos_net_pct > 0, f"{g.oos_net_pct:+.1f}%", "> 0 after full costs"))
    out.append(Gate("Deflated Sharpe", g.dsr >= rules.dsr_min, f"{g.dsr:.3f}", f">= {rules.dsr_min:g}", f"counting {g.n_trials} trials"))
    share = g.positive_windows / g.windows if g.windows else 0.0
    out.append(Gate("Walk-forward", g.windows > 0 and share >= rules.wf_positive_min, f"{g.positive_windows}/{g.windows} windows positive",
                    f">= {rules.wf_positive_min * 100:.0f}%"))
    limit = rules.crash_mult * rules.plan_dd(g.strategy)
    if g.crash_windows:
        worst = max(dd for _, dd in g.crash_windows)
        out.append(Gate("Crash window", worst <= limit, f"{worst:.1f}% drawdown", f"<= {limit:.1f}% ({rules.crash_mult:g} x planned {rules.plan_dd(g.strategy):g}%)"))
    else:
        out.append(Gate("Crash window", False, "no crash window", f"a test window with a BTC fall of {rules.crash_fall * 100:.0f}%+",
                        "no out-of-sample window contains a crash, so none can pass"))
    out.append(Gate("Latest year", g.latest_year_pct is not None and g.latest_year_pct > 0,
                    "n/a" if g.latest_year_pct is None else f"{g.latest_year_pct:+.1f}%", "> 0"))
    if g.neighbours:
        bad = [f"{k} ({v:+.1f}%)" for k, v in g.neighbours if v <= 0]
        out.append(Gate("Parameter plateau", not bad, f"{len(g.neighbours) - len(bad)}/{len(g.neighbours)} neighbours profitable",
                        f"all within +-{rules.plateau_band * 100:.0f}%", "failed: " + ", ".join(bad[:4]) if bad else ""))
    else:
        out.append(Gate("Parameter plateau", False, "no tunable settings", "neighbours profitable"))
    if g.noise:
        bad = [k for k, v in g.noise.items() if v <= 0]
        out.append(Gate("Noise test", not bad, ", ".join(f"{k} {v:+.1f}%" for k, v in g.noise.items()), "all > 0", ("failed: " + ", ".join(bad)) if bad else ""))
    else:
        out.append(Gate("Noise test", False, "not run", "1-bar delay and 2x slippage > 0"))
    if g.capacity_ratio_pct is None:
        out.append(Gate("Capacity", False, "no trades", f"<= {rules.capacity_pct:g}% of hourly volume"))
    else:
        out.append(Gate("Capacity", g.capacity_ratio_pct <= rules.capacity_pct, f"{g.capacity_ratio_pct:.3f}% ({g.capacity_symbol})",
                        f"<= {rules.capacity_pct:g}% of median hourly volume"))
    per_8w = g.oos_trades / g.oos_days * 56 if g.oos_days > 0 else 0.0
    out.append(Gate("Frequency", per_8w >= rules.min_trades_8w, f"{per_8w:.1f} trades per 8 weeks", f">= {rules.min_trades_8w}"))
    return out


def gates_to_dicts(gates: list[Gate]) -> list[dict]:
    return [asdict(x) for x in gates]
