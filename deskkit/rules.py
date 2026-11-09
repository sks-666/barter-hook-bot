"""The desk rules: every threshold and window of the protocol in one place. Defaults are the paper's; desk_rules.toml
overrides them and its `version` is written beside every result, so a change of rules never silently mixes two regimes."""
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class DeskRules:
    version: str = "paper-v1"
    capital: float = 10_000.0            # the planned account: sizing, capacity
    risk_pct: float = 1.0                # f_max: % of equity risked per trade
    # walk-forward windows (months of 30.4375 days): fit on `train`, judge on the next `test`, roll by `step`
    train_months: int = 12
    test_months: int = 3
    step_months: int = 3
    warmup_bars: int = 250               # candles before the first window, only to warm indicators up
    lookback_bars: int = 700             # candles of history each window run sees before its start
    # Table III
    min_oos_trades: int = 30
    dsr_min: float = 0.95
    wf_positive_min: float = 0.70
    crash_fall: float = 0.30             # a BTC fall of this much inside a TEST window makes it a crash window
    crash_mult: float = 1.5              # drawdown in a crash window must stay within crash_mult x D_plan
    d_plan_pct: float = 25.0             # planned maximum drawdown (%), unless d_plan names the strategy
    d_plan: dict = field(default_factory=dict)
    plateau_band: float = 0.20
    capacity_pct: float = 1.0            # planned size as % of the median HOURLY traded value
    min_trades_8w: int = 20
    # ladder
    rung_min_trades: int = 20
    rung1_min_days: int = 14
    rung5_clean_days: int = 28
    retire_days: int = 56
    drift_min_trades: int = 10           # the bootstrap drift test needs at least this many live trades

    def plan_dd(self, strategy: str) -> float:
        return float(self.d_plan.get(strategy, self.d_plan_pct))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "DeskRules":
        """Defaults, overridden by the [desk] table (and [d_plan]) of the file when it exists. Unknown keys are an error:
        a misspelt threshold must never fall back to a default silently."""
        rules = cls()
        if path is None or not Path(path).exists():
            return rules
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        table = dict(data.get("desk", {}))
        bad = set(table) - known
        if bad:
            raise ValueError(f"{path}: unknown desk rule(s) {', '.join(sorted(bad))}")
        for k, v in table.items():
            setattr(rules, k, type(getattr(rules, k))(v) if k != "d_plan" else dict(v))
        rules.d_plan = {**rules.d_plan, **{k: float(v) for k, v in data.get("d_plan", {}).items()}}
        return rules
