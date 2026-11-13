"""Dashboard routes of the Freqtrade subsystem, mounted into arbbot's aiohttp app (arbbot/server.py).

Reads (GET /api/freqtrade/...) are open to the page like the other read endpoints; every write (POST) needs the
per-run control token (X-Token) that only the locally served page carries, and the Host / Origin / LAN-key guard of
the dashboard applies to all of them. Freqtrade's own API, its login and the exchange secrets never reach the browser.
"""
import hmac
import logging
import time

from aiohttp import web

from .client import FreqtradeApiError, FreqtradeError, FreqtradeUnavailable
from .manager import KINDS, TIMEFRAMES, FreqtradeManager

log = logging.getLogger("freqkit.routes")
NO_STORE = {"Cache-Control": "no-store"}


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=NO_STORE)


def _err(msg: str, status: int = 400) -> web.Response:
    return _json({"ok": False, "error": msg}, status)


def _int(q, key: str, default: int, lo: int, hi: int) -> int:
    raw = q.get(key, "")
    if raw == "":
        return default
    if not raw.lstrip("-").isdigit():
        raise ValueError(f"{key} must be a whole number")
    return max(lo, min(hi, int(raw)))


class FreqtradeRoutes:
    def __init__(self, dash, manager: FreqtradeManager):
        self.dash, self.fm = dash, manager

    def register(self, app: web.Application) -> None:
        g, p = app.router.add_get, app.router.add_post
        g("/api/freqtrade/overview", self.overview)
        g("/api/freqtrade/trades", self.trades)
        g("/api/freqtrade/performance", self.performance)
        g("/api/freqtrade/pnl", self.pnl)
        g("/api/freqtrade/balance", self.balance)
        g("/api/freqtrade/logs", self.logs)
        g("/api/freqtrade/events", self.events)
        g("/api/freqtrade/strategy", self.strategy)
        g("/api/freqtrade/exchanges", self.exchanges)
        g("/api/freqtrade/pairs", self.pairs)
        g("/api/freqtrade/whitelist", self.whitelist)
        g("/api/freqtrade/locks", self.locks)
        g("/api/freqtrade/candles", self.candles)
        g("/api/freqtrade/history", self.history)
        g("/api/freqtrade/backtest", self.backtest_get)
        g("/api/freqtrade/backtest/history", self.backtest_history)
        g("/api/freqtrade/backtest/result", self.backtest_result)
        p("/api/freqtrade/engine", self.engine)
        p("/api/freqtrade/webserver", self.webserver)
        p("/api/freqtrade/bot", self.bot)
        p("/api/freqtrade/config", self.config)
        p("/api/freqtrade/exchange", self.exchange)
        p("/api/freqtrade/mode", self.mode)
        p("/api/freqtrade/forceexit", self.forceexit)
        p("/api/freqtrade/backtest", self.backtest_post)
        p("/api/freqtrade/blacklist", self.blacklist)
        p("/api/freqtrade/lock", self.lock)

    # ---- plumbing ---------------------------------------------------------------------------------------------------
    def _check_token(self, request: web.Request) -> None:
        if not hmac.compare_digest(request.headers.get("X-Token", ""), self.dash.token):
            raise web.HTTPForbidden(text="bad token")

    async def _body(self, request: web.Request) -> dict:
        self._check_token(request)
        if request.content_type != "application/json":
            raise ValueError("send JSON")
        try:
            body = await request.json()
        except ValueError:
            raise ValueError("bad JSON")
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body

    async def _guarded(self, fn, *, write: bool = False):
        """Run one handler step and turn its failures into the page's error shape (never a raw traceback)."""
        try:
            return await fn()
        except web.HTTPException:
            raise
        except ValueError as e:
            return _err(str(e), 400)
        except FreqtradeUnavailable as e:
            return _err(f"Freqtrade is not reachable ({str(e)[:120]}). Is the process running?", 503)
        except FreqtradeApiError as e:
            status = e.status if 400 <= e.status < 600 else 502
            return _err(f"Freqtrade refused it: {e.detail}", status if status not in (401, 403) else 502)
        except FreqtradeError as e:
            return _err(f"Freqtrade error: {str(e)[:200]}", 502)
        except Exception as e:                       # never leak a secret through an unexpected exception text
            log.exception("freqtrade route failed: %s", type(e).__name__)
            return _err(f"internal error: {type(e).__name__}", 500)

    def _need_trade(self):
        if self.fm.trade.state != "running" or not self.fm.trade.api_ok:
            raise FreqtradeUnavailable("the trading bot is not running")
        return self.fm.trade.api

    def _need_webserver(self):
        if self.fm.webserver.state != "running" or not self.fm.webserver.api_ok:
            raise FreqtradeUnavailable("the webserver is not running (it starts with the dashboard; relaunch it from the page)")
        return self.fm.webserver.api

    # ---- reads ---------------------------------------------------------------------------------------------------------
    async def overview(self, request: web.Request) -> web.Response:
        return _json(self.fm.overview())

    async def trades(self, request: web.Request) -> web.Response:
        async def go():
            q = request.query
            limit, offset = _int(q, "limit", 50, 1, 500), _int(q, "offset", 0, 0, 10_000_000)
            api = self._need_trade()
            data = await api.trades(limit, offset)
            from .manager import trim_trade
            return _json({"trades": [trim_trade(t) for t in data.get("trades", [])],
                          "trades_count": data.get("trades_count", 0), "total_trades": data.get("total_trades"),
                          "offset": offset, "dry_run": self.fm.trade.dry_run})
        return await self._guarded(go)

    async def performance(self, request: web.Request) -> web.Response:
        async def go():
            return _json({"performance": await self._need_trade().performance()})
        return await self._guarded(go)

    async def pnl(self, request: web.Request) -> web.Response:
        async def go():
            period = request.query.get("period", "day")
            n = _int(request.query, "n", {"day": 30, "week": 12, "month": 12}.get(period, 30), 1, 400)
            api = self._need_trade()
            if period == "day":
                data = await api.daily(n)
            elif period == "week":
                data = await api.weekly(n)
            elif period == "month":
                data = await api.monthly(n)
            else:
                raise ValueError("period must be day, week or month")
            return _json(dict(data, period=period, dry_run=self.fm.trade.dry_run))
        return await self._guarded(go)

    async def balance(self, request: web.Request) -> web.Response:
        async def go():
            return _json(await self._need_trade().balance())
        return await self._guarded(go)

    async def logs(self, request: web.Request) -> web.Response:
        async def go():
            kind = request.query.get("kind", "trade")
            if kind not in KINDS:
                raise ValueError("kind must be trade or webserver")
            limit = _int(request.query, "limit", 200, 10, 1000)
            api_logs = None
            proc = self.fm.procs[kind]
            if proc.state == "running" and proc.api_ok:
                try:
                    api_logs = (await proc.api.logs(limit)).get("logs")
                except FreqtradeError:
                    api_logs = None
            return _json({"kind": kind, "file": self.fm.logs(kind, limit), "api": api_logs, "state": proc.state})
        return await self._guarded(go)

    async def events(self, request: web.Request) -> web.Response:
        return _json({"events": self.fm.store.events(_int(request.query, "limit", 100, 1, 1000))})

    async def strategy(self, request: web.Request) -> web.Response:
        async def go():
            name = request.query.get("name", "")
            from .manager import NAME_RE
            if not NAME_RE.match(name):
                raise ValueError("name must be a strategy class name")
            data = await self._need_webserver().strategy(name)
            return _json({"strategy": data.get("strategy"), "timeframe": data.get("timeframe"),
                          "code": data.get("code", ""), "params": data.get("params", [])})
        return await self._guarded(go)

    async def exchanges(self, request: web.Request) -> web.Response:
        async def go():
            data = await self._need_webserver().exchanges()
            rows = []
            for ex in data.get("exchanges", []):
                if not isinstance(ex, dict):
                    continue
                rows.append({"name": ex.get("classname") or ex.get("name"), "label": ex.get("name"),
                             "supported": ex.get("supported"), "valid": ex.get("valid"), "comment": ex.get("comment"),
                             "trade_modes": ex.get("trade_modes", []), "dex": ex.get("dex")})
            return _json({"exchanges": rows})
        return await self._guarded(go)

    async def pairs(self, request: web.Request) -> web.Response:
        """Markets of the configured exchange (for the pair picker) and the pairs with downloaded candles."""
        async def go():
            q, bot = request.query, self.fm.store.bot_config()
            api = self._need_webserver()
            out = {"stake": bot["stake_currency"], "trading_mode": bot["trading_mode"]}
            if q.get("markets") == "1":
                m = await api.markets(exchange=bot["exchange"], quote=bot["stake_currency"],
                                      trading_mode=bot["trading_mode"], margin_mode=bot["margin_mode"] or None)
                syms = []
                for sym, info in (m.get("markets") or {}).items():
                    if isinstance(info, dict) and info.get("quote") == bot["stake_currency"]:
                        syms.append(sym)
                out["markets"] = sorted(syms)
            try:
                avail = await api.available_pairs(stake_currency=bot["stake_currency"])
                out["downloaded"] = avail.get("pair_interval", [])
            except FreqtradeError as e:
                out["downloaded"], out["downloaded_error"] = [], str(getattr(e, "detail", e))[:200]
            return _json(out)
        return await self._guarded(go)

    async def whitelist(self, request: web.Request) -> web.Response:
        async def go():
            api = self._need_trade()
            return _json({"whitelist": await api.whitelist(), "blacklist": await api.blacklist()})
        return await self._guarded(go)

    async def locks(self, request: web.Request) -> web.Response:
        async def go():
            return _json(await self._need_trade().locks())
        return await self._guarded(go)

    async def candles(self, request: web.Request) -> web.Response:
        async def go():
            q = request.query
            pair, tf = q.get("pair", "").upper(), q.get("timeframe", "")
            from .manager import PAIR_RE
            if not PAIR_RE.match(pair):
                raise ValueError("pair must look like BTC/USDT")
            if tf and tf not in TIMEFRAMES:
                raise ValueError("bad timeframe")
            limit = _int(q, "limit", 300, 10, 1500)
            api = self._need_trade()
            tf = tf or (self.fm.cache.get("config") or {}).get("timeframe") or "5m"
            data = await api.pair_candles(pair, tf, limit)
            cols, rows = data.get("columns", []), data.get("data", [])
            want = ["date", "open", "high", "low", "close", "volume", "enter_long", "exit_long", "enter_short",
                    "exit_short"]
            idx = {c: cols.index(c) for c in want if c in cols}
            return _json({"pair": pair, "timeframe": tf, "columns": list(idx),
                          "data": [[r[i] for i in idx.values()] for r in rows],
                          "last_analyzed": data.get("last_analyzed"), "strategy": data.get("strategy")})
        return await self._guarded(go)

    async def history(self, request: web.Request) -> web.Response:
        hours = _int(request.query, "hours", 24 * 7, 1, 24 * 365)
        mode = request.query.get("mode") or None
        if mode not in (None, "dry_run", "live"):
            return _err("mode must be dry_run or live")
        return _json({"snapshots": self.fm.store.perf_history(time.time() - hours * 3600, mode),
                      "backtests": self.fm.store.backtests(30)})

    async def backtest_get(self, request: web.Request) -> web.Response:
        return _json(self.fm.backtest_view())

    async def backtest_history(self, request: web.Request) -> web.Response:
        async def go():
            return _json({"history": await self.fm.backtest_history(), "saved": self.fm.store.backtests(30)})
        return await self._guarded(go)

    async def backtest_result(self, request: web.Request) -> web.Response:
        async def go():
            return _json(await self.fm.backtest_result(request.query.get("filename", ""),
                                                       request.query.get("strategy", "")))
        return await self._guarded(go)

    # ---- writes (control token) -----------------------------------------------------------------------------------------
    async def engine(self, request: web.Request) -> web.Response:
        async def go():
            action = (await self._body(request)).get("action")
            if action == "start":
                st = await self.fm.start_trade()
            elif action == "stop":
                st = await self.fm.stop_trade()
            elif action == "restart":
                st = await self.fm.restart_trade()
            else:
                raise ValueError("action must be start, stop or restart")
            return _json({"ok": True, "status": st})
        return await self._guarded(go, write=True)

    async def webserver(self, request: web.Request) -> web.Response:
        async def go():
            action = (await self._body(request)).get("action")
            if action == "start":
                st = await self.fm.start_webserver()
            elif action == "stop":
                st = await self.fm.stop_webserver()
            elif action == "restart":
                await self.fm.stop_webserver()
                st = await self.fm.start_webserver()
            else:
                raise ValueError("action must be start, stop or restart")
            return _json({"ok": True, "status": st})
        return await self._guarded(go, write=True)

    async def bot(self, request: web.Request) -> web.Response:
        async def go():
            action = (await self._body(request)).get("action")
            return _json({"ok": True, "result": await self.fm.bot_action(str(action))})
        return await self._guarded(go, write=True)

    async def config(self, request: web.Request) -> web.Response:
        async def go():
            body = await self._body(request)
            return _json({"ok": True, "config": self.fm.save_config(body)})
        return await self._guarded(go, write=True)

    async def exchange(self, request: web.Request) -> web.Response:
        """Store or delete exchange API keys. The answer never echoes them."""
        async def go():
            body = await self._body(request)
            exchange = str(body.get("exchange", ""))
            if body.get("delete") is True:
                return _json({"ok": True, "deleted": self.fm.delete_exchange_keys(exchange),
                              "keys": self.fm.store.exchanges_with_keys()})
            self.fm.save_exchange_keys(exchange, str(body.get("key", "")), str(body.get("secret", "")),
                                       str(body.get("password", "") or ""), str(body.get("uid", "") or ""))
            return _json({"ok": True, "keys": self.fm.store.exchanges_with_keys()})
        return await self._guarded(go, write=True)

    async def mode(self, request: web.Request) -> web.Response:
        async def go():
            body = await self._body(request)
            mode = self.fm.set_mode(str(body.get("mode", "")), str(body.get("confirm", "")))
            return _json({"ok": True, "mode": mode, "status": self.fm.status()})
        return await self._guarded(go, write=True)

    async def forceexit(self, request: web.Request) -> web.Response:
        async def go():
            body = await self._body(request)
            return _json({"ok": True, "result": await self.fm.forceexit(body.get("tradeid", ""),
                                                                        body.get("ordertype"))})
        return await self._guarded(go, write=True)

    async def backtest_post(self, request: web.Request) -> web.Response:
        async def go():
            body = await self._body(request)
            if body.get("action") == "abort":
                return _json({"ok": True, "backtest": await self.fm.abort_backtest()})
            return _json({"ok": True, "backtest": await self.fm.start_backtest(body)})
        return await self._guarded(go, write=True)

    async def blacklist(self, request: web.Request) -> web.Response:
        async def go():
            body = await self._body(request)
            api = self._need_trade()
            pair = str(body.get("pair", "")).upper().strip()
            from .manager import PAIR_RE
            if not PAIR_RE.match(pair) and not pair.endswith("/.*"):
                raise ValueError("pair must look like DOGE/USDT")
            if body.get("action") == "add":
                res = await api.blacklist_add(pair)
            elif body.get("action") == "remove":
                res = await api.blacklist_delete([pair])
            else:
                raise ValueError("action must be add or remove")
            self.fm.event("info", f"blacklist {body.get('action')} {pair}")
            return _json({"ok": True, "result": res})
        return await self._guarded(go, write=True)

    async def lock(self, request: web.Request) -> web.Response:
        async def go():
            body = await self._body(request)
            if body.get("action") != "delete" or not str(body.get("id", "")).isdigit():
                raise ValueError('expected {"action": "delete", "id": <lock id>}')
            res = await self._need_trade().delete_lock(int(body["id"]))
            return _json({"ok": True, "result": res})
        return await self._guarded(go, write=True)


def add_routes(app: web.Application, dash, manager: FreqtradeManager) -> FreqtradeRoutes:
    routes = FreqtradeRoutes(dash, manager)
    routes.register(app)
    return routes
