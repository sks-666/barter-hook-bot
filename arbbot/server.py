"""Local dashboard server: static page + WebSocket snapshot stream + three guarded control endpoints.

Bound to localhost by default. Guards against DNS-rebinding and cross-site requests: the Host header
must match the bound address, and control endpoints need the per-run token embedded in the page.
"""
import asyncio
import csv
import hmac
import io
import ipaddress
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import WSMsgType, web

from .binance_client import BinanceError, NotSentError
from .config import Config
from .engine import Engine
from .pnl_report import PERIODS, report as pnl_report

log = logging.getLogger("server")
ACTIONS = ("halt", "resume", "flatten", "dismiss")
LOOPBACK = ("127.0.0.1", "::1")
LAN_COOKIE = "arb_lan_key"   # + "_<port>": a browser keeps ONE cookie per name for a host whatever the port, so a shared
                             # name let one dashboard's key overwrite another's (open the paper link, mainnet locks)


def lan_host_ok(host: str) -> bool:
    """A Host header a phone on the home network sends: a private IP address or this Mac's .local name. (DNS-rebinding
    attacks need a domain name the attacker controls, so private IP literals and .local cannot carry one.)"""
    name = host.rsplit(":", 1)[0].strip("[]")
    if name.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return ip.is_private or ip in TAILNET     # Python does not count Tailscale's 100.64.0.0/10 as private


TAILNET = ipaddress.ip_network("100.64.0.0/10")   # Tailscale addresses: reachable only from your own devices


def lan_key(path: Path) -> str:
    """The key other devices need (--lan). Kept in data/ so a bookmarked phone link survives restarts."""
    path = Path(path)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if len(key) < 16:
        key = secrets.token_urlsafe(16)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
        os.chmod(path, 0o600)
    return key


# interfaces that are not the home network: loopback, VPN tunnels (macOS utun, Linux tun/wg/NordLynx), containers and
# virtual bridges (Docker, libvirt), Apple's peer-to-peer links
NOT_LAN = ("lo", "utun", "tun", "tap", "wg", "nordlynx", "docker", "br-", "veth", "virbr", "bridge", "awdl", "llw")


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def lan_addresses() -> list[str]:
    """This computer's addresses on the home network: private IPv4 on real interfaces. Linux: `ip -4 -o addr`
    (most distributions no longer ship ifconfig); macOS and older Linux: `ifconfig`."""
    found = []
    for line in _run(["ip", "-4", "-o", "addr", "show"]).splitlines():   # "3: wlan0    inet 192.168.1.5/24 brd ..."
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet" and not parts[1].startswith(NOT_LAN):
            found.append(parts[3].split("/")[0])
    if not found:
        iface = ""
        for line in _run(["ifconfig"]).splitlines():
            if line and not line[0].isspace():
                iface = line.split(":", 1)[0]
            elif line.strip().startswith("inet ") and not iface.startswith(NOT_LAN):
                found.append(line.split()[1].removeprefix("addr:"))       # old Linux net-tools: "inet addr:1.2.3.4"
    return [ip for ip in found if _private_ipv4(ip)]


def _private_ipv4(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and (a.is_private or a in TAILNET) and not a.is_loopback     # a Tailscale address is a phone link too


LOCKED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Locked</title><body style="font:16px system-ui;padding:24px;max-width:520px;margin:auto">
<h2>This dashboard is locked</h2><p>Open it with the phone link the bot printed when it started (it ends in
<code>?key=…</code>). Run <code>Start Binance Bots.command</code> on the Mac to see it again.</p></body>"""


class Dashboard:
    def __init__(self, engine: Engine, cfg: Config, web_dir: Path, market=None, paper=None, lan_key: str | None = None,
                 whales=None, alert_book=None, desk=None, macro=None, freq=None, signal_desk=None, signal_trader=None,
                 book_market=None):
        self.engine = engine
        self.freq = freq                          # a freqkit.manager.FreqtradeManager (the /trading page), or None
        self.macro = macro                        # an arbbot.macro.MacroNews (the terminal's MACRO / NEWS panel), or None
        self.desk = desk                          # a callable returning deskkit.report.desk_view(...), or None
        self.alert_book = alert_book              # an arbbot.price_alerts.AlertBook (terminal alerts), or None
        self.whales = whales                      # an arbbot.whales.WhaleWatch (big trades, liquidations), or None
        self.signal_desk = signal_desk            # an arbbot.signals.SignalDesk (made on the first request if None)
        self.signal_trader = signal_trader        # an arbbot.signal_trader.SignalTrader (testnet auto-trading), or None
        self.market = market                      # an arbbot.market.MarketData for the chart / order book panel, or None
        self.book_market = book_market            # the MarketData the BOOK overlay reads (mainnet public), else `market`
        self.book = None                          # an arbbot.orderbook.BookWatch, made on the first BOOK request
        self.heat = None                          # an arbbot.heatmap.Heatmap, made on the first heatmap request
        self.live = None                          # an arbbot.livefeed.LiveFeed, made when a terminal first connects
        self.live_book = None                     # ...and one on the heatmap's exchange (mainnet), for /ws/live?src=book
        self.paper = paper                        # an arbbot.paper_futures.PaperFutures for the order ticket, or None
        self.cfg = cfg
        self.web_dir = web_dir
        self.token = secrets.token_urlsafe(24)
        self.clients: set[web.WebSocketResponse] = set()
        port = cfg.server.port
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
            self.allowed_hosts.add(f"{cfg.server.host}:{port}")
        # --lan: other devices on the home network may open the page, but only with this key (the page carries the
        # control token, and on mainnet the ticket trades real money). This Mac itself never needs it.
        self.lan_key = lan_key

    def is_local(self, request: web.Request) -> bool:
        return request.remote in LOOPBACK

    def make_app(self) -> web.Application:
        @web.middleware
        async def guard(request: web.Request, handler):
            lan_host = (bool(self.lan_key) and request.host.endswith(f":{self.cfg.server.port}")
                        and lan_host_ok(request.host))
            if request.host not in self.allowed_hosts and not lan_host:
                raise web.HTTPForbidden(text="bad host")
            origin = request.headers.get("Origin")
            if origin and origin != f"http://{request.host}":
                raise web.HTTPForbidden(text="bad origin")
            if self.lan_key and not self.is_local(request):
                cookie = f"{LAN_COOKIE}_{self.cfg.server.port}"
                if hmac.compare_digest(request.query.get("key", ""), self.lan_key):
                    resp = web.HTTPFound("/")          # remember the key, then drop it from the address bar
                    resp.set_cookie(cookie, self.lan_key, max_age=180 * 86_400, httponly=True, samesite="Strict")
                    raise resp
                # the old shared cookie still opens the page it holds the key of (phones set up before the change)
                if not (hmac.compare_digest(request.cookies.get(cookie, ""), self.lan_key)
                        or hmac.compare_digest(request.cookies.get(LAN_COOKIE, ""), self.lan_key)):
                    log.warning("locked out %s (no or wrong LAN key) on %s", request.remote, request.path)
                    return web.Response(text=LOCKED, content_type="text/html", status=401)
            return await handler(request)

        app = web.Application(middlewares=[guard])
        app.router.add_get("/", self.index)                 # the P&L reports
        for page in ("/arbitrage", "/swing", "/market"):    # the details: same page, another view (see pageView())
            app.router.add_get(page, self.index)
            app.router.add_get(page + "/", self.index)
        app.router.add_get("/terminal", self.terminal)     # the read-only terminal view (web/terminal.html)
        app.router.add_get("/terminal/", self.terminal)
        app.router.add_get("/heatmap", self.heatmap_page)  # the liquidity heatmap (web/heatmap.html, arbbot/heatmap.py)
        app.router.add_get("/heatmap/", self.heatmap_page)
        app.router.add_get("/trading", self.trading)       # the Freqtrade trading bot (web/freqtrade.html, freqkit/)
        app.router.add_get("/trading/", self.trading)
        if self.freq is not None:                           # /api/freqtrade/*: reads open, writes need the token
            from freqkit.routes import add_routes
            add_routes(app, self, self.freq)
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/ws/live", self.ws_live)        # the terminal's live trades / candle / ticker (livefeed.py)
        app.router.add_get("/api/snapshot", self.snapshot)
        app.router.add_get("/api/pnl", self.pnl)
        app.router.add_get("/api/market/{kind}", self.market_data)
        app.router.add_get("/api/paper/state", self.paper_state)
        app.router.add_post("/api/paper/{op}", self.paper_action)
        app.router.add_post("/api/swing-capital", self.swing_capital)      # before the generic /api/{action}
        app.router.add_post("/api/swing-power", self.swing_power)
        app.router.add_get("/api/signal-trader", self.signal_trader_get)
        app.router.add_post("/api/signal-trader", self.signal_trader_post)   # before the generic /api/{action}
        app.router.add_get("/api/signal-types", self.signal_types_get)
        app.router.add_post("/api/signal-types", self.signal_types_post)
        app.router.add_get("/api/alerts", self.alerts_get)
        app.router.add_post("/api/alerts", self.alerts_post)
        app.router.add_post("/api/{action}", self.action)
        return app

    async def signal_trader_get(self, request: web.Request) -> web.Response:
        if self.signal_trader is None:
            return web.json_response({"error": "the signal auto-trader is off in this bot (testnet: run.py --live "
                                               "--testnet --signal-trader)"}, status=404)
        return web.json_response(self.signal_trader.view(), headers={"Cache-Control": "no-store"})

    async def signal_trader_post(self, request: web.Request) -> web.Response:
        """Switch the auto-trader's new entries on or off ({"enabled": true|false}; open positions stay managed) and/or
        change the timeframe it trades ({"window_s": 900}, one of its timeframes) and/or the most open trades
        ({"max_open": 5}) and/or the risk limits ({"risk_usd": 30} lost at the stop per new trade,
        {"daily_loss_usd": 150} of realised loss in a UTC day before new entries stop) and/or the leverage set on a
        coin before its next entry ({"leverage": 3})). {"take": "BTCUSDT", "tf": 3600} opens that coin's signal now
        (the Trade button; see SignalTrader.take)."""
        if not hmac.compare_digest(request.headers.get("X-Token", ""), self.token):
            raise web.HTTPForbidden(text="bad token")
        if self.signal_trader is None:
            return web.json_response({"ok": False, "error": "the signal auto-trader is off in this bot"}, status=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        if "take" in body:                        # the Trade button: open this coin's signal now ({"take": sym, "tf": s})
            tf = body.get("tf")
            if tf is not None and (not isinstance(tf, int) or isinstance(tf, bool)):
                return web.json_response({"ok": False, "error": "tf must be a whole number of seconds"}, status=400)
            try:
                rec = await self.signal_trader.take(body.get("take"), tf)
            except ValueError as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)
            except Exception as e:                # noqa: BLE001 - the exchange refused it: say so, never crash
                return web.json_response({"ok": False, "error": f"{type(e).__name__}: {getattr(e, 'msg', None) or e}"},
                                         status=502)
            return web.json_response({"ok": True, "taken": rec, **self.signal_trader.view()})
        on, win, most = body.get("enabled"), body.get("window_s"), body.get("max_open")
        risk, daily, lev = body.get("risk_usd"), body.get("daily_loss_usd"), body.get("leverage")
        has_size = "size_usd" in body             # a number, or 0 / null to size by risk again
        if not isinstance(on, bool) and win is None and most is None and risk is None and daily is None and lev is None \
                and not has_size:
            return web.json_response({"ok": False, "error": 'send {"enabled": true|false}, {"window_s": seconds}, '
                                                            '{"max_open": n}, {"risk_usd": usd}, '
                                                            '{"daily_loss_usd": usd} and/or {"leverage": n}'}, status=400)
        try:
            if win is not None:
                if not isinstance(win, int) or isinstance(win, bool):
                    raise ValueError("window_s must be a whole number of seconds")
                self.signal_trader.set_window(win)
            if most is not None:
                self.signal_trader.set_max_open(most)
            if risk is not None:
                self.signal_trader.set_risk_usd(risk)
            if daily is not None:
                self.signal_trader.set_daily_loss_usd(daily)
            if lev is not None:
                self.signal_trader.set_leverage(lev)
            if has_size:
                self.signal_trader.set_size_usd(body["size_usd"])
            if isinstance(on, bool):
                self.signal_trader.set_enabled(on)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, **self.signal_trader.view()})

    def _types(self):
        return getattr(self.signal_desk, "types", None)

    async def signal_types_get(self, request: web.Request) -> web.Response:
        """The signal types (arbbot/signal_types.py): built-in and custom, the condition menu for the builder, and each
        type's closed-trade results from the testnet auto-trader (when it runs)."""
        book = self._types()
        if book is None:
            return web.json_response({"error": "signal types need the combined signal (the whale watch is off)"}, status=404)
        stats = self.signal_trader.stats_by_type() if self.signal_trader is not None else None
        return web.json_response({**book.view(), "stats": stats}, headers={"Cache-Control": "no-store"})

    async def signal_types_post(self, request: web.Request) -> web.Response:
        """{"action": "toggle", "key": k, "enabled": bool} · {"action": "save", "type": {...}} (a custom type: new
        without a key, replaced with one) · {"action": "delete", "key": k}."""
        if not hmac.compare_digest(request.headers.get("X-Token", ""), self.token):
            raise web.HTTPForbidden(text="bad token")
        book = self._types()
        if book is None:
            return web.json_response({"ok": False, "error": "signal types are off in this bot"}, status=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        try:
            act = body.get("action")
            if act == "toggle":
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError("enabled must be true or false")
                book.set_enabled(str(body.get("key", "")), body["enabled"])
            elif act == "save":
                book.save(body.get("type"))
            elif act == "delete":
                book.delete(str(body.get("key", "")))
            else:
                raise ValueError('action is "toggle", "save" or "delete"')
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        stats = self.signal_trader.stats_by_type() if self.signal_trader is not None else None
        return web.json_response({"ok": True, **book.view(), "stats": stats})

    async def market_data(self, request: web.Request) -> web.Response:
        """Public Binance futures market data for the market panel (read-only; see arbbot/market.py)."""
        kind, q = request.match_info["kind"], request.query
        if kind == "whales":
            if self.whales is None:
                return web.json_response({"error": "the whale watch is off in this bot"}, status=404)
            if "window" not in q:
                return web.json_response(self.whales.view(), headers={"Cache-Control": "no-store"})
            try:                                  # the flow table's timeframe, in seconds (clamped to 1 s .. 1 week)
                window = float(q["window"])
            except ValueError:
                return web.json_response({"error": "window must be a number of seconds"}, status=400)
            return web.json_response(self.whales.view(window_s=window), headers={"Cache-Control": "no-store"})
        if kind == "desk":
            if self.desk is None:
                return web.json_response({"error": "the desk panel is not available in this bot"}, status=404)
            try:
                return web.json_response(self.desk(), headers={"Cache-Control": "no-store"})
            except Exception as e:                # a bad results file must not take the page down
                log.warning("desk view failed: %s: %s", type(e).__name__, e)
                return web.json_response({"error": f"could not read the desk results: {type(e).__name__}"}, status=500)
        if kind == "signals":                     # the combined signal (arbbot/signals.py): whales + crowd + curve + macro + news
            if self.whales is None or self.market is None:
                return web.json_response({"error": "signals need the whale watch and market data (off in this bot)"},
                                         status=404)
            if self.signal_desk is None:
                from .signals import SignalDesk
                self.signal_desk = SignalDesk(self.whales, self.market, self.macro)
            try:
                window = float(q.get("window", "3600"))
            except ValueError:
                return web.json_response({"error": "window must be a number of seconds"}, status=400)
            sym = q.get("symbol", "").upper()
            if sym and not (sym.endswith("USDT") and sym.isalnum()):
                return web.json_response({"error": "symbol must look like BTCUSDT"}, status=400)
            return web.json_response(await self.signal_desk.view(window, sym or None), headers={"Cache-Control": "no-store"})
        if kind == "whale-marks":
            if self.whales is None:
                return web.json_response({"error": "the whale watch is off in this bot"}, status=404)
            sym = q.get("symbol", "").upper()
            if not sym.endswith("USDT") or not sym.isalnum():
                return web.json_response({"error": "symbol must look like BTCUSDT"}, status=400)
            return web.json_response(self.whales.markers(sym), headers={"Cache-Control": "no-store"})
        if kind in ("macro", "news"):
            if self.macro is None:
                return web.json_response({"error": "macro & news are not available in this bot"}, status=404)
            try:
                body = await (self.macro.macro() if kind == "macro" else self.macro.news())
            except Exception as e:
                return web.json_response({"error": f"{type(e).__name__}: {str(e)[:160]}"}, status=502)
            return web.json_response(body, headers={"Cache-Control": "no-store"})
        if self.market is None:
            return web.json_response({"error": "market data is not available in this bot"}, status=404)
        if kind == "history":
            return await self.market_history(q)
        if kind == "heatmap-symbols":             # the heatmap's search: every mainnet USDT perpetual, busiest first
            try:
                return web.json_response(await (self.book_market or self.market).symbols(),
                                         headers={"Cache-Control": "no-store"})
            except Exception as e:
                return web.json_response({"error": f"{type(e).__name__}: {str(e)[:160]}"}, status=502)
        if kind == "heatmap":                     # the heatmap page: the mainnet book recorded over time
            src = self.book_market or self.market
            session = getattr(getattr(src, "client", None), "session", None)
            if session is None:
                return web.json_response({"error": "the heatmap needs a live connection to Binance (off in this bot)"},
                                         status=404)
            if self.heat is None:
                from .heatmap import Heatmap
                self.heat = Heatmap(src, session, whales=self.whales)
            num = lambda k: float(q[k]) if q.get(k) not in (None, "", "undefined", "null", "NaN") else None    # noqa: E731
            try:
                body = await self.heat.view(q.get("symbol", ""), q.get("interval", "5m"), t0=num("t0"), t1=num("t1"),
                                            res=num("res"), plo=num("plo"), phi=num("phi"), after=num("after") or 0.0)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:                # Binance unreachable, unknown coin: the page says it
                return web.json_response({"error": f"{type(e).__name__}: {str(e)[:160]}"}, status=502)
            resp = web.json_response(body, headers={"Cache-Control": "no-store"})
            resp.enable_compression()
            return resp
        if kind == "book":                        # the chart's BOOK overlay: order book + taker flow, per timeframe
            if self.book is None:
                from .orderbook import BookWatch
                self.book = BookWatch(self.book_market or self.market)
            try:
                body = await self.book.view(q.get("symbol", ""), q.get("interval", "1h"))
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(body, headers={"Cache-Control": "no-store"})
        calls = {"curve": lambda: self.market.curve(q.get("symbol", ""), q.get("period", "1d")),"klines": lambda: self.market.klines(q.get("symbol", ""), q.get("interval", "1h")),
                 "depth": lambda: self.market.depth(q.get("symbol", ""), 100 if q.get("limit") == "100" else 20),
                 "trades": lambda: self.market.trades(q.get("symbol", "")),
                 "info": lambda: self.market.info(q.get("symbol", "")),
                 "data": lambda: self.market.trading_data(q.get("symbol", ""), q.get("period", "1h")),
                 "movers": lambda: self.market.movers(),
                 "symbols": lambda: self.market.symbols()}
        if kind not in calls:
            raise web.HTTPNotFound()
        try:
            return web.json_response(await calls[kind](), headers={"Cache-Control": "no-store"})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:                    # Binance down, unknown symbol, rate limit: the panel shows it, nothing else
            return web.json_response({"error": f"{type(e).__name__}: {str(e)[:160]}"}, status=502)

    async def market_history(self, q) -> web.Response:
        """The terminal's EXPORT: candles for a basket of coins as one CSV file (or JSON with format=json)."""
        try:
            syms = [s for s in q.get("symbols", q.get("symbol", "")).upper().split(",") if s]
            interval = q.get("interval", "1d")
            years = int(q.get("years", "5")) if q.get("years", "5").isdigit() else -1
            data = await self.market.history(syms, interval, years)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {str(e)[:160]}"}, status=502)
        if q.get("format") == "json":
            return web.json_response(data, headers={"Cache-Control": "no-store"})
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["symbol", "date_utc", "open_time_ms", "open", "high", "low", "close", "volume_base", "volume_usdt",
                    "taker_buy_usdt"])
        for sym, rows in data.items():
            for r in rows:
                when = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                w.writerow([sym, when, r[0], *(repr(v) for v in r[1:])])
        name = f"{'-'.join(s.removesuffix('USDT') for s in data)}_{interval}_{years}y_binance-usdm-perp.csv"
        return web.Response(text=out.getvalue(), content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    async def paper_state(self, request: web.Request) -> web.Response:
        if self.paper is None:
            return web.json_response({"error": "the paper futures account is not running in this bot"}, status=404)
        return web.json_response(self.paper.view(), headers={"Cache-Control": "no-store"})

    async def paper_action(self, request: web.Request) -> web.Response:
        """The order ticket: a paper account (arbbot/paper_futures.py), or a real Binance futures account
        (arbbot/testnet_ticket.py: the testnet, or mainnet only under --live --real-ticket). Never the arbitrage executor."""
        if not hmac.compare_digest(request.headers.get("X-Token", ""), self.token):
            raise web.HTTPForbidden(text="bad token")
        if self.paper is None:
            return web.json_response({"ok": False, "error": "the paper futures account is not running in this bot"},
                                     status=404)
        op = request.match_info["op"]
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if op == "order":
                result = await self.paper.place_order(body)
            elif op == "cancel":
                result = await self.paper.cancel(body.get("id"))
            elif op == "close":
                result = await self.paper.close_position(body.get("symbol", ""))
            elif op == "leverage":
                result = await self.paper.set_leverage(body.get("symbol", ""), body.get("leverage"))
            elif op == "transfer":
                result = await self.paper.transfer(body.get("amount"))
            else:
                raise web.HTTPNotFound()
        except ValueError as e:                                             # a refused order: say why
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except web.HTTPException:
            raise
        except NotSentError as e:                                           # held back by the rate-limit backoff: never sent
            log.warning("ticket %s not sent: %s", op, e)
            return web.json_response({"ok": False, "error": f"Not sent, nothing changed on your account: {e.msg}. "
                                                            f"Try again then."}, status=503)
        except BinanceError as e:                                           # Binance itself refused (e.g. not enough margin)
            log.warning("ticket %s refused by Binance: %s", op, e)
            return web.json_response({"ok": False, "error": f"Binance refused it: {e.msg} (code {e.code})"}, status=400)
        except Exception as e:                                              # Binance unreachable for the price
            log.warning("paper %s failed: %s: %s", op, type(e).__name__, e)
            return web.json_response({"ok": False, "error": f"could not get a price from Binance ({type(e).__name__})"},
                                     status=502)
        return web.json_response({"ok": True, "result": result, "state": self.paper.view()})

    async def swing_power(self, request: web.Request) -> web.Response:
        """Switch the swing toolkit on or off (SwingRunner.set_enabled): off = no new entries, exits keep working."""
        if not hmac.compare_digest(request.headers.get("X-Token", ""), self.token):
            raise web.HTTPForbidden(text="bad token")
        swing = getattr(self.engine, "swing", None)
        if swing is None:
            return web.json_response({"ok": False, "error": "the swing toolkit is not running in this bot (start it with --swing)"},
                                     status=404)
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
                raise ValueError('expected {"on": true} or {"on": false}')
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        message = await swing.set_enabled(body["on"])
        return web.json_response({"ok": True, "message": message, "enabled": swing.enabled})

    async def swing_capital(self, request: web.Request) -> web.Response:
        """Give one swing paper account a new fund (see SwingRunner.set_capital: that account restarts from now)."""
        if not hmac.compare_digest(request.headers.get("X-Token", ""), self.token):
            raise web.HTTPForbidden(text="bad token")
        swing = getattr(self.engine, "swing", None)
        if swing is None:
            return web.json_response({"ok": False, "error": "the swing toolkit is not running in this bot"}, status=404)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            message = await swing.set_capital(str(body.get("strategy", "")), body.get("capital"))
        except ValueError as e:                                             # bad json, unknown account, bad amount
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:                                              # e.g. the candle download failed mid-restart
            log.warning("swing fund change: %s: %s", type(e).__name__, e)
            return web.json_response({"ok": False, "error": f"saved, but the refresh failed ({type(e).__name__}); "
                                                            "it will retry on its own"}, status=500)
        return web.json_response({"ok": True, "message": message})

    async def alerts_get(self, request: web.Request) -> web.Response:
        if self.alert_book is None:
            return web.json_response({"error": "alerts need the whale watch (it is off in this bot)"}, status=404)
        return web.json_response(dict(self.alert_book.view(), telegram=self.engine.alerts.enabled),
                                 headers={"Cache-Control": "no-store"})

    async def alerts_post(self, request: web.Request) -> web.Response:
        """Add or remove a terminal alert. No control token: an alert only sends a message, it can never trade. A page on
        another site cannot call this: the guard refuses a foreign Origin, and a JSON body forces a CORS preflight."""
        if self.alert_book is None:
            return web.json_response({"ok": False, "error": "alerts need the whale watch (it is off in this bot)"},
                                     status=404)
        if request.content_type != "application/json":
            return web.json_response({"ok": False, "error": "send JSON"}, status=415)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            action = body.get("action")
            if action == "add":
                result = self.alert_book.add(body.get("rule"))
            elif action == "remove":
                result = self.alert_book.remove(body.get("id"))
            elif action == "clear-fired":
                result = self.alert_book.clear_fired()
            else:
                raise ValueError("action must be add, remove or clear-fired")
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "result": result, "state": self.alert_book.view()})

    async def index(self, request: web.Request) -> web.Response:
        html = (self.web_dir / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def trading(self, request: web.Request) -> web.Response:
        """The Freqtrade page: same control token as the main page; without the subsystem it explains how to enable it."""
        html = (self.web_dir / "freqtrade.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
        html = html.replace("__FREQ_ENABLED__", "true" if self.freq is not None else "false")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def terminal(self, request: web.Request) -> web.Response:
        """A dense, keyboard-driven view of the same data. It cannot pause, flatten or place orders; its one control is
        the testnet signal auto-trader's timeframe, so it gets the control token (the same one the dashboard at / gets,
        behind the same LAN key)."""
        html = (self.web_dir / "terminal.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def heatmap_page(self, request: web.Request) -> web.Response:
        """The liquidity heatmap: read-only, no controls."""
        html = (self.web_dir / "heatmap.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def snapshot(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.snapshot())

    async def pnl(self, request: web.Request) -> web.Response:
        """P&L by day, week or month for one panel (read-only): ?panel=arb|swing|demo|ticket&period=day|week|month."""
        panel, period = request.query.get("panel", "arb"), request.query.get("period", "day")
        if period not in PERIODS:
            return web.json_response({"error": f"period must be one of {', '.join(PERIODS)}"}, status=400)
        swing = getattr(self.engine, "swing", None)
        try:
            if panel == "arb":
                events, note = self.engine.pf.store.pnl_events(), "arbitrage trades · after fees"
            elif panel in ("swing", "demo") and swing is not None:
                events = swing.pnl_events("demo" if panel == "demo" else "paper")
                note = ("swing kit trades on the futures testnet demo account · after fees, from Binance's fills"
                        if panel == "demo" else "swing kit paper accounts (trend, swing, smc) · after fees")
            elif panel == "ticket" and self.paper is not None:
                events, note = await self.paper.pnl_events()
            else:
                return web.json_response({"error": f"no {panel} panel in this bot"}, status=404)
        except Exception as e:                    # Binance down or rate limited for the account's history
            log.warning("pnl report %s failed: %s: %s", panel, type(e).__name__, e)
            return web.json_response({"error": f"could not read the history: {type(e).__name__}: {str(e)[:120]}"},
                                     status=502)
        return web.json_response(dict(pnl_report(events, period, time.time()), panel=panel, note=note),
                                 headers={"Cache-Control": "no-store"})

    async def action(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        supplied = request.headers.get("X-Token", "")
        if not hmac.compare_digest(supplied, self.token):
            raise web.HTTPForbidden(text="bad token")
        if action not in ACTIONS:
            raise web.HTTPNotFound()
        if action == "dismiss":
            if not self.engine.request_dismiss():
                return web.json_response({"ok": False, "error": "nothing to dismiss"}, status=409)
            return web.json_response({"ok": True, "action": action})
        {"halt": self.engine.request_halt, "resume": self.engine.request_resume,
         "flatten": self.engine.request_flatten}[action]()
        return web.json_response({"ok": True, "action": action})

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_str(json.dumps(self.engine.snapshot()))
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.clients.discard(ws)
        return ws

    async def ws_live(self, request: web.Request) -> web.WebSocketResponse:
        """The loaded coin's market data pushed as Binance sends it (arbbot/livefeed.py), from the same exchange as
        the chart's REST data. Without a Binance connection the socket just closes and the page keeps polling."""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        book = request.query.get("src") == "book"             # the heatmap: the exchange its book comes from
        client = getattr(self.book_market or self.market if book else self.market, "client", None)
        session = getattr(client, "session", None)
        if session is None:
            await ws.close()
            return ws
        attr = "live_book" if book else "live"
        if getattr(self, attr) is None:
            from .livefeed import LiveFeed
            setattr(self, attr, LiveFeed(session, getattr(client, "env", "mainnet")))
        await getattr(self, attr).serve(ws)
        return ws

    async def broadcast_loop(self) -> None:
        interval = max(0.25, self.cfg.feed.interval_s)
        while True:
            await asyncio.sleep(interval)
            if not self.clients:
                continue
            payload = json.dumps(self.engine.snapshot())
            for ws in list(self.clients):
                try:
                    await ws.send_str(payload)
                except Exception:
                    self.clients.discard(ws)
