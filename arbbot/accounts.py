"""The mainnet accounts the dashboard trades: your Binance sub-account (BINANCE_API_KEY) and, when its key is set, the
main account (BINANCE_MAIN_API_KEY / BINANCE_MAIN_API_SECRET).

Each account has its own Binance key, order ticket (arbbot/testnet_ticket.py, real-money caps and the KILL file) and
Trade-button trader (arbbot/signal_trader.py, button only), and they all run at the same time: a trade's stop, TP1,
TP2 and exits keep working on its own account whichever one the page shows. The page's order ticket, balances,
positions and ▶ Trade it button use the ACTIVE account, switched from the terminal (saved in data/active-account-*.json).
An order names the account it was meant for, so a switch made in another tab can never send it to the other account.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("accounts")


class Accounts:
    def __init__(self, path: Path | None = None, notify=None):
        self.path, self.notify = path, notify or (lambda text: None)
        self.items: dict[str, dict] = {}          # name -> {label, ticket, trader}
        self.active: str | None = None
        self._saved = self._load()

    def _load(self) -> str | None:
        try:
            return json.loads(Path(self.path).read_text(encoding="utf-8")).get("active") if self.path else None
        except (OSError, ValueError, AttributeError):
            return None

    def _save(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"active": self.active}), encoding="utf-8")
        os.replace(tmp, p)

    def add(self, name: str, label: str, ticket, trader=None) -> None:
        self.items[name] = dict(label=label, ticket=ticket, trader=trader)
        if self.active is None or name == self._saved:
            self.active = name

    @property
    def ticket(self):
        return self.items[self.active]["ticket"] if self.active else None

    @property
    def trader(self):
        return self.items[self.active]["trader"] if self.active else None

    @property
    def label(self) -> str:
        return self.items[self.active]["label"] if self.active else ""

    def check(self, name) -> None:
        """An order meant for `name` (None: not said) may go only to the active account."""
        if name is not None and name != self.active:
            raise ValueError(f"the active account is now {self.label}, not {str(name).upper()}: nothing was sent. "
                             f"Check the account and try again")

    def switch(self, name: str) -> None:
        if name not in self.items:
            raise ValueError(f"no account {name!r}: {', '.join(self.items)}")
        if name == self.active:
            return
        was = self.label
        self.active = name
        self._save()
        log.warning("dashboard account switched: %s -> %s", was, self.label)
        self.notify(f"🔁 Dashboard account: {self.label} (was {was}). The ticket and the Trade button now trade the "
                    f"{self.label} account; open trades on {was} stay managed.")

    def view(self) -> dict:
        rows = []
        for name, it in self.items.items():
            t = it["ticket"]
            acct = getattr(t, "account", None) or {}
            row = dict(name=name, label=it["label"], ok=bool(acct) and not t.last_error, error=t.last_error,
                       positions=sum(1 for p in getattr(t, "risk", []) if float(p.get("positionAmt") or 0)),
                       trade_button=it["trader"] is not None)
            if acct:
                row.update(balance=t._usdt("walletBalance"), available=t._usdt("availableBalance"))
            rows.append(row)
        return dict(active=self.active, label=self.label, accounts=rows)
