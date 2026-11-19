"""Entry point: python -m arbbot [--feed rest|sim] [--live [--testnet]] [--no-trade]"""
import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__
from .alerts import Notifier, find_chat_ids
from .binance_client import HOSTS, BinanceClient, BinanceError
from .commands import CommandHandler, TelegramListener
from .config import load
from .engine import Engine
from .executor import PaperExecutor
from .feeds import WS_DEFAULTS, WS_MARK_PATH, Board, HybridFeed, RestFeed, SimFeed, WsFeed
from .health import hosted_port
from .hosted_dashboard import MIN_PASSWORD, make_hosted_app, settings_from_env
from .live import LiveExecutor
from .market import MarketData
from .paper_futures import PaperFutures
from .testnet_ticket import TestnetTicket
from .portfolio import Portfolio, Store
from .publisher import SnapshotPublisher
from .recorder import DepthSampler, Recorder
from .risk import Risk
from .scanner import Scanner, round_trip_cost_bps
from .server import Dashboard
from .sizing import RestDepth

ROOT = Path(__file__).resolve().parent.parent
LIVE_ACK = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"
VENUE_NAME = {"fapi": "futures", "spot": "spot"}
# Which environment variables hold the keys. On testnet, spot and futures are two separate exchanges with separate keys;
# each falls back to BINANCE_API_KEY / BINANCE_API_SECRET when its own pair is not set.
KEY_VARS = {
    "mainnet": {"fapi": ("BINANCE_API_KEY", "BINANCE_API_SECRET"), "spot": ("BINANCE_API_KEY", "BINANCE_API_SECRET")},
    "testnet": {"fapi": ("BINANCE_TESTNET_FUTURES_API_KEY", "BINANCE_TESTNET_FUTURES_API_SECRET"),
                "spot": ("BINANCE_TESTNET_SPOT_API_KEY", "BINANCE_TESTNET_SPOT_API_SECRET")},
}


def live_keys(env: str, environ=None) -> dict[str, tuple[str, str, str]]:
    """{venue: (key, secret, name of the variable it came from)}. Values are never logged, only the variable names."""
    environ = os.environ if environ is None else environ
    out = {}
    for venue, (k, s) in KEY_VARS[env].items():
        if environ.get(k) and environ.get(s):
            out[venue] = (environ[k], environ[s], k)
        else:
            out[venue] = (environ.get("BINANCE_API_KEY", ""), environ.get("BINANCE_API_SECRET", ""), "BINANCE_API_KEY")
    return out


def explain_binance_error(e: BinanceError, env: str, keys: dict) -> str:
    """A refusal from Binance, in words: which exchange, which key variable, and what to check."""
    venue = "fapi" if e.path.startswith(("/fapi", "/futures")) else "spot"
    name, var = VENUE_NAME[venue], keys[venue][2]
    host = HOSTS[env][venue]
    if e.status in (401, 403) or e.code in (-2014, -2015, -1022):
        return (f"Binance {env} {name} ({host}) rejected the key from {var}: HTTP {e.status}, code {e.code}, "
                f"{e.msg}. Check the key was created for {host}, has {name} trading permission, and has no IP "
                f"restriction blocking this machine.")
    return f"Binance {env} {name} ({host}) refused {e.path}: HTTP {e.status}, code {e.code}, {e.msg}"


async def run_check(cfg, keys: dict) -> int:
    """--check: every read-only step of the live preflight, one by one, so the report says exactly what works.
    Nothing here can place an order: it only reads the clock, the account mode, balances and fee rates."""
    env = cfg.execution.env
    print(f"Read-only check of the {env} live setup. No orders are sent; the bot is not started.")
    for venue in ("fapi", "spot"):
        print(f"  {VENUE_NAME[venue]:8s} {HOSTS[env][venue]}  key from {keys[venue][2]}"
              + ("" if keys[venue][0] and keys[venue][1] else "  (NOT SET)"))
    ok = True
    async with aiohttp.ClientSession() as session:
        client = BinanceClient(session, env, timeout_s=10.0, venue_keys={v: (k, s) for v, (k, s, _) in keys.items()})
        ex = LiveExecutor(client, cfg)

        async def step(label, coro_fn, show):
            nonlocal ok
            try:
                print(f"  ✔ {label}: {show(await coro_fn())}")
            except BinanceError as e:
                ok = False
                print(f"  ✘ {label}: {explain_binance_error(e, env, keys)}")
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                ok = False
                print(f"  ✘ {label}: {type(e).__name__}: {e}")

        await step("clock", client.sync_time, lambda off: f"offset {off} ms")
        await step("futures account mode", lambda: client.get("fapi", "/fapi/v1/positionSide/dual", signed=True),
                   lambda d: "Hedge Mode: switch to One-way Mode before trading" if d.get("dualSidePosition")
                   else "One-way Mode (ok)")
        await step("futures balance", lambda: ex._fut_get("/fapi/v3/balance", "/fapi/v2/balance"),
                   lambda rows: f"{next((float(b.get('availableBalance', 0)) for b in rows if b.get('asset') == 'USDT'), 0.0):,.2f} USDT available")
        await step("spot account", lambda: client.get("spot", "/api/v3/account", signed=True),
                   lambda a: f"{next((float(b['free']) for b in a['balances'] if b['asset'] == 'USDT'), 0.0):,.2f} USDT free"
                   + ("" if a.get("canTrade", True) else " (canTrade is FALSE)"))
        await step("fee rates", ex.fetch_fees, lambda f: f"futures taker {f[0]:.2f} bps, spot taker {f[1]:.2f} bps")
    print("All checks passed. Nothing was traded." if ok else "Some checks failed (see above). Nothing was traded.")
    return 0 if ok else 1


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="arbbot", description="Binance USD-M perp/spot basis scanner and trader")
    p.add_argument("--config", default=str(ROOT / "config.toml"))
    p.add_argument("--feed", choices=["ws", "rest", "sim"], help="override feed.mode (ws = streams with REST fallback)")
    p.add_argument("--interval", type=float, help="override feed.interval_s (seconds)")
    p.add_argument("--port", type=int, help="override server.port")
    p.add_argument("--lan", action="store_true",
                   help="also serve the dashboard to phones on your home network (same router); other devices need the "
                        "private key in the phone link printed at start (data/lan-key-<port>.txt)")
    p.add_argument("--no-commands", action="store_true",
                   help="Telegram alerts only, no commands (only one bot per Telegram token may poll for commands)")
    p.add_argument("--max-markets", type=int, help="override universe.max_markets")
    p.add_argument("--min-samples", type=int, help="override strategy.min_samples (shorter warm-up)")
    p.add_argument("--no-trade", action="store_true", help="monitor only: never open positions")
    p.add_argument("--live", action="store_true", help="send REAL orders (needs API keys; mainnet also needs ARBBOT_LIVE_ACK)")
    p.add_argument("--testnet", action="store_true", help="use Binance testnet hosts (with --live or --check)")
    p.add_argument("--check", action="store_true",
                   help="read-only check of the live setup (keys, permissions, account mode, balances, fees) on mainnet "
                        "or --testnet, then exit. Sends NO orders and does not start the bot")
    p.add_argument("--hosted", action="store_true",
                   help="container-host mode (paper only): serve a READ-ONLY dashboard behind a login (DASHBOARD_PASSWORD) "
                        "plus /healthz on 0.0.0.0:$PORT instead of the local dashboard; the recorder stays off unless "
                        "ARBBOT_RECORD=1")
    p.add_argument("--no-whales", action="store_true",
                   help="do not stream Binance's big trades and liquidations for the terminal's whale panels")
    p.add_argument("--real-ticket", action="store_true",
                   help="MAINNET only (with --live): the dashboard's order ticket sends REAL-MONEY orders to your Binance "
                        "futures account instead of a paper account (capped by --real-ticket-max-usd)")
    p.add_argument("--real-ticket-max-usd", type=float, default=100.0,
                   help="largest value in USDT of one real-money ticket order (default 100); closing is never capped")
    p.add_argument("--real-ticket-max-total-usd", type=float, default=None,
                   help="largest value in USDT of all the real-money ticket's positions and open orders together "
                        "(default 3x --real-ticket-max-usd); closing is never capped")
    p.add_argument("--swing-mirror", metavar="trend,swing,smc|all",
                   help="TESTNET only (with --swing --live --testnet): copy these swing accounts' new trades onto the "
                        "futures testnet as real testnet orders with exchange-side stops (one account, a comma "
                        "separated list, or all; a coin another mirrored account already holds is skipped)")
    p.add_argument("--swing-mirror-copy-open", action="store_true",
                   help="with --swing-mirror: also copy positions the accounts already hold (entered at today's price, "
                        "with the paper stop), not only new entries")
    p.add_argument("--swing-mirror-fund", type=float, default=10_000.0,
                   help="size the testnet copies as if the mirrored account held this many dollars (default 10000)")
    p.add_argument("--signal-trader", action="store_true",
                   help="TESTNET only (with --live --testnet): auto-trade the terminal's combined signal (whales, crowd, "
                        "futures curve, macro, news) with test money; entries can be switched off from the terminal")
    p.add_argument("--signal-button", action="store_true",
                   help="MAINNET with --real-ticket: the terminal's Trade button on a signal sends a REAL order (you "
                        "confirm each one). Automatic entries stay locked off. Starts at $5 risk per trade, $20 daily "
                        "loss, 2 open trades, 3x leverage (change them on the terminal)")
    p.add_argument("--signal-trader-window", default="1h", metavar="1m|5m|15m|30m|1h|4h|1d|1w",
                   help="the signal's timeframe; it also sets how long an entry waits and a trade is held (default 1h)")
    p.add_argument("--signal-trader-risk-usd", type=float, default=20.0,
                   help="test USDT lost if a trade's stop is hit: the position is sized from it (default 20)")
    p.add_argument("--signal-trader-max-usd", type=float, default=2000.0,
                   help="largest position value in test USDT (default 2000)")
    p.add_argument("--signal-trader-max-open", type=int, default=3,
                   help="most positions and pending entries at once (default 3)")
    p.add_argument("--signal-trader-min-conf", type=int, default=60,
                   help="lowest signal confidence (0-100) to enter on, or to exit on a flip (default 60)")
    p.add_argument("--signal-trader-daily-loss-usd", type=float, default=100.0,
                   help="no new entries after this much realised test-USDT loss in a UTC day (default 100)")
    p.add_argument("--signal-trader-leverage", type=int, default=5,
                   help="leverage it sets on a coin before entering (1-60, default 5): Binance auto-deleverages the "
                        "most profitable x most leveraged positions first, so lower is safer; it needs more margin")
    p.add_argument("--swing", action="store_true",
                   help="also paper-trade the swingkit strategies (trend, swing, SMC) in this process: alerts plus the "
                        "/setups /journal /swing Telegram commands. Paper only; separate from the arbitrage book")
    try:  # the --swing-* options live with the toolkit; an install without swingkit/ simply doesn't offer them
        from swingkit.paper import add_paper_args
        add_paper_args(p, "swing-")
    except ImportError:
        pass
    p.add_argument("--seed", type=int, help="random seed for the simulator")
    p.add_argument("--test-alert", action="store_true", help="send one Telegram test message and exit")
    p.add_argument("--find-chat-id", action="store_true", help="list Telegram chats that messaged your bot, then exit")
    return p.parse_args(argv)


async def telegram_utility(args: argparse.Namespace, cfg, token: str, chat_id: str) -> None:
    async with aiohttp.ClientSession() as session:
        if args.find_chat_id:
            if not token:
                sys.exit("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
            try:
                chats = await find_chat_ids(session, cfg.alerts, token)
            except Exception as e:
                sys.exit(f"Telegram error: {type(e).__name__}: {str(e).replace(token, '***')}")
            if not chats:
                sys.exit("No chats found. Open your bot in Telegram, press Start (or send any message), then run this again.")
            for c in chats:
                print(f"chat id {c['id']}  ({c['type']}, {c['name']})")
            print("Put that id in .env as TELEGRAM_CHAT_ID=<id>, then run --test-alert.")
            return
        notifier = Notifier(cfg.alerts, token, chat_id, "TEST", session)
        ok, detail = await notifier.send_now("🤖 arbbot test alert: Telegram alerts are wired up.")
        if not ok:
            sys.exit(f"Test alert FAILED: {detail}")
        print("Test alert sent. Check Telegram.")


def initial_pause(saved: str | None, start_paused: bool) -> str:
    """Why entries begin paused at startup: "saved" (you paused before the restart), "default"
    (risk.start_paused = true), or "" (they begin active)."""
    if saved == "1":
        return "saved"
    return "default" if start_paused else ""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_h = logging.handlers.RotatingFileHandler(ROOT / "logs" / "bot.log", maxBytes=5_000_000, backupCount=3)
    file_h.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_h)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def amain(args: argparse.Namespace) -> None:
    load_dotenv(ROOT / ".env")
    if not Path(args.config).is_file():   # never fall back to built-in defaults silently (ports, recorder, sizes...)
        sys.exit(f"config file not found: {args.config}")
    cfg = load(args.config)
    if args.feed:
        cfg.feed.mode = args.feed
    if args.interval:
        cfg.feed.interval_s = args.interval
    if args.port:
        cfg.server.port = args.port
    if args.no_commands:
        cfg.alerts.commands = False
    if args.max_markets:
        cfg.universe.max_markets = args.max_markets
    if args.min_samples:
        cfg.strategy.min_samples = args.min_samples
        cfg.strategy.lookback = max(cfg.strategy.lookback, args.min_samples)
    if args.testnet:
        cfg.execution.env = "testnet"
    if args.hosted:
        if args.live:
            sys.exit("--hosted is paper-only: real orders are never sent from an unattended host. Run --live yourself.")
        cfg.recorder.enabled = os.environ.get("ARBBOT_RECORD") == "1"  # ephemeral disks fill up; opt in with a disk

    # Two-key rule for real orders: config says live AND --live is passed.
    if cfg.execution.mode == "live" and not args.live:
        sys.exit("config.toml sets execution.mode='live' but --live was not passed. Refusing to start.")
    live = args.live
    if live:
        cfg.execution.mode = "live"
        if cfg.feed.mode == "sim":
            sys.exit("--live requires a real feed (--feed ws or rest); the simulator cannot drive real orders.")
        if cfg.execution.env == "mainnet" and os.environ.get("ARBBOT_LIVE_ACK") != LIVE_ACK:
            sys.exit(f"Mainnet live trading needs ARBBOT_LIVE_ACK={LIVE_ACK} in the environment. Try --testnet first.")
        for venue, (k, s, _) in live_keys(cfg.execution.env).items():
            if not (k and s):
                want = " and ".join(KEY_VARS[cfg.execution.env][venue])
                sys.exit(f"--live needs {VENUE_NAME[venue]} keys: set {want} (or BINANCE_API_KEY and "
                         f"BINANCE_API_SECRET) in .env (see .env.example).")
    else:
        cfg.execution.mode = "paper"
    cfg.risk.kill_file = str(ROOT / cfg.risk.kill_file) if not os.path.isabs(cfg.risk.kill_file) else cfg.risk.kill_file
    cfg.validate()

    if args.real_ticket and not (live and cfg.execution.env == "mainnet"):
        sys.exit("--real-ticket sends real-money orders from the dashboard: it needs --live on mainnet (the testnet "
                 "bot's ticket already trades the testnet)")
    if args.real_ticket and not args.real_ticket_max_usd > 0:
        sys.exit("--real-ticket-max-usd must be above 0")
    if args.real_ticket and args.real_ticket_max_total_usd is not None \
            and not args.real_ticket_max_total_usd >= args.real_ticket_max_usd:
        sys.exit("--real-ticket-max-total-usd must be at least --real-ticket-max-usd")
    if args.swing_mirror and not (args.swing and live and cfg.execution.env == "testnet"):
        sys.exit("--swing-mirror copies swing trades to the TESTNET only: it needs --swing --live --testnet")
    if args.swing_mirror:                # a typo fails here, before anything connects
        from .swing_mirror import parse_accounts
        try:
            parse_accounts(args.swing_mirror)
        except ValueError as e:
            sys.exit(str(e))
    if args.signal_trader and not (live and cfg.execution.env == "testnet"):
        sys.exit("--signal-trader trades the combined signal on the TESTNET only: it needs --live --testnet")
    if args.signal_button and not (live and cfg.execution.env == "mainnet" and args.real_ticket):
        sys.exit("--signal-button is the MAINNET Trade button: it needs --live, the mainnet config and --real-ticket")
    if args.signal_button and args.signal_trader:
        sys.exit("--signal-button and --signal-trader do not go together (the auto-trader is testnet only)")
    if (args.signal_trader or args.signal_button) and args.no_whales:
        sys.exit("--signal-trader reads the whale watch: drop --no-whales")
    WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14_400, "1d": 86_400, "1w": 604_800}
    if args.signal_trader and args.signal_trader_window.lower() not in WINDOWS:
        sys.exit(f"--signal-trader-window takes {', '.join(WINDOWS)}")
    if args.signal_trader and not 1 <= args.signal_trader_leverage <= 60:
        sys.exit("--signal-trader-leverage takes 1 to 60")
    if args.check:   # read-only: never builds the engine, never sends an order
        sys.exit(await run_check(cfg, live_keys(cfg.execution.env)))
    setup_logging()
    log = logging.getLogger("main")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if args.test_alert or args.find_chat_id:
        await telegram_utility(args, cfg, token, chat_id)
        return
    mode = "live" if live else "paper"
    db_path = ROOT / "data" / f"{mode}-{cfg.execution.env}-{'sim' if cfg.feed.mode == 'sim' else 'real'}.db"
    now = time.time()
    store = Store(db_path)
    portfolio = Portfolio(store, now)
    risk = Risk(cfg.risk, portfolio)
    why_paused = initial_pause(store.get_setting("manual_halt"), cfg.risk.start_paused)
    paused_before, start_paused = why_paused == "saved", why_paused == "default"
    risk.manual_halt = bool(why_paused)
    scanner = Scanner(cfg.strategy, cfg.fees, cfg.feed.max_quote_age_s)
    board = Board(cfg.feed.mode)

    async with aiohttp.ClientSession() as session:
        keys = live_keys(cfg.execution.env) if live else {}
        client = BinanceClient(
            session, cfg.execution.env,
            os.environ.get("BINANCE_API_KEY", "") if live else "",
            os.environ.get("BINANCE_API_SECRET", "") if live else "",
            cfg.feed.request_timeout_s,
            venue_keys={v: (k, s) for v, (k, s, _) in keys.items()},
        )
        if live:
            executor = LiveExecutor(client, cfg)
            try:
                await executor.preflight()
            except BinanceError as e:   # refuse to start, in words, before any order could be sent
                sys.exit("Live preflight failed, nothing was traded. " + explain_binance_error(e, cfg.execution.env, keys))
            try:  # the edge calculation must use what this account really pays, not the VIP-0 defaults
                fut_bps, spot_bps = await executor.fetch_fees()
                cfg.fees.futures_taker_bps, cfg.fees.spot_taker_bps = fut_bps, spot_bps
                log.info("real fee tier: futures taker %.2f bps, spot taker %.2f bps (config.toml values replaced)",
                         fut_bps, spot_bps)
            except Exception as e:
                log.warning("could not read real fee rates (%s: %s); using the config.toml fees", type(e).__name__, e)
        else:
            executor = PaperExecutor(cfg.fees)

        if cfg.feed.mode in ("rest", "ws"):
            rest = RestFeed(client, board, cfg, with_filters=live)
            try:
                await rest.load_universe(must_include=frozenset(portfolio.open))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                sys.exit(f"Could not load the market universe from Binance ({type(e).__name__}: {e}). "
                         "Check your connection, or try --feed sim for offline data.")
            feed = rest
            if cfg.feed.mode == "ws":
                fut_default, spot_default = WS_DEFAULTS[cfg.execution.env]
                ws = WsFeed(session, board, cfg, cfg.feed.ws_futures_url or fut_default,
                            cfg.feed.ws_spot_url or spot_default,
                            mark_path="" if cfg.feed.ws_futures_url else WS_MARK_PATH[cfg.execution.env])
                feed = HybridFeed(ws, rest, board, cfg)
        else:
            feed = SimFeed(board, cfg, seed=args.seed)
            feed.load_universe()
            feed.step()

        label = "SIM" if cfg.feed.mode == "sim" else f"{mode.upper()} {cfg.execution.env}"
        alerts = Notifier(cfg.alerts, token, chat_id, label, session)
        # The dashboard's order ticket: REAL testnet orders under --live --testnet, REAL-MONEY mainnet orders only under
        # --live --real-ticket (capped per order, blocked by the KILL file), a paper futures account everywhere else.
        if live and (cfg.execution.env == "testnet" or args.real_ticket):
            kill = cfg.risk.kill_file
            ticket = TestnetTicket(client, ROOT / "data" / f"ticket-{db_path.stem}.json",
                                   arb_symbols=lambda: set(portfolio.open), notify=alerts.send,
                                   taker_bps=cfg.fees.futures_taker_bps, real_money=args.real_ticket,
                                   max_order_usd=args.real_ticket_max_usd if args.real_ticket else None,
                                   max_total_usd=args.real_ticket_max_total_usd if args.real_ticket else None,
                                   default_leverage=cfg.execution.leverage if args.real_ticket else 15,
                                   blocked=lambda: f"the kill file {kill} is present" if os.path.exists(kill) else "")
            risk.external = ticket.owned              # the arbitrage bot stays off the ticket's coins
            if args.real_ticket:
                log.warning("REAL-MONEY order ticket ON: dashboard orders go to your Binance futures account (max %g "
                            "USDT per order, %g USDT in all, default leverage %dx). Pausing the arbitrage bot does not "
                            "pause the ticket.", args.real_ticket_max_usd, ticket.max_total_usd, cfg.execution.leverage)
        else:
            # one paper futures account per bot state, so two bots (e.g. paper + testnet) never write the same file
            pf_name = "paper-futures.json" if db_path.stem == "paper-mainnet-real" else f"paper-futures-{db_path.stem}.json"
            ticket = PaperFutures(ROOT / "data" / pf_name, client, cfg.fees.futures_taker_bps, notify=alerts.send)
        if live:  # never trade on top of a book that disagrees with the exchange
            try:
                problems = await executor.reconcile(portfolio.open, board.markets,
                                                    ignore=ticket.owned() if isinstance(ticket, TestnetTicket) else frozenset())
                unknown = executor.unknown_positions
            except Exception as e:
                problems, unknown = [f"could not reconcile with the exchange: {type(e).__name__}: {e}"], {}
            for problem in problems:
                log.error("RECONCILE: %s", problem)
            if risk.trip_reconcile(problems, unknown, store.get_setting("dismissed_unknown")):
                if risk.fault_dismissable:
                    log.warning("RECONCILE: dismiss the warning from the dashboard to trade the other coins")
            elif problems:
                log.warning("RECONCILE: these positions were dismissed earlier; staying off %s", ", ".join(sorted(unknown)))
            else:
                log.info("reconciled: saved positions match the exchange")
        depth = RestDepth(client) if (cfg.feed.mode in ("rest", "ws") and cfg.risk.use_depth_sizing) else None
        recorder = None
        # recordings are mainnet market data for backtests: never synthetic (sim) and never testnet prices
        if cfg.recorder.enabled and cfg.feed.mode != "sim" and cfg.execution.env == "mainnet":
            recorder = Recorder(cfg.recorder, cfg.feed.max_quote_age_s, root=ROOT, fees=cfg.fees)
        engine = Engine(cfg, board, scanner, portfolio, risk, executor, trading=not args.no_trade, alerts=alerts,
                        depth=depth, recorder=recorder)
        engine.stuck |= getattr(executor, "mismatched", set())   # a saved size the exchange disputes is never auto-closed
        sampler = DepthSampler(recorder, depth, board, cfg.recorder.depth_every_s) if (recorder and depth) else None
        handler = CommandHandler(engine, label)
        swing, mirror = None, None
        if args.swing:  # optional second paper account family; imported lazily so the arbitrage bot never needs it
            try:
                from swingkit.journal import Journal
                from swingkit.paper import DEFAULT_SYMBOLS, SwingRunner, paper_cfg_from, resolve_spec
                spec = args.swing_symbols
                if spec.strip().lower() == "arb" and cfg.feed.mode == "sim":
                    spec = ",".join(DEFAULT_SYMBOLS)    # the simulated feed's markets do not exist on Binance
                    log.warning("swing: the simulated feed has no real markets; paper-trading %s instead", spec)
                # swing signals always come from real (mainnet) candles: on testnet a key-less public mainnet client
                swing_client = client if cfg.execution.env == "mainnet" else BinanceClient(
                    session, "mainnet", timeout_s=cfg.feed.request_timeout_s)
                if args.swing_journal_db == "data/swing-journal.db":   # the default: one journal per bot, never mixed
                    if cfg.execution.env != "mainnet":
                        args.swing_journal_db = "data/swing-journal-testnet.db"
                    elif mode == "live":
                        args.swing_journal_db = "data/swing-journal-live-mainnet.db"
                    # paper on mainnet keeps data/swing-journal.db (the paper bot's journal)
                # "arb" = the very markets the arbitrage bot scans (same universe, same volume ranking)
                symbols, auto = await resolve_spec(spec, swing_client, list(board.markets))
                swing = SwingRunner(paper_cfg_from(args, "swing-", symbols, auto), swing_client,
                                    Journal(ROOT / args.swing_journal_db), notify=alerts.send)
                if args.swing_mirror:
                    from .swing_mirror import SwingMirror, parse_accounts, state_path
                    mirror = [SwingMirror(swing, ticket, name, args.swing_mirror_fund, state_path(ROOT / "data", name),
                                          notify=alerts.send, copy_existing=args.swing_mirror_copy_open)
                              for name in parse_accounts(args.swing_mirror)]
                    swing.mirrors = mirror                 # the dashboard shows the testnet results next to the paper
                    ticket.source = lambda sym: next((f"swing:{m.strategy}" for m in mirror if sym in m.s["open"]), "")
            except ImportError:
                sys.exit("--swing needs the swingkit/ folder next to arbbot/.")
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                sys.exit(f"--swing: {e}")
            handler.swing = engine.swing = swing
        listener = TelegramListener(cfg.alerts, alerts, handler, poll_timeout_s=cfg.alerts.poll_timeout_s)
        phone_key = None
        if args.lan and not args.hosted:      # home network too; the key keeps other devices out
            from .server import lan_key
            phone_key = lan_key(ROOT / "data" / f"lan-key-{cfg.server.port}.txt")
            cfg.server.host = "0.0.0.0"
        stats_client = None if cfg.execution.env == "mainnet" else BinanceClient(   # sentiment: mainnet only
            session, "mainnet", timeout_s=cfg.feed.request_timeout_s)
        from deskkit.report import desk_view      # research gates + promotion ladder, read-only (python desk.py research)
        from deskkit.rules import DeskRules
        desk_rules, desk_journal = DeskRules.load(ROOT / "desk_rules.toml"), ROOT / args.swing_journal_db
        desk_view_fn = lambda: desk_view(ROOT / "data" / "desk", [desk_journal], desk_rules)  # noqa: E731
        whales, alert_book = None, None
        if not (args.hosted or args.no_whales):   # the terminal's whale tape: public mainnet streams, never traded on
            from .price_alerts import AlertBook
            from .whales import WhaleWatch
            whales = WhaleWatch(session, stats_client or client)
            # the terminal's alerts (price, funding, whale, liquidation) ride on the same streams; sent to Telegram
            alert_book = AlertBook(ROOT / "data" / f"alerts-{db_path.stem}.json", send=alerts.send)
            whales.on_marks, whales.on_event = alert_book.check_marks, alert_book.on_event
        from .macro import MacroNews          # the terminal's MACRO / NEWS panel: public data, fetched only when asked
        freq = None
        if cfg.freqtrade.enabled and not args.hosted:   # the Freqtrade subsystem: never on an unattended host
            from freqkit.manager import FreqtradeManager
            from freqkit.store import FreqStore
            freq = FreqtradeManager(ROOT, cfg.freqtrade, FreqStore(ROOT / "data" / "freqtrade.db"), session)
        macro = None if args.hosted else MacroNews(session)
        signal_desk, trader = None, None
        if whales is not None:
            from .signals import SignalDesk
            # the combined signal reads mainnet only (a testnet bot's own candles are not the real market)
            from .signal_types import TypeBook
            # signal types (built-in on/off and your custom ones), saved per environment
            types = TypeBook(ROOT / "data" / f"signal-types-{cfg.execution.env}.json", notify=alerts.send)
            signal_desk = SignalDesk(whales, MarketData(stats_client or client, stats_client=stats_client), macro,
                                     types=types)
        if args.signal_trader:
            from .signal_trader import SignalTrader
            trader = SignalTrader(signal_desk, ticket, WINDOWS[args.signal_trader_window.lower()],
                                  risk_usd=args.signal_trader_risk_usd, max_open=args.signal_trader_max_open,
                                  min_conf=args.signal_trader_min_conf, max_usd=args.signal_trader_max_usd,
                                  daily_loss_usd=args.signal_trader_daily_loss_usd,
                                  leverage=args.signal_trader_leverage,
                                  path=ROOT / "data" / "signal-trader-testnet.json", notify=alerts.send)
        elif args.signal_button and signal_desk is not None:
            # MAINNET: the Trade button only, real money, small defaults (the page can change them); never enters itself
            from .signal_trader import SignalTrader
            trader = SignalTrader(signal_desk, ticket, WINDOWS[args.signal_trader_window.lower()], risk_usd=5.0,
                                  max_open=2, min_conf=args.signal_trader_min_conf, max_usd=args.real_ticket_max_usd,
                                  daily_loss_usd=20.0, leverage=3, manual_only=True,
                                  path=ROOT / "data" / "signal-trader-mainnet.json", notify=alerts.send)
            log.warning("signal Trade button ON for the MAINNET account (real money; automatic entries locked off)")
        if trader is not None:
            other = ticket.source
            ticket.source = lambda sym: "signal" if trader.holds(sym) else other(sym)
        # MAINNET: a second account (Binance's main account beside the sub-account of BINANCE_API_KEY), switched from the
        # terminal; each runs its own ticket and Trade button at once (arbbot/accounts.py)
        accounts, extra = None, []
        main_key, main_secret = os.environ.get("BINANCE_MAIN_API_KEY", ""), os.environ.get("BINANCE_MAIN_API_SECRET", "")
        if args.real_ticket and isinstance(ticket, TestnetTicket) and main_key and main_secret:
            from .accounts import Accounts
            main_client = BinanceClient(session, cfg.execution.env, main_key, main_secret, cfg.feed.request_timeout_s,
                                        venue_keys={"fapi": (main_key, main_secret), "spot": (main_key, main_secret)})
            main_client.time_offset_ms = client.time_offset_ms
            problem = ""
            try:                                   # the ticket trades one-way positions only, like the sub-account
                if (await main_client.get("fapi", "/fapi/v1/positionSide/dual", signed=True)).get("dualSidePosition"):
                    problem = "it is in Hedge mode: switch it to One-way in Binance's futures settings"
            except BinanceError as e:
                problem = explain_binance_error(e, cfg.execution.env, {"fapi": ("", "", "BINANCE_MAIN_API_KEY"),
                                                                       "spot": ("", "", "BINANCE_MAIN_API_KEY")})
            except Exception as e:                 # noqa: BLE001 - network: the account still loads and retries
                log.warning("main account: first read failed (%s); it retries", e)
            if problem:
                log.error("main account NOT added: %s", problem)
                alerts.send(f"⚠️ Main account not added to the dashboard: {problem}")
            else:
                accounts = Accounts(ROOT / "data" / f"active-account-{db_path.stem}.json", notify=alerts.send)
                ticket.notify = lambda text, _send=alerts.send: _send("[SUB] " + text)
                if trader is not None:
                    trader.notify = ticket.notify
                accounts.add("sub", "SUB", ticket, trader)
                main_send = lambda text, _send=alerts.send: _send("[MAIN] " + text)    # noqa: E731
                main_ticket = TestnetTicket(main_client, ROOT / "data" / f"ticket-{db_path.stem}-main.json",
                                            notify=main_send, taker_bps=cfg.fees.futures_taker_bps, real_money=True,
                                            max_order_usd=args.real_ticket_max_usd,
                                            max_total_usd=args.real_ticket_max_total_usd,
                                            default_leverage=cfg.execution.leverage, blocked=ticket.blocked)
                main_trader = None
                if trader is not None:
                    from .signal_trader import SignalTrader
                    main_trader = SignalTrader(signal_desk, main_ticket, WINDOWS[args.signal_trader_window.lower()],
                                               risk_usd=5.0, max_open=2, min_conf=args.signal_trader_min_conf,
                                               max_usd=args.real_ticket_max_usd, daily_loss_usd=20.0, leverage=3,
                                               manual_only=True, notify=main_send,
                                               path=ROOT / "data" / "signal-trader-mainnet-main.json")
                    main_ticket.source = lambda sym, _t=main_trader: "signal" if _t.holds(sym) else ""
                    extra.append(main_trader.run())
                extra.append(main_ticket.run())
                accounts.add("main", "MAIN", main_ticket, main_trader)
                log.warning("MAIN account added (real money, same caps as the sub-account: max %g USDT per order); "
                            "the dashboard trades the %s account now (switch it on the terminal)",
                            args.real_ticket_max_usd, accounts.label)
        # Telegram: /ideas reads the combined signal, /auto drives the auto-trader, /account lists the exchange account
        handler.signal_desk, handler.signal_trader = signal_desk, trader
        handler.ticket = ticket if isinstance(ticket, TestnetTicket) else None
        dash = Dashboard(engine, cfg, ROOT / "web", market=MarketData(client, stats_client=stats_client), paper=ticket,
                         lan_key=phone_key, whales=whales, alert_book=alert_book, desk=desk_view_fn,
                         macro=macro, freq=freq, signal_desk=signal_desk, signal_trader=trader,
                         book_market=MarketData(stats_client, stats_client=stats_client) if stats_client else None,
                         accounts=accounts)
        if args.hosted:  # public interface: the READ-ONLY page behind a login, never the dashboard with its controls
            hosted_password, hosted_user = settings_from_env()
            runner = web.AppRunner(make_hosted_app(engine, ROOT / "vercel-dashboard" / "views" / "dashboard.html",
                                                   hosted_password, hosted_user))
            host, port = "0.0.0.0", hosted_port()
        else:
            runner = web.AppRunner(dash.make_app())
            host, port = cfg.server.host, cfg.server.port
        await runner.setup()
        await web.TCPSite(runner, host, port).start()

        banner_mode = "LIVE (REAL ORDERS)" if live else "PAPER (no orders sent)"
        log.info("arbbot %s | %s | feed=%s env=%s", __version__, banner_mode, cfg.feed.mode, cfg.execution.env)
        log.info("scanning %d markets | round-trip cost %.1f bps | z>=%.1f net>=%.1f bps",
                 len(board.markets), round_trip_cost_bps(cfg.fees), cfg.strategy.z_entry, cfg.strategy.min_net_edge_bps)
        if args.hosted:
            if len(hosted_password) >= MIN_PASSWORD:
                log.info("hosted mode: READ-ONLY dashboard on %s:%d (login user %r) | state: %s | pause entries with the "
                         "Telegram /pause command", host, port, hosted_user, db_path.name)
            else:
                seen = "is not set" if not hosted_password else f"has only {len(hosted_password)} characters"
                log.warning("hosted mode: no dashboard because DASHBOARD_PASSWORD %s (it needs at least %d); only /healthz "
                            "answers on %s:%d | state: %s", seen, MIN_PASSWORD, host, port, db_path.name)
        else:
            log.info("dashboard: http://%s:%d   | state: %s | kill switch: touch %s",
                     "127.0.0.1" if host == "0.0.0.0" else host, port, db_path.name, cfg.risk.kill_file)
            if phone_key:
                from .server import lan_addresses
                for ip in lan_addresses() or ["<this Mac's Wi-Fi address>"]:
                    log.info("phone: http://%s:%d/?key=%s   (same Wi-Fi; open it once, the phone remembers the key)",
                             ip, port, phone_key)
                if live and cfg.execution.env == "mainnet":
                    log.warning("phone access is plain HTTP: anyone who can watch this Wi-Fi's traffic can copy the key "
                                "and use this REAL-MONEY dashboard. Use it only on a network you trust (never public "
                                "Wi-Fi); delete data/lan-key-%d.txt and restart to revoke it", port)
        if portfolio.open:
            log.warning("resuming with %d open position(s): %s", len(portfolio.open), ", ".join(portfolio.open))
        if cfg.feed.mode == "sim":
            log.warning("SIMULATED FEED: prices and dislocations are synthetic")
        if alerts.enabled:
            log.info("Telegram alerts: ON (signals=%s trades=%s halts=%s) | commands: %s",
                     cfg.alerts.notify_signals, cfg.alerts.notify_trades, cfg.alerts.notify_halts,
                     "ON (your chat only; try /help)" if cfg.alerts.commands else "off")
        else:
            log.info("Telegram alerts: off (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env; test with --test-alert)")
        if recorder is not None:
            log.info("recorder: ON -> %s (backtest with: python backtest.py info|slippage|replay)", recorder.dir)
        alerts.started(len(board.markets), len(portfolio.open))
        if paused_before:
            log.warning("entries are PAUSED (you paused them before the restart); resume from the dashboard or /resume")
            engine.event("warn", "entries still paused from before the restart (Resume or /resume to continue)")
            alerts.send("⛔ Started with entries PAUSED, as you left them before the restart. "
                        "Open positions are still managed. Send /resume or press Resume to trade again.")
        elif start_paused:
            log.warning("entries are PAUSED at start (risk.start_paused = true in config.toml); resume from the "
                        "dashboard or /resume")
            engine.event("warn", "started paused (start_paused = true); Resume or /resume to trade")
            alerts.send("⛔ Started with entries PAUSED (that is the default: start_paused = true in config.toml). "
                        "Open positions are still managed. Send /resume or press Resume to trade.")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        publisher = SnapshotPublisher(cfg.publisher, engine, os.environ.get("DASHBOARD_PUSH_URL", ""),
                                      os.environ.get("DASHBOARD_PUSH_TOKEN", ""), session)
        if publisher.enabled:
            url_fp, token_fp = publisher.fingerprints()
            log.info("hosted dashboard: publishing a read-only snapshot every %.0fs (settings fingerprint: url %s, token %s)",
                     cfg.publisher.interval_s, url_fp, token_fp)
        coros = [feed.run(), engine.run(), dash.broadcast_loop(), alerts.run(), listener.run(), publisher.run(),
                 ticket.run(), *extra]
        if whales is not None:
            coros.append(whales.run())
        if trader is not None:
            coros.append(trader.run())
        if trader is not None and not trader.manual_only:
            log.warning("signal auto-trader: ON on the futures TESTNET (%s timeframe, $%g risk per trade, max %d open, "
                        "%dx leverage, confidence >= %d, entries %s)",
                        next((k for k, v in WINDOWS.items() if v == trader.window_s), f"{trader.window_s:g}s")
                        + ("" if trader.window_s == WINDOWS[args.signal_trader_window.lower()] else ", picked on the dashboard"),
                        trader.risk_usd, trader.max_open, trader.leverage,
                        trader.min_conf, "on" if trader.s["enabled"] else "OFF (switched off in the terminal)")
        if sampler is not None:
            coros.append(sampler.run())
        if swing is not None:
            coros.append(swing.run())
        if freq is not None:
            coros.append(freq.run())
            log.info("freqtrade: ON (%s runner) -> http://127.0.0.1:%d/trading | API %s | user_data %s | mode: %s",
                     cfg.freqtrade.runner, cfg.server.port, freq.trade.api.base_url, freq.user_data, freq.store.mode())
        if mirror:
            from .swing_mirror import run_all
            coros.append(run_all(mirror))
            log.info("swing mirror: copying new trades of the %s account%s to the futures TESTNET (each sized for $%.0f)",
                     "+".join(m.strategy for m in mirror), "s" if len(mirror) > 1 else "", args.swing_mirror_fund)
            log.info("swing toolkit: paper accounts %s on %s candles for %d markets (journal %s); /setups /journal /swing",
                     "+".join(swing.cfg.strategies), swing.cfg.interval, len(swing.symbols), args.swing_journal_db)
        tasks = [asyncio.create_task(c) for c in coros]
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        crashed = [t.exception() for t in done if t is not waiter and not t.cancelled() and t.exception()]
        for exc in crashed:
            log.error("task crashed: %r", exc)
        if crashed:
            alerts.send(f"🚨 arbbot CRASHED [{label}]: {type(crashed[0]).__name__}", priority=True)
        alerts.stopped(list(portfolio.open))
        await alerts.flush(3)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if recorder is not None:
            recorder.close()
        await runner.cleanup()
        if portfolio.open:
            log.warning("shutting down with OPEN positions (saved; they resume on next start): %s",
                        ", ".join(portfolio.open))
            if live:
                log.warning("LIVE positions remain on the exchange. Close them manually if you will not restart the bot.")
        if crashed:
            sys.exit(1)  # non-zero so launchd (KeepAlive.SuccessfulExit=false) restarts the bot


def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
