"""Alerts you set from the terminal: price and funding levels, whale orders, liquidations. Sent to Telegram.

The rules are checked against the whale watch's live streams (arbbot/whales.py): mark price and funding for every coin
once a second, and each whale order and liquidation as it happens. Nothing here trades.

* price    "BTCUSDT mark > 82000"          one-shot: fires once, then stays listed as fired
* funding  "BTCUSDT funding > 0.05%"       one-shot
* whale    whale order of $X+ (one coin or any)   repeating, at most one message per rule every COOLDOWN_S
* liq      liquidation of $X+ (one coin or any)   repeating, same cooldown

Rules survive restarts (a small JSON file next to the bot's state). Every fired alert is also listed for the terminal,
so alerts work without Telegram too.
"""
import json
import logging
import math
import os
import re
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("price-alerts")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
KINDS = ("price", "funding", "whale", "liq")
MAX_RULES = 50
COOLDOWN_S = 60.0
MIN_EVENT_USD = 10_000.0


def _usd(v: float) -> str:
    for n, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= n:
            return f"${v / n:.2f}{s}".replace(".00", "")
    return f"${v:,.0f}"


def _px(v: float) -> str:
    return f"{v:,.2f}" if v >= 100 else f"{v:,.4f}" if v >= 1 else f"{v:.6g}"


def describe(r: dict) -> str:
    coin = r["symbol"] if r["symbol"] != "*" else "any coin"
    if r["kind"] == "price":
        return f"{r['symbol']} mark {r['op']} {_px(r['value'])}"
    if r["kind"] == "funding":
        return f"{r['symbol']} funding {r['op']} {r['value']:g}%"
    return f"{'whale order' if r['kind'] == 'whale' else 'liquidation'} {_usd(r['value'])}+ on {coin}"


class AlertBook:
    def __init__(self, path: Path | None, send=None, clock=time.time):
        self.path, self.clock = path, clock
        self.send = send or (lambda text: None)
        s = self._load()
        self.rules: list[dict] = s.get("rules", [])
        self.next_id = max([r["id"] for r in self.rules], default=0) + 1
        self.fired: deque[dict] = deque(s.get("fired", []), maxlen=50)

    # ---- state ---------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            s = json.loads(Path(self.path).read_text(encoding="utf-8")) if self.path else {}
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rules": self.rules, "fired": list(self.fired)}), encoding="utf-8")
        os.replace(tmp, p)

    # ---- rules ---------------------------------------------------------------------------------
    def add(self, rule: dict) -> dict:
        """Validate and store one rule. Raises ValueError with a reason the terminal can show."""
        if not isinstance(rule, dict):
            raise ValueError("expected an alert rule")
        kind = rule.get("kind")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        sym = str(rule.get("symbol") or "").upper()
        if kind in ("whale", "liq") and sym in ("", "*", "ANY"):
            sym = "*"
        elif not SYMBOL_RE.match(sym):
            raise ValueError("symbol must look like BTCUSDT")
        try:
            value = float(rule.get("value"))
        except (TypeError, ValueError):
            raise ValueError("value must be a number") from None
        if not math.isfinite(value):
            raise ValueError("value must be a number")
        op = rule.get("op", ">")
        if kind in ("price", "funding") and op not in (">", "<"):
            raise ValueError("op must be > or <")
        if kind == "price" and value <= 0:
            raise ValueError("the price must be above 0")
        if kind in ("whale", "liq") and value < MIN_EVENT_USD:
            raise ValueError(f"the size must be at least {_usd(MIN_EVENT_USD)}")
        if len(self.rules) >= MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} alerts: remove some first")
        r = {"id": self.next_id, "kind": kind, "symbol": sym, "op": op if kind in ("price", "funding") else ">=",
             "value": value, "created": int(self.clock() * 1000), "fired": None, "muted_until": 0.0, "skipped": 0}
        r["text"] = describe(r)
        self.next_id += 1
        self.rules.append(r)
        self._save()
        log.info("alert added: %s", r["text"])
        return r

    def remove(self, rule_id) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if str(r["id"]) != str(rule_id)]
        self._save()
        return len(self.rules) < before

    def clear_fired(self) -> int:
        """Drop the one-shot rules that already fired."""
        n = len(self.rules)
        self.rules = [r for r in self.rules if not (r["kind"] in ("price", "funding") and r["fired"])]
        self._save()
        return n - len(self.rules)

    # ---- checking ------------------------------------------------------------------------------
    def _fire(self, r: dict, text: str, symbol: str) -> None:
        now = self.clock()
        if r["kind"] in ("whale", "liq"):
            if now < r["muted_until"]:
                r["skipped"] += 1
                return
            if r["skipped"]:
                text += f" (+{r['skipped']} more in the last {COOLDOWN_S:.0f}s)"
            r["muted_until"], r["skipped"] = now + COOLDOWN_S, 0
        r["fired"] = int(now * 1000)
        self.fired.append({"ts": r["fired"], "id": r["id"], "kind": r["kind"], "symbol": symbol, "text": text})
        self.send(text)
        self._save()

    def check_marks(self, marks: dict[str, tuple[float, float]]) -> None:
        """Price and funding rules against {symbol: (mark price, funding rate as a fraction)}."""
        for r in self.rules:
            if r["kind"] not in ("price", "funding") or r["fired"] or r["symbol"] not in marks:
                continue
            mark, rate = marks[r["symbol"]]
            v = mark if r["kind"] == "price" else rate * 100
            if (v >= r["value"]) if r["op"] == ">" else (v <= r["value"]):
                word = "above" if r["op"] == ">" else "below"
                text = (f"🔔 {r['symbol']} mark {_px(mark)} is {word} {_px(r['value'])}" if r["kind"] == "price" else
                        f"💸 {r['symbol']} funding {rate * 100:.4f}% is {word} {r['value']:g}%")
                self._fire(r, text, r["symbol"])

    def on_event(self, kind: str, ev: dict) -> None:
        """A whale order ("whale") or a liquidation ("liq") from the whale watch."""
        for r in self.rules:
            if r["kind"] != kind or ev["usd"] < r["value"] or r["symbol"] not in ("*", ev["symbol"]):
                continue
            if kind == "whale":
                text = f"🐋 {ev['symbol']} whale {ev['side'].upper()} {_usd(ev['usd'])} @ {_px(ev['price'])}"
            else:
                text = f"💥 {ev['symbol']} {ev['side'].upper()} liquidated {_usd(ev['usd'])} @ {_px(ev['price'])}"
            self._fire(r, text, ev["symbol"])

    def view(self) -> dict:
        return {"rules": self.rules, "fired": list(self.fired)[::-1]}
