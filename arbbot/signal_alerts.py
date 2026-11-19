"""Telegram alerts for the terminal's Signals panel: a message when a coin's signal turns TRADE on a timeframe you
watch (the panel's 🔔 Telegram button, per timeframe, saved in data/signal-alerts-<state>.json).

Every POLL_S it reads the combined signal (arbbot/signals.py, the same rows the panel shows) for each watched timeframe
and sends one message per coin that is newly TRADE with a LONG or SHORT side: its type, quality, confidence, the price,
the entry, stop, TP1 and TP2 with their distance and R. A coin is announced again only after it has left TRADE (or
flipped side) and at least one timeframe has passed since its last message, so a verdict that flickers is not spam.
When a timeframe is switched on, and when the bot starts, the coins already TRADE are noted without a message (the
switch-on reply lists them). Read-only: it never places an order. A heuristic, not advice.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("signal-alerts")
POLL_S = 60.0
WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14_400, "1d": 86_400, "1w": 604_800}
KEEP_RECENT = 30


def label(window_s: float) -> str:
    return next((k for k, v in WINDOWS.items() if v == window_s), f"{window_s:g}s")


def _px(v) -> str:
    return "—" if v is None else f"{v:,.6g}" if abs(v) < 1000 else f"{v:,.2f}"


def message(r: dict, window_s: float, venue: str = "") -> str:
    """One TRADE signal as a Telegram message."""
    long = r["bias"] == "long"
    q = r.get("quality") or {}
    e, stop, mark = r.get("entry"), r.get("stop"), r.get("mark")
    pct = lambda x: f"{(x - e) / e * 100:+.2f}%" if x and e else ""        # noqa: E731
    to = r.get("to_entry_pct")
    entry = (f"{_px(e)} (at the price)" if to is not None and abs(to) < 0.1 else
             f"{_px(e)} (limit, {to:+.2f}% from now)" if to is not None else _px(e))
    lines = [
        f"🔔 TRADE · {label(window_s)} · {'▲ LONG' if long else '▼ SHORT'} {r['symbol']}{(' · ' + venue) if venue else ''}",
        f"{r.get('signal_type_name') or (q.get('type') or {}).get('name') or 'signal'} · quality {q.get('quality', '—')} · "
        f"conf {r.get('confidence')} · score {r.get('score', 0) * 100:+.0f}"
        + (f" · {(q.get('regime') or {}).get('name', '').replace('_', ' ').lower()}" if q.get("regime") else ""),
        f"Price {_px(mark)} · entry {entry}",
        f"Stop {_px(stop)} {pct(stop)}"
        + (f" · TP1 {_px(r.get('tp1'))} {pct(r.get('tp1'))} ({r['rr1']:.1f}R, half closes)" if r.get("tp1") and r.get("rr1") else "")
        + (f" · TP2 {_px(r.get('target'))} {pct(r.get('target'))} ({r['rr']:.1f}R)" if r.get("target") and r.get("rr") else ""),
    ]
    pm = (q.get("premortem") or {}).get("text")
    if pm:
        lines.append(f"Pre-mortem: {pm}")
    lines.append("Not advice. Check it in the terminal; ▶ Trade it opens the ticket.")
    return "\n".join(lines)


class SignalAlerts:
    def __init__(self, desk, send, path: Path | None = None, clock=time.time, poll_s: float = POLL_S, venue: str = ""):
        self.desk, self.send, self.path, self.clock, self.poll_s, self.venue = desk, send, path, clock, poll_s, venue
        self.s = self._load() or {"windows": []}
        self.s["windows"] = [w for w in self.s.get("windows", []) if w in WINDOWS.values()]
        self.active: dict[str, float] = {}         # "window:symbol:side" -> when it was last TRADE (this run)
        self.last_sent: dict[str, float] = {}      # "window:symbol:side" -> when its last message went out
        self.primed: set[float] = set()            # timeframes whose current TRADE rows have been noted
        self.recent: list[dict] = []
        self.last_run, self.last_error = 0.0, ""

    def _load(self) -> dict | None:
        try:
            s = json.loads(Path(self.path).read_text(encoding="utf-8")) if self.path else None
            return s if isinstance(s, dict) else None
        except (OSError, ValueError):
            return None

    def _save(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"windows": self.s["windows"]}), encoding="utf-8")
        os.replace(tmp, p)

    @property
    def windows(self) -> list[float]:
        return list(self.s["windows"])

    async def set_window(self, window_s, on: bool) -> list[dict]:
        """Watch (on) or stop watching a timeframe. Returns the coins TRADE on it right now (noted, not sent)."""
        if window_s not in WINDOWS.values():
            raise ValueError(f"the timeframe must be one of {', '.join(WINDOWS)}")
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        ws = set(self.s["windows"])
        if on:
            ws.add(window_s)
        else:
            ws.discard(window_s)
            self.primed.discard(window_s)
        self.s["windows"] = sorted(ws)
        self._save()
        now_trade = []
        if on:
            now_trade = await self._pass(window_s, quiet=True)
            names = ", ".join(f"{'▲' if r['bias'] == 'long' else '▼'}{r['symbol'].removesuffix('USDT')}" for r in now_trade)
            self.send(f"🔔 Signal alerts ON for the {label(window_s)} signal: a message when a coin turns TRADE."
                      + (f" TRADE now: {names}." if names else " No coin is TRADE right now."))
        else:
            self.send(f"🔕 Signal alerts OFF for the {label(window_s)} signal.")
        log.info("signal alerts: %s %s", label(window_s), "ON" if on else "OFF")
        return now_trade

    async def _pass(self, w: float, quiet: bool = False) -> list[dict]:
        """Read one timeframe; send a message for each newly TRADE coin (quiet: only note them)."""
        view = await self.desk.view(w)
        now = self.clock()
        trade = [r for r in view["rows"] if r.get("verdict") == "trade" and r.get("bias") in ("long", "short")
                 and r.get("entry") and r.get("stop") and r.get("target")]
        keys = {f"{w}:{r['symbol']}:{r['bias']}": r for r in trade}
        for k in [k for k in self.active if k.startswith(f"{w}:") and k not in keys]:
            del self.active[k]                                       # left TRADE: may be announced again later
        for k, r in keys.items():
            new = k not in self.active
            self.active[k] = now
            if not new or quiet or w not in self.primed:
                continue
            if now - self.last_sent.get(k, -1e18) < w:               # flickered back within one timeframe: no repeat
                continue
            self.last_sent[k] = now
            self.send(message(r, w, self.venue))
            self.recent = ([dict(symbol=r["symbol"], side=r["bias"], window_s=w, at=int(now * 1000),
                                 entry=r.get("entry"), stop=r.get("stop"), target=r.get("target"))] + self.recent)[:KEEP_RECENT]
            log.info("signal alert: %s %s %s", label(w), r["bias"], r["symbol"])
        self.primed.add(w)
        return trade

    async def step(self) -> None:
        for w in self.windows:
            await self._pass(w)

    async def run(self) -> None:
        await asyncio.sleep(90)                                      # let the whale watch collect orders first
        while True:
            try:
                await self.step()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:                                   # never stop the bot; try again next pass
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                log.warning("signal alerts: %s", self.last_error)
            self.last_run = self.clock()
            await asyncio.sleep(self.poll_s)

    def view(self) -> dict:
        return dict(windows=self.windows, windows_all=WINDOWS, poll_s=self.poll_s, last_run=int(self.last_run * 1000),
                    error=self.last_error, recent=self.recent)
