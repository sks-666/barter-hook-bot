"""Pre-trade risk gate and circuit breakers.

Halt sources (any one blocks new entries):
  manual  - dashboard / --no-trade; clearable from the dashboard
  kill    - KILL file exists; also flattens; clears when the file is removed
  loss    - UTC-day realised loss limit; also flattens; clears at the next UTC day
  open    - the same limit counting open positions at what closing them now would realise; blocks entries only (no
            flatten: a hedged book's mark-to-exit loss often recovers) and clears as soon as it is back above the limit
  fault   - repeated execution errors or an unhedged leg; needs a restart (fail closed). The one exception: a startup
            reconciliation that only found positions the bot has no record of can be dismissed from the dashboard
"""
import json
import os

from .config import RiskCfg
from .models import Signal
from .portfolio import Portfolio


def unknown_key(unknown: dict[str, float]) -> str:
    """How a dismissed set of unknown positions is saved, so a restart with the same positions does not ask again."""
    return json.dumps(sorted(unknown.items()))


class Risk:
    def __init__(self, cfg: RiskCfg, portfolio: Portfolio):
        self.cfg = cfg
        self.pf = portfolio
        self.manual_halt = False
        self.fault = ""
        self.fault_dismissable = False
        # positions Binance holds that the bot has no record of (startup reconciliation): never entered here
        self.unknown: dict[str, float] = {}
        self.kill_active = False
        self.loss_active = False
        self.open_loss_active = False
        self.consecutive_errors = 0
        self._cooldown: dict[str, float] = {}
        # coins another user of the same exchange account is working (the testnet order ticket): never entered here
        self.external = lambda: set()

    @property
    def halted(self) -> bool:
        return bool(self.manual_halt or self.fault or self.kill_active or self.loss_active or self.open_loss_active)

    @property
    def halt_reason(self) -> str:
        if self.fault:
            return f"FAULT: {self.fault}"
        if self.kill_active:
            return f"kill file '{self.cfg.kill_file}' present"
        if self.loss_active:
            return f"daily loss limit ${self.cfg.daily_loss_limit_usd:.2f} reached"
        if self.open_loss_active:
            return f"daily loss limit ${self.cfg.daily_loss_limit_usd:.2f} reached counting open positions"
        if self.manual_halt:
            return "paused manually"
        return ""

    @property
    def flatten_required(self) -> bool:
        return self.kill_active or self.loss_active

    def refresh(self, now: float, unrealized: float = 0.0) -> None:
        """`unrealized`: what closing every open position now would realise (portfolio.unrealized_usd, summed)."""
        self.kill_active = os.path.exists(self.cfg.kill_file)
        limit = -abs(self.cfg.daily_loss_limit_usd)
        day = self.pf.day_pnl(now)
        self.loss_active = day <= limit
        self.open_loss_active = not self.loss_active and day + min(0.0, unrealized) <= limit

    def size_entry(self, sig: Signal, now: float) -> tuple[float, str]:
        """Return (notional_usd, reason). Notional 0 means blocked, and reason says why."""
        c = self.cfg
        if self.halted:
            return 0.0, self.halt_reason
        if sig.symbol in self.pf.open:
            return 0.0, "already open"
        if sig.symbol in self.external():
            return 0.0, "held by the order ticket"
        if sig.symbol in self.unknown:
            return 0.0, "unknown position on the exchange"
        if len(self.pf.open) >= c.max_open_positions:
            return 0.0, "max open positions"
        if now < self._cooldown.get(sig.symbol, 0.0):
            return 0.0, "symbol cooldown"
        if sig.fillable_usd < c.min_touch_depth_usd:
            return 0.0, f"thin touch (${sig.fillable_usd:.0f} < ${c.min_touch_depth_usd:.0f})"
        room = c.max_total_notional_usd - self.pf.open_notional()
        notional = min(c.trade_notional_usd, sig.fillable_usd * c.depth_fraction, room)
        if notional < c.min_trade_usd:
            return 0.0, "size below minimum (depth or exposure cap)"
        return notional, ""

    def note_exit(self, symbol: str, now: float) -> None:
        self._cooldown[symbol] = now + self.cfg.symbol_cooldown_s

    def record_success(self) -> None:
        self.consecutive_errors = 0

    def record_error(self) -> bool:
        """Count an execution error. Returns True if this tripped the fault breaker."""
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.cfg.max_consecutive_errors and not self.fault:
            self.fault = f"{self.consecutive_errors} consecutive execution errors"
            self.fault_dismissable = False
            return True
        return False

    def trip_fault(self, reason: str) -> None:
        self.fault = reason
        self.fault_dismissable = False

    def trip_reconcile(self, problems: list[str], unknown: dict[str, float], dismissed: str | None = None) -> bool:
        """Apply the startup reconciliation. Returns True if it tripped the fault.

        When the only problems are positions the bot has no record of (`unknown`), the fault can be dismissed, and
        a saved dismissal (`dismissed`, see unknown_key) that covers each of them at the same size skips it. Either
        way the bot stays off those coins.
        """
        self.unknown = dict(unknown)
        if not problems:
            return False
        only_unknown = len(problems) == len(unknown)
        if only_unknown and dismissed:
            saved = dict(json.loads(dismissed))
            if all(saved.get(sym) == amt for sym, amt in unknown.items()):
                return False
        self.trip_fault(("startup reconciliation failed: " + "; ".join(problems))[:400])
        self.fault_dismissable = only_unknown
        return True

    def dismiss_fault(self) -> bool:
        """Clear a dismissable reconciliation fault. Returns False (and changes nothing) for any other fault."""
        if not (self.fault and self.fault_dismissable):
            return False
        self.fault, self.fault_dismissable = "", False
        return True
