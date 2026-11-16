"""Auto-trade the terminal's combined signal (arbbot/signals.py) on the Binance futures TESTNET (test money only).

Every `poll_s` it reads the combined signal over its timeframe and, through the order ticket (arbbot/testnet_ticket.py,
which refuses any non-testnet client), it:

* ENTERS a coin whose signal passed the quality check (verdict TRADE: regime, four pillars, guardrails, structural
  stop, TP1 >= 1.8R, pre-mortem; arbbot/quality.py) with confidence >= min_conf, the whale reading present and enough
  of the sources readable (coverage). Sized so the loss at the stop is `risk_usd` (capped at `max_usd` of position
  value). The entry is a market order when the signal's entry is the price now, otherwise a limit order at the whales'
  price that is cancelled if it has not filled within the ENTRY_TTL share of the timeframe. The stop and the runner
  target (TP2) ride on the exchange (stop-market / take-profit-market, closePosition), plus a reduce-only take-profit
  for TP1_SHARE of the position at TP1.
* After TP1 fills, the stop moves to the entry (breakeven) once the mark is past it.
* EXITS when the exchange stop or TP2 fills, when the signal turns to the other side with confidence >= min_conf,
  or after holding HOLD_X x the timeframe.

The signal is read from mainnet data (whales, crowd, curve, candles) while orders fill at testnet prices, so every level
is moved onto the testnet by its distance from the price: entry = testnet price x (signal entry / mainnet price). A coin
whose testnet price is more than MAX_DRIFT away from mainnet is skipped (its testnet book is not the real market), and so
is one whose testnet book is too thin for the stop's market close (DEPTH_X x the position within DEPTH_SLIP_R of the
stop distance past the stop): on a thin book a stop can fill far past its price.

Limits: at most `max_open` positions and pending entries together, no new entries after `daily_loss_usd` of realised
loss in a UTC day (closed trades' real results from Binance's fills), a cooldown after each exit, the KILL file (the
ticket refuses new orders while it exists), coins held by the arbitrage bot or by any other ticket user are left alone.
Entries can be switched off and on from the terminal (the state survives restarts); positions are still managed while
off. State: data/signal-trader-testnet.json. Nothing here has been backtested: it is an experiment with test money.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .binance_client import BinanceError, NotSentError
from .quality import hold_s

log = logging.getLogger("signal-trader")
MIN_RR = 1.5
TP1_SHARE = 0.5              # the share of the position closed at TP1; the rest runs to TP2 with the stop at breakeven
MIN_COVERAGE = 0.6           # share of the full weight that had something to read
MARKET_WITHIN_PCT = 0.10     # the signal's entry this close to the price: take it with a market order
ENTRY_TTL = 0.5              # a limit entry waits at most this share of the timeframe (between 5 min and 4 h)
COOLDOWN_X = 0.5             # no new entry on a coin for this share of the timeframe after an exit (at least 5 min)
MAX_DRIFT = 0.02             # testnet price this far from mainnet: skip the coin
DEPTH_X = 2.0                # the testnet book must hold this many times the position near the stop (exit depth)...
DEPTH_SLIP_R = 0.5           # ...at prices at most this share of the stop distance past the stop
KEEP_TRADES = 300
# the timeframes it can trade (the terminal's Signals panel buttons), in seconds
WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14_400, "1d": 86_400, "1w": 604_800}
MAX_OPEN_RANGE = (1, 20)     # the most open trades (positions + waiting limit entries) it can be set to from the page
RISK_RANGE = (1.0, 500.0)    # USDT lost at the stop per trade, settable from the page
DAILY_LOSS_RANGE = (5.0, 5000.0)   # USDT of realised loss in a UTC day before new entries stop, settable from the page
LEVERAGE_RANGE = (1, 20)     # the leverage it sets on a coin before entering, settable from the page
# Binance's own closes, by the closing order's client id: auto-deleveraging is the exchange closing a PROFITABLE
# position against a liquidated trader (on the testnet at absurd prices), not a loss the strategy made
FORCED = (("adl_autoclose", "adl", "closed by Binance: auto-deleveraging"),
          ("autoclose-", "liquidation", "liquidated by Binance"))
CHECK_FORCED_PER_PASS = 3   # closed trades re-checked for a forced close per pass (one order lookup each)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class SignalTrader:
    def __init__(self, desk, ticket, window_s: float = 3600.0, risk_usd: float = 20.0, max_open: int = 3,
                 min_conf: int = 60, max_usd: float = 2000.0, daily_loss_usd: float = 100.0, path: Path | None = None,
                 notify=None, clock=time.time, poll_s: float = 30.0, leverage: int = 5):
        if getattr(ticket, "venue", "") != "testnet":
            raise RuntimeError("the signal auto-trader runs on the Binance futures TESTNET only")
        if not (risk_usd > 0 and max_usd > 0 and daily_loss_usd > 0 and max_open >= 1 and 0 <= min_conf <= 100):
            raise ValueError("signal trader: risk, max size and daily loss must be above 0, max open at least 1, "
                             "min confidence 0..100")
        self.desk, self.ticket = desk, ticket
        self.risk_usd, self.max_open, self.min_conf = float(risk_usd), int(max_open), int(min_conf)
        self.max_usd, self.daily_loss_usd = float(max_usd), float(daily_loss_usd)
        if not LEVERAGE_RANGE[0] <= int(leverage) <= LEVERAGE_RANGE[1]:
            raise ValueError(f"signal trader: leverage must be {LEVERAGE_RANGE[0]}x to {LEVERAGE_RANGE[1]}x")
        # the leverage of its coins: Binance picks auto-deleveraging victims by profit x leverage, so low leverage puts
        # its winning trades at the back of that queue (the ticket's own default is 15x)
        self.leverage = int(leverage)
        self.path, self.notify, self.clock, self.poll_s = path, notify or (lambda text: None), clock, poll_s
        self.s = self._load() or {"enabled": True, "open": {}, "pending": {}, "cooldown": {}, "trades": []}
        for k, v in (("open", {}), ("pending", {}), ("cooldown", {}), ("trades", []), ("enabled", True)):
            self.s.setdefault(k, v)
        # a timeframe picked on the dashboard (saved in the state) wins over the command line's
        saved = self.s.get("window_s")
        self.window_s = desk.whales.clamp_window(saved if saved in WINDOWS.values() else window_s)
        saved = self.s.get("max_open")                               # likewise the most open trades
        if isinstance(saved, int) and MAX_OPEN_RANGE[0] <= saved <= MAX_OPEN_RANGE[1]:
            self.max_open = saved
        for key, (lo, hi) in (("risk_usd", RISK_RANGE), ("daily_loss_usd", DAILY_LOSS_RANGE)):   # and the risk limits
            saved = self.s.get(key)
            if isinstance(saved, (int, float)) and not isinstance(saved, bool) and lo <= saved <= hi:
                setattr(self, key, float(saved))
        saved = self.s.get("leverage")                              # and the leverage
        if isinstance(saved, int) and not isinstance(saved, bool) and LEVERAGE_RANGE[0] <= saved <= LEVERAGE_RANGE[1]:
            self.leverage = saved
        self.last_error, self.last_run, self.note = "", 0.0, ""
        self.last_signals: list[dict] = []
        self._halted_day = None
        self._lock = asyncio.Lock()                                   # a pass and a manual take never interleave

    # ---- timing from the timeframe ---------------------------------------------------------------
    @property
    def entry_ttl_s(self) -> float:
        return _clamp(self.window_s * ENTRY_TTL, 300, 4 * 3600)

    @property
    def max_hold_s(self) -> float:
        return hold_s(self.window_s)

    @property
    def cooldown_s(self) -> float:
        return max(self.window_s * COOLDOWN_X, 300)

    # ---- state ------------------------------------------------------------------------------------
    def _load(self) -> dict | None:
        if not self.path or not Path(self.path).exists():
            return None
        try:
            s = json.loads(Path(self.path).read_text(encoding="utf-8"))
            return s if isinstance(s, dict) and "open" in s else None
        except (OSError, ValueError):
            return None

    def _save(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s), encoding="utf-8")
        os.replace(tmp, p)

    def holds(self, sym: str) -> bool:
        """Whether this coin's testnet position or pending entry is the auto-trader's (for the page's 'source')."""
        return sym in self.s["open"] or sym in self.s["pending"]

    def set_enabled(self, on: bool) -> None:
        self.s["enabled"] = bool(on)
        self._save()
        log.info("signal auto-trader: new entries %s", "ON" if on else "OFF")
        self.notify(f"🤖 TESTNET signal auto-trader: new entries {'ON' if on else 'OFF'}"
                    f"{'' if on else ' (open positions are still managed)'}")

    def set_window(self, window_s) -> None:
        """Trade the signal over another timeframe from the next pass on (one of WINDOWS; saved, so it survives a
        restart). Open positions keep the maximum hold of the timeframe they were opened on."""
        if window_s not in WINDOWS.values():
            raise ValueError(f"the timeframe must be one of {', '.join(WINDOWS)}")
        label = next(k for k, v in WINDOWS.items() if v == window_s)
        self.window_s = self.desk.whales.clamp_window(window_s)
        self.s["window_s"] = window_s
        self._save()
        log.info("signal auto-trader: timeframe set to %s", label)
        self.notify(f"🤖 TESTNET signal auto-trader: now trading the {label} signal")

    def set_leverage(self, n) -> None:
        """The leverage it sets on a coin before its NEXT entry (saved). Open trades keep theirs. Lower = less margin
        efficient but later in Binance's auto-deleveraging queue and further from liquidation."""
        lo, hi = LEVERAGE_RANGE
        if not (isinstance(n, int) and not isinstance(n, bool) and lo <= n <= hi):
            raise ValueError(f"the leverage must be a whole number from {lo} to {hi}")
        self.leverage = n
        self.s["leverage"] = n
        self._save()
        log.info("signal auto-trader: leverage set to %dx", n)
        self.notify(f"🤖 TESTNET signal auto-trader: new trades at {n}x leverage (open trades keep theirs)")

    def set_max_open(self, n) -> None:
        """The most positions and waiting limit entries it holds at once (saved, so it survives a restart). Lowering it
        closes nothing: it only stops new entries until enough trades have closed."""
        lo, hi = MAX_OPEN_RANGE
        if not (isinstance(n, int) and not isinstance(n, bool) and lo <= n <= hi):
            raise ValueError(f"the most open trades must be a whole number from {lo} to {hi}")
        self.max_open = n
        self.s["max_open"] = n
        self._save()
        log.info("signal auto-trader: most open trades set to %d", n)
        self.notify(f"🤖 TESTNET signal auto-trader: at most {n} open trade{'s' if n != 1 else ''} now")

    def _set_usd(self, key: str, value, rng: tuple, what: str) -> float:
        lo, hi = rng
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError(f"the {what} must be a number of USDT from {lo:g} to {hi:g}")
        v = round(float(value), 2)
        setattr(self, key, v)
        self.s[key] = v
        self._save()
        log.info("signal auto-trader: %s set to %g USDT", what, v)
        return v

    def set_risk_usd(self, value) -> None:
        """USDT lost if a NEW trade hits its stop (sizing; still capped by max_usd of position). Saved; open trades
        keep the size they were opened with."""
        v = self._set_usd("risk_usd", value, RISK_RANGE, "risk per trade")
        self.notify(f"🤖 TESTNET signal auto-trader: new trades risk ${v:g} at the stop")

    def set_daily_loss_usd(self, value) -> None:
        """Realised loss in a UTC day after which no new entry is made (open trades stay managed). Saved."""
        v = self._set_usd("daily_loss_usd", value, DAILY_LOSS_RANGE, "daily loss limit")
        self._halted_day = None                                       # announce again if the new limit is reached
        self.notify(f"🤖 TESTNET signal auto-trader: daily loss limit ${v:g} (today so far {self.realized_today():+.2f})")

    def _max_hold_of(self, m: dict) -> float:
        w = m.get("window_s") or self.window_s
        return hold_s(w)

    # ---- results ----------------------------------------------------------------------------------
    def realized_today(self) -> float:
        """Today's (UTC) realised result for the daily loss limit. Auto-deleveraging closes are left out: the exchange
        took a profitable position away, the strategy did not lose it (they still count in the totals)."""
        day = int(self.clock() // 86_400 * 86_400 * 1000)
        return sum(t["pnl"] for t in self.s["trades"]
                   if t.get("pnl") is not None and t["closed_ms"] >= day and t.get("forced") != "adl")

    def stats(self) -> dict:
        done = [t for t in self.s["trades"] if t.get("pnl") is not None]
        wins = sum(1 for t in done if t["pnl"] > 0)
        adl = [t for t in done if t.get("forced") == "adl"]
        return dict(closed=len(done), wins=wins, losses=len(done) - wins, realized=sum(t["pnl"] for t in done),
                    today=self.realized_today(), unsettled=sum(1 for t in self.s["trades"] if t.get("pnl") is None),
                    win_rate=wins / len(done) if done else None,
                    adl_count=len(adl), adl_pnl=sum(t["pnl"] for t in adl))

    def stats_by_type(self) -> dict:
        """Closed trades per signal type: count, wins, win rate, realised P&L, average R. Trades from before types
        existed are "untyped"."""
        out: dict[str, dict] = {}
        for t in self.s["trades"]:
            if t.get("pnl") is None:
                continue
            k = t.get("type") or "untyped"
            o = out.setdefault(k, {"name": t.get("type_name") or ("Before types" if k == "untyped" else k),
                                   "closed": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "r_n": 0})
            o["closed"] += 1
            o["wins"] += t["pnl"] > 0
            o["pnl"] += t["pnl"]
            if t.get("r") is not None and t.get("forced") != "adl":
                o["r_sum"] += t["r"]
                o["r_n"] += 1
        for o in out.values():
            o["win_rate"] = o["wins"] / o["closed"] if o["closed"] else None
            o["avg_r"] = o.pop("r_sum") / o["r_n"] if o["r_n"] else None
            o.pop("r_n")
        return out

    async def _forced(self, t: dict, order_ids) -> None:
        """Mark a closed trade that Binance closed itself (auto-deleveraging or liquidation), from its closing orders."""
        lookup = getattr(self.ticket, "client_order_id", None)
        if lookup is None:
            return
        for oid in sorted(set(order_ids)):
            cid = await lookup(t["symbol"], oid)
            hit = next((f for f in FORCED if cid.startswith(f[0])), None)
            if hit:
                t["forced"], t["why"] = hit[1], hit[2]
                break
        t["forced_checked"] = True

    async def _check_forced(self) -> None:
        """Older closed trades (settled before this check existed): look for a forced close, a few per pass."""
        todo = [t for t in self.s["trades"] if t.get("pnl") is not None and not t.get("forced_checked")]
        for t in todo[-CHECK_FORCED_PER_PASS:]:
            try:
                end = t["closed_ms"] + 60_000
                fills = await self.ticket.fills_between(t["symbol"], max(t["opened_ms"] - 60_000, end - 6 * 86_400_000), end)
                out_side = "SELL" if t["side"] == "buy" else "BUY"
                was = t.get("forced")
                await self._forced(t, [f["orderId"] for f in fills
                                       if f.get("side") == out_side and int(f["time"]) >= t["opened_ms"] and "orderId" in f])
                if t.get("forced") and not was:
                    log.warning("signal trader: %s was %s (%+.2f USDT)", t["symbol"], t["why"], t["pnl"])
            except NotSentError:
                raise
            except Exception as e:                                    # rate limit, network: ask again next pass
                log.info("signal trader: could not check how %s closed (%s)", t["symbol"], e)
                return

    async def _settle(self, t: dict) -> None:
        """A closed trade's real result on the testnet: realised P&L of the closing fills minus the fees of both sides."""
        t["tries"] = t.get("tries", 0) + 1
        out_side = "SELL" if t["side"] == "buy" else "BUY"
        try:
            end = t["closed_ms"] + 60_000
            fills = await self.ticket.fills_between(t["symbol"], max(t["opened_ms"] - 60_000, end - 6 * 86_400_000), end)
        except Exception as e:                                       # rate limit, network: ask again next pass
            log.info("signal trader: the result of %s is not readable yet (%s)", t["symbol"], e)
            return
        exits = [f for f in fills if f.get("side") == out_side and int(f["time"]) >= t["opened_ms"]]
        entries = [f for f in fills if f.get("side") != out_side and int(f["time"]) >= t["opened_ms"] - 60_000]
        if not exits:
            return
        fee = lambda fs: sum(float(f.get("commission") or 0) for f in fs if f.get("commissionAsset", "USDT") == "USDT")  # noqa: E731
        qty = sum(float(f["qty"]) for f in exits)
        t["exit"] = sum(float(f["price"]) * float(f["qty"]) for f in exits) / qty if qty else None
        t["fees"] = fee(exits) + fee(entries)
        t["pnl"] = sum(float(f.get("realizedPnl") or 0) for f in exits) - t["fees"]
        if t.get("risk_usd"):
            t["r"] = t["pnl"] / t["risk_usd"]
        try:                                                          # did Binance close it itself?
            await self._forced(t, [f["orderId"] for f in exits if "orderId" in f])
        except NotSentError:
            raise
        except Exception as e:                                        # the result stands; _check_forced asks again
            log.info("signal trader: could not check how %s closed (%s)", t["symbol"], e)

    def _closed(self, sym: str, m: dict, why: str, now_ms: int) -> dict:
        t = dict(symbol=sym, side=m["side"], qty=m.get("qty"), entry=m.get("entry"), stop=m.get("stop"),
                 target=m.get("target"), risk_usd=m.get("risk_usd"), score=m.get("score"), confidence=m.get("confidence"),
                 opened_ms=m["opened_ms"], closed_ms=now_ms, why=why, exit=None, pnl=None, fees=None,
                 leverage=m.get("leverage"), tp1=m.get("tp1"), tp1_hit=m.get("tp1_hit"), quality=m.get("quality"),
                 regime=m.get("regime"), type=m.get("type"), type_name=m.get("type_name"), manual=m.get("manual"))
        self.s["trades"] = (self.s["trades"] + [t])[-KEEP_TRADES:]
        self.s["cooldown"][sym] = now_ms + int(self.cooldown_s * 1000)
        return t

    # ---- one pass ---------------------------------------------------------------------------------
    async def step(self) -> None:
        now_ms = int(self.clock() * 1000)
        for t in [t for t in self.s["trades"] if t.get("pnl") is None and t.get("tries", 0) < 10]:
            await self._settle(t)
            if t.get("pnl") is not None:
                r = f" ({t['r']:+.2f}R)" if t.get("r") is not None else ""
                note = " (not counted in the daily loss limit)" if t.get("forced") == "adl" else ""
                self.notify(f"🤖 TESTNET signal {t['symbol']} result: {t['pnl']:+.2f} USDT{r} — {t['why']}{note}")
        await self._check_forced()
        self.s["cooldown"] = {k: v for k, v in self.s["cooldown"].items() if v > now_ms}
        await self.ticket.refresh()
        view = await self.desk.view(self.window_s)
        rows = {r["symbol"]: r for r in view["rows"]}
        self.last_signals = view["rows"]
        await self._pending(now_ms)
        await self._manage(now_ms, rows)
        await self._enter(now_ms, view["rows"])
        self._save()

    async def _pending(self, now_ms: int) -> None:
        """Limit entries: filled -> a position to manage; expired -> cancelled; gone without a fill -> dropped."""
        ids = {str(o.get("orderId")) for o in self.ticket.open_orders}
        for sym, p in list(self.s["pending"].items()):
            amt = self.ticket.position(sym)
            if amt != 0:
                info = self.ticket.position_info(sym)
                self.s["open"][sym] = dict(p, qty=abs(amt), entry=info.get("entry_px") or p["entry"], opened_ms=now_ms)
                del self.s["pending"][sym]
                if str(p["order_id"]) in ids:                         # a partial fill: the rest is not wanted any more
                    await self._cancel(p["order_id"])
                self.notify(f"🤖 TESTNET signal {p['side'].upper()} {sym} filled at {self.s['open'][sym]['entry']:.6g} "
                            f"(stop {p['stop']:.6g}, target {p['target']:.6g})")
                await self._place_tp1(sym, self.s["open"][sym])
            elif str(p["order_id"]) not in ids:
                del self.s["pending"][sym]
                log.info("signal trader: the limit entry on %s is gone without a fill", sym)
            elif now_ms >= p["expires_ms"]:
                await self._cancel(p["order_id"])
                del self.s["pending"][sym]
                log.info("signal trader: the limit entry on %s did not fill in time; cancelled", sym)

    async def _cancel(self, order_id) -> None:
        try:
            await self.ticket.cancel(order_id)
        except (ValueError, BinanceError) as e:
            log.info("signal trader: cancel %s: %s", order_id, e)

    async def _manage(self, now_ms: int, rows: dict) -> None:
        for sym, m in list(self.s["open"].items()):
            if self.ticket.position(sym) == 0:                        # the exchange stop or target filled
                del self.s["open"][sym]
                self._closed(sym, m, "stop or target", now_ms)
                continue
            await self._breakeven(sym, m)
            row = rows.get(sym)
            flipped = (row and row["bias"] not in ("wait", "long" if m["side"] == "buy" else "short")
                       and row["confidence"] >= self.min_conf)
            why = ("signal turned " + row["bias"].upper()) if flipped else (
                "held the maximum time" if now_ms - m["opened_ms"] >= self._max_hold_of(m) * 1000 else "")
            if not why:
                continue
            try:
                await self.ticket.close_position(sym)
            except (ValueError, BinanceError) as e:
                log.warning("signal trader: could not close %s (%s): %s", sym, why, e)
                continue
            del self.s["open"][sym]
            self._closed(sym, m, why, now_ms)
            self.notify(f"🤖 TESTNET signal {sym} closed: {why}")

    async def _place_tp1(self, sym: str, m: dict) -> None:
        """The reduce-only take-profit for TP1_SHARE of a just-filled position. A refusal only means no partial exit:
        the stop and TP2 already protect the position."""
        place = getattr(self.ticket, "place_take_profit", None)
        if not m.get("tp1") or place is None or m.get("tp1_placed"):
            return
        try:
            await place(sym, m["side"], (m.get("qty") or 0) * TP1_SHARE, m["tp1"], trigger="mark")
            m["tp1_placed"] = True
        except NotSentError:
            raise
        except (ValueError, BinanceError) as e:
            m["tp1_placed"] = False
            log.warning("signal trader: no TP1 on %s (%s); the stop and TP2 still stand", sym, getattr(e, "msg", None) or e)

    async def _breakeven(self, sym: str, m: dict) -> None:
        """TP1 filled (the position shrank): move the stop to the entry, once the mark is past the entry (a stop
        Binance would trigger at once is refused, and the old one is already cancelled by then)."""
        if not m.get("tp1") or m.get("tp1_hit") or not m.get("qty"):
            return
        if abs(self.ticket.position(sym)) > m["qty"] * (1 - TP1_SHARE / 2):
            return
        mark = (self.ticket.position_info(sym) or {}).get("mark")
        long = m["side"] == "buy"
        if not mark or (mark <= m["entry"] if long else mark >= m["entry"]):
            return                                                    # not past the entry now: try again next pass
        replace = getattr(self.ticket, "replace_stop", None)
        if replace is None:
            return
        try:
            await replace(sym, m["side"], m["entry"])
        except NotSentError:
            raise
        except (ValueError, BinanceError) as e:
            log.warning("signal trader: could not move the %s stop to breakeven: %s", sym, getattr(e, "msg", None) or e)
            return
        m.update(tp1_hit=True, stop=m["entry"])
        self.notify(f"🤖 TESTNET signal {sym}: TP1 hit, half closed; stop moved to breakeven {m['entry']:.6g}")

    def can_enter(self) -> str:
        """Why no new entry may be made now ('' = it may)."""
        if not self.s["enabled"]:
            return "entries are off"
        blocked = self.ticket.blocked()
        if blocked:
            return blocked
        if self.realized_today() <= -self.daily_loss_usd:
            return f"the daily loss limit ({self.daily_loss_usd:g} USDT) is reached"
        if len(self.s["open"]) + len(self.s["pending"]) >= self.max_open:
            return f"{self.max_open} positions / entries open"
        return ""

    def candidates(self, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            whale = next((f for f in r.get("factors", []) if f["key"] == "whales"), None)
            if (r["bias"] in ("long", "short") and r.get("verdict") == "trade" and r["confidence"] >= self.min_conf
                    and r.get("entry")
                    and (r.get("rr") or 0) >= MIN_RR and (r.get("coverage") or 0) >= MIN_COVERAGE
                    and whale and whale["weight"] > 0):
                out.append(r)
        return sorted(out, key=lambda r: -r["confidence"])

    async def _enter(self, now_ms: int, rows: list[dict]) -> None:
        why = self.can_enter()
        self.note = why
        if why:
            day = now_ms // 86_400_000
            if "daily loss" in why and self._halted_day != day:
                self._halted_day = day
                self.notify(f"🤖 TESTNET signal auto-trader: {why}; no new entries until the next UTC day")
            return
        taken = set(self.ticket.owned()) | set(self.ticket.arb_symbols())
        for r in self.candidates(rows):
            if self.can_enter():
                break
            sym = r["symbol"]
            if sym in self.s["open"] or sym in self.s["pending"] or sym in self.s["cooldown"] or sym in taken \
                    or self.ticket.position(sym) != 0:
                continue
            try:
                await self._open(sym, r, now_ms)
            except NotSentError:                                      # rate-limit pause: try again next pass
                raise
            except (ValueError, BinanceError) as e:                   # not listed on the testnet, below the minimum...
                self.s["cooldown"][sym] = now_ms + int(self.cooldown_s * 1000)
                log.info("signal trader: %s skipped: %s", sym, getattr(e, "msg", None) or e)

    @staticmethod
    def _check_exit_depth(book: dict | None, long: bool, ref: float, stop: float, qty: float) -> None:
        """Refuse a coin whose testnet book could not take the stop's market close near the stop.

        The stop is a market order on the exchange: on a thin testnet book it fills wherever the next resting order
        is. So the side the stop would close into (asks for a short, bids for a long) must hold DEPTH_X x the position
        at prices no worse than DEPTH_SLIP_R of the stop distance past the stop. This guards the stop's fill only; it
        cannot prevent an exchange auto-deleveraging close (clientOrderId "adl_autoclose")."""
        if not book:
            raise ValueError("the testnet order book could not be read")
        worst = stop + DEPTH_SLIP_R * abs(stop - ref) * (-1 if long else 1)
        levels = book.get("bids" if long else "asks") or []
        size = sum(v for p, v in levels if (p >= worst if long else p <= worst))
        if size < DEPTH_X * qty:
            raise ValueError(f"the testnet book is too thin for its stop: {size:.6g} within reach of the stop, "
                             f"needs {DEPTH_X:g}x the position ({DEPTH_X * qty:.6g})")

    async def take(self, sym: str, window_s: float | None = None) -> dict:
        """Open the coin's signal now because YOU said so (the terminal's Trade button): the same entry, stop, TP1, TP2,
        sizing, leverage and exits as an automatic trade, read fresh for the timeframe given (the auto-trader's when
        None). It may take a NO TRADE verdict or one below the confidence bar (you decided), and works with automatic
        entries off; the safety limits still hold: kill switch / rate-limit pause, daily loss, most open trades, a coin
        already held, a signal with no side or no valid targets. Returns the trade's record; ValueError says why not."""
        sym = str(sym or "").upper()
        w = float(window_s) if window_s else self.window_s
        if w not in WINDOWS.values():
            raise ValueError("pick one of the auto-trader's timeframes (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w)")
        async with self._lock:
            blocked = self.ticket.blocked()
            if blocked:
                raise ValueError(blocked)
            if self.realized_today() <= -self.daily_loss_usd:
                raise ValueError(f"the daily loss limit ({self.daily_loss_usd:g} USDT) is reached")
            if len(self.s["open"]) + len(self.s["pending"]) >= self.max_open:
                raise ValueError(f"{self.max_open} positions / entries are open already (raise 'most open trades')")
            if sym in self.s["open"] or sym in self.s["pending"]:
                raise ValueError(f"the auto-trader already holds {sym}")
            if sym in set(self.ticket.owned()) | set(self.ticket.arb_symbols()) or self.ticket.position(sym) != 0:
                raise ValueError(f"{sym} already has a position on the testnet")
            view = await self.desk.view(w, sym)
            r = next((x for x in view["rows"] if x["symbol"] == sym), None)
            if r is None:
                raise ValueError(f"no signal for {sym} right now (it needs whale orders in that timeframe)")
            if r["bias"] not in ("long", "short"):
                raise ValueError(f"{sym} has no clear side right now (WAIT)")
            if not (r.get("entry") and r.get("stop") and r.get("target")):
                raise ValueError(f"{sym} has no valid stop and targets right now: " + "; ".join(r.get("gates") or []))
            now_ms = int(self.clock() * 1000)
            await self._open(sym, r, now_ms, window_s=w, manual=True)
            self.s["cooldown"].pop(sym, None)
            self._save()
            rec = self.s["open"].get(sym) or self.s["pending"].get(sym) or {}
            return dict(rec, symbol=sym, status="open" if sym in self.s["open"] else "waiting")

    async def _open(self, sym: str, r: dict, now_ms: int, window_s: float | None = None, manual: bool = False) -> None:
        q = await self.ticket._quote(sym, book=True)
        here = (q["bid"] + q["ask"]) / 2
        drift = here / r["mark"] - 1
        if abs(drift) > MAX_DRIFT:
            raise ValueError(f"the testnet price is {drift * 100:+.1f}% from mainnet")
        k = here / r["mark"]                                          # mainnet levels moved onto the testnet price
        long = r["bias"] == "long"
        side = "buy" if long else "sell"
        entry, stop, target = r["entry"] * k, r["stop"] * k, r["target"] * k
        tp1 = r["tp1"] * k if r.get("tp1") else None
        market = abs(r["entry"] / r["mark"] - 1) * 100 < MARKET_WITHIN_PCT
        ref = (q["ask"] if long else q["bid"]) if market else entry
        if long and not stop < ref < target or not long and not target < ref < stop:
            raise ValueError("the price has already moved past the stop or the target")
        mult = float(((r.get("quality") or {}).get("type") or {}).get("risk_mult") or 1.0)   # the signal type's risk x
        qty = min(self.risk_usd * mult / abs(ref - stop), self.max_usd / ref)
        self._check_exit_depth(q.get("book"), long, ref, stop, qty)
        if getattr(self.ticket, "leverage", {}).get(sym) != self.leverage:   # low leverage: last in the ADL queue
            await self.ticket.set_leverage(sym, self.leverage)
        # stop and target trigger on the MARK price: a stray trade on a thin testnet book can spike the last price (and
        # fill the stop there), not the mark
        order = dict(symbol=sym, side=side, qty=qty, tp=target, sl=stop, trigger="mark")
        if market:
            order.update(type="market", slippage_pct=0.3)               # refuse a market order that would slip > 0.3%
        else:
            order.update(type="limit", price=entry, tif="GTC")
        o = await self.ticket.place_order(order)
        rec = dict(side=side, entry=o.get("filled_px") or ref, stop=stop, target=target, score=r["score"],
                   confidence=r["confidence"], risk_usd=qty * abs(ref - stop), order_id=o.get("id"),
                   window_s=window_s or self.window_s, manual=manual or None, leverage=getattr(self.ticket, "leverage", {}).get(sym) or self.leverage,
                   tp1=tp1, quality=(r.get("quality") or {}).get("quality"),
                   type=r.get("signal_type"), type_name=r.get("signal_type_name"),
                   regime=((r.get("quality") or {}).get("regime") or {}).get("name"))
        if o.get("status") == "filled":
            self.s["open"][sym] = dict(rec, qty=o.get("qty"), opened_ms=now_ms)
            self.notify(f"{'🖐 TESTNET YOUR' if manual else '🤖 TESTNET'} signal {side.upper()} {sym} {o.get('qty'):.6g} at market {rec['entry']:.6g} "
                        f"({rec.get('type_name') or 'signal'}, score {r['score'] * 100:+.0f}, conf {r['confidence']}, "
                        f"quality {rec['quality']}) stop {stop:.6g}"
                        f"{f' TP1 {tp1:.6g}' if tp1 else ''} TP2 {target:.6g}")
            await self._place_tp1(sym, self.s["open"][sym])
        elif o.get("status") == "new":
            self.s["pending"][sym] = dict(rec, qty=qty, placed_ms=now_ms,
                                          expires_ms=now_ms + int(self.entry_ttl_s * 1000))
            self.notify(f"{'🖐 TESTNET YOUR' if manual else '🤖 TESTNET'} signal {side.upper()} {sym}: limit {entry:.6g} for {self.entry_ttl_s / 60:.0f} min "
                        f"(score {r['score'] * 100:+.0f}, conf {r['confidence']}) stop {stop:.6g} target {target:.6g}")
        else:
            raise ValueError(f"the entry came back {o.get('status')}")
        log.info("signal trader: %s %s %s qty %g entry %g stop %g target %g", "market" if market else "limit",
                 side, sym, qty, rec["entry"], stop, target)

    async def step_safely(self) -> None:
        try:
            async with self._lock:
                await self.step()
            self.last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as e:                                        # never stop the bot; try again next pass
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("signal trader step failed: %s", self.last_error)
        self.last_run = self.clock()

    async def run(self) -> None:
        await asyncio.sleep(60)                                       # let the whale watch collect orders first
        while True:
            await self.step_safely()
            await asyncio.sleep(self.poll_s)

    def view(self) -> dict:
        info = lambda sym: self.ticket.position_info(sym)            # noqa: E731
        return dict(venue="testnet", enabled=self.s["enabled"], window_s=self.window_s, windows=WINDOWS,
                    max_open_range=list(MAX_OPEN_RANGE), risk_range=list(RISK_RANGE), leverage_range=list(LEVERAGE_RANGE),
                    daily_loss_range=list(DAILY_LOSS_RANGE),
                    risk_usd=self.risk_usd,
                    max_open=self.max_open, min_conf=self.min_conf, max_usd=self.max_usd, leverage=self.leverage,
                    daily_loss_usd=self.daily_loss_usd, entry_ttl_s=self.entry_ttl_s, max_hold_s=self.max_hold_s,
                    blocked=self.can_enter(), last_run=int(self.last_run * 1000), error=self.last_error,
                    open={sym: dict(m, **info(sym)) for sym, m in self.s["open"].items()},
                    pending=self.s["pending"], trades=self.s["trades"][-30:][::-1], stats=self.stats(),
                    stats_by_type=self.stats_by_type())
