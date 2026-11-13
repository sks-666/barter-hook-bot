"""Lifecycle manager for the Freqtrade subsystem.

Two Freqtrade processes are managed, each addressed only through Freqtrade's REST API:

* `trade`      the trading bot (`freqtrade trade`): dry-run by default, live only after the arming rules below.
* `webserver`  the analysis server (`freqtrade webserver`): strategies, data download, backtesting, candle history.
               It never trades and gets no exchange credentials.

Both run as their own OS process (the local venv binary, or the official Docker image through docker compose), in
their own session, so they survive a dashboard restart: the manager adopts a running instance from its pid file.

Safety rules (the ones the dashboard cannot bypass):
  1. The generated config always says `dry_run: true` unless ALL of these hold: the saved mode is "live", the
     environment has FREQTRADE_LIVE_ACK=I_UNDERSTAND_THIS_TRADES_REAL_MONEY, and exchange keys for the configured
     exchange are stored. Otherwise the bot starts dry-run and an event says why.
  2. Switching to live is a separate request that must carry the typed phrase "TRADE REAL MONEY" and is refused while
     the bot runs; it goes back to dry-run on every dashboard start unless a live bot is already running (adopted).
  3. Dry-run and live trades live in different Freqtrade databases (tradesv3.dryrun.sqlite / tradesv3.sqlite).
  4. Exchange secrets go from the store into Freqtrade's config file (mode 0600) and nowhere else: never into the
     page, the API answers, the events or the logs. force_entry_enable stays false.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import FreqtradeApiError, FreqtradeClient, FreqtradeError, FreqtradeUnavailable
from .store import DEFAULT_BOT_CONFIG, MODES, FreqStore

log = logging.getLogger("freqkit")

LIVE_ACK_VAR = "FREQTRADE_LIVE_ACK"
LIVE_ACK = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"
LIVE_CONFIRM = "TRADE REAL MONEY"
TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"]
TRADING_MODES = ("spot", "futures")
PAIRLISTS = ("static", "volume")
PAIR_RE = re.compile(r"^[A-Z0-9.]{1,15}/[A-Z0-9]{2,10}(:[A-Z0-9]{2,10})?$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
EXCHANGE_RE = re.compile(r"^[a-z0-9]{2,30}$")
TIMERANGE_RE = re.compile(r"^(\d{8})?-(\d{8})?$")
KINDS = ("trade", "webserver")
CONTAINER_USER_DATA = "/freqtrade/user_data"   # where the official image mounts user_data
SECRET_ENV = re.compile(r"(SECRET|TOKEN|PASSWORD|API_KEY|_KEY$)", re.I)   # never inherited by the child process


# ---------------------------------------------------------------------------------------------------------------------
# settings validation and Freqtrade config generation
# ---------------------------------------------------------------------------------------------------------------------
def normalise_pair(p: str, trading_mode: str, stake: str) -> str:
    p = str(p).strip().upper().replace(" ", "")
    if trading_mode == "futures" and ":" not in p and "/" in p:
        p = f"{p}:{stake}"              # Freqtrade's futures pair notation: BTC/USDT:USDT
    if trading_mode == "spot" and ":" in p:
        p = p.split(":", 1)[0]
    if not PAIR_RE.match(p):
        raise ValueError(f"pair {p!r} must look like BTC/USDT (futures: BTC/USDT:USDT)")
    return p


def validate_bot_config(raw: dict, current: dict | None = None) -> dict:
    """The dashboard's settings form, checked field by field. Unknown keys are refused, like config.toml."""
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    unknown = set(raw) - set(DEFAULT_BOT_CONFIG)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    cfg = {**(current or DEFAULT_BOT_CONFIG), **raw}
    out: dict[str, Any] = {}
    ex = str(cfg["exchange"]).strip().lower()
    if not EXCHANGE_RE.match(ex):
        raise ValueError("exchange must be a lowercase ccxt id such as binance, bybit, okx or kraken")
    out["exchange"] = ex
    if cfg["trading_mode"] not in TRADING_MODES:
        raise ValueError("trading_mode must be spot or futures")
    out["trading_mode"] = cfg["trading_mode"]
    out["margin_mode"] = "isolated" if out["trading_mode"] == "futures" else ""
    stake = str(cfg["stake_currency"]).strip().upper()
    if not re.match(r"^[A-Z0-9]{2,10}$", stake):
        raise ValueError("stake_currency must be a currency code such as USDT")
    out["stake_currency"] = stake
    sa = cfg["stake_amount"]
    if isinstance(sa, str) and sa.strip().lower() == "unlimited":
        out["stake_amount"] = "unlimited"
    else:
        try:
            sa = float(sa)
        except (TypeError, ValueError):
            raise ValueError("stake_amount must be a number or 'unlimited'")
        if not 0 < sa <= 1e9:
            raise ValueError("stake_amount must be above 0")
        out["stake_amount"] = sa
    try:
        mot = int(cfg["max_open_trades"])
    except (TypeError, ValueError):
        raise ValueError("max_open_trades must be a whole number (-1 = unlimited)")
    if mot == 0 or mot < -1 or mot > 500:
        raise ValueError("max_open_trades must be between 1 and 500, or -1 for unlimited")
    out["max_open_trades"] = mot
    try:
        wallet = float(cfg["dry_run_wallet"])
    except (TypeError, ValueError):
        raise ValueError("dry_run_wallet must be a number")
    if not 1 <= wallet <= 1e12:
        raise ValueError("dry_run_wallet must be at least 1")
    out["dry_run_wallet"] = wallet
    if cfg["pairlist"] not in PAIRLISTS:
        raise ValueError("pairlist must be static or volume")
    out["pairlist"] = cfg["pairlist"]
    pairs = cfg["pairs"]
    if isinstance(pairs, str):
        pairs = re.split(r"[\s,;]+", pairs)
    if not isinstance(pairs, list):
        raise ValueError("pairs must be a list")
    seen: list[str] = []
    for p in pairs:
        if not str(p).strip():
            continue
        n = normalise_pair(p, out["trading_mode"], stake)
        if n not in seen:
            seen.append(n)
    if out["pairlist"] == "static" and not seen:
        raise ValueError("a static pairlist needs at least one pair")
    if len(seen) > 300:
        raise ValueError("at most 300 pairs")
    for p in seen:
        if not p.split(":")[0].endswith("/" + stake):
            raise ValueError(f"pair {p} is not quoted in the stake currency {stake}")
    out["pairs"] = seen
    try:
        va = int(cfg["volume_assets"])
    except (TypeError, ValueError):
        raise ValueError("volume_assets must be a whole number")
    if not 1 <= va <= 300:
        raise ValueError("volume_assets must be between 1 and 300")
    out["volume_assets"] = va
    bl = cfg["blacklist"]
    if isinstance(bl, str):
        bl = re.split(r"[\s,;]+", bl)
    if not isinstance(bl, list):
        raise ValueError("blacklist must be a list")
    out["blacklist"] = []
    for p in bl:
        p = str(p).strip().upper()
        if not p:
            continue
        if not re.match(r"^[A-Z0-9.*]{1,15}/[A-Z0-9*]{1,10}(:[A-Z0-9*]{1,10})?$", p):   # wildcards allowed: BNB/.*
            raise ValueError(f"blacklist entry {p!r} must look like DOGE/USDT or BNB/.*")
        out["blacklist"].append(p)
    strat = str(cfg["strategy"]).strip()
    if not NAME_RE.match(strat):
        raise ValueError("strategy must be a class name such as SampleStrategy")
    out["strategy"] = strat
    tf = str(cfg["timeframe"]).strip()
    if tf and tf not in TIMEFRAMES:
        raise ValueError(f"timeframe must be empty (the strategy's own) or one of {' '.join(TIMEFRAMES)}")
    out["timeframe"] = tf
    try:
        thr = int(cfg["process_throttle_secs"])
    except (TypeError, ValueError):
        raise ValueError("process_throttle_secs must be a whole number")
    if not 1 <= thr <= 300:
        raise ValueError("process_throttle_secs must be between 1 and 300")
    out["process_throttle_secs"] = thr
    return out


def build_freqtrade_config(bot: dict, *, api: dict, listen_ip: str, listen_port: int, dry_run: bool,
                           keys: dict | None, db_url: str | None, pairs: list[str] | None = None,
                           static_only: bool = False, bot_name: str = "arbbot-freqtrade") -> dict:
    """Freqtrade's config.json from the dashboard's settings (keys follow docs/configuration.md)."""
    if keys and dry_run:
        keys = None                     # a dry-run bot never needs the secrets, so it never sees them
    pairs = pairs if pairs is not None else bot["pairs"]
    if bot["pairlist"] == "volume" and not static_only:
        pairlists = [{"method": "VolumePairList", "number_assets": bot["volume_assets"], "sort_key": "quoteVolume",
                      "min_value": 0, "refresh_period": 1800}]
    else:
        pairlists = [{"method": "StaticPairList"}]
    cfg: dict[str, Any] = {
        "$schema": "https://schema.freqtrade.io/schema.json",
        "bot_name": bot_name,
        "strategy": bot["strategy"],
        "max_open_trades": bot["max_open_trades"],
        "stake_currency": bot["stake_currency"],
        "stake_amount": bot["stake_amount"],
        "tradable_balance_ratio": 0.99,
        "fiat_display_currency": "USD",
        "dry_run": bool(dry_run),
        "dry_run_wallet": bot["dry_run_wallet"],
        "cancel_open_orders_on_exit": False,
        "trading_mode": bot["trading_mode"],
        "margin_mode": bot["margin_mode"],
        "unfilledtimeout": {"entry": 10, "exit": 10, "exit_timeout_count": 0, "unit": "minutes"},
        "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1,
                          "price_last_balance": 0.0, "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1}},
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "exchange": {
            "name": bot["exchange"],
            "key": (keys or {}).get("key", ""),
            "secret": (keys or {}).get("secret", ""),
            "ccxt_config": {},
            "ccxt_async_config": {},
            "pair_whitelist": list(pairs),
            "pair_blacklist": list(bot["blacklist"]),
        },
        "pairlists": pairlists,
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "api_server": {
            "enabled": True,
            "listen_ip_address": listen_ip,
            "listen_port": int(listen_port),
            "verbosity": "error",
            "enable_openapi": False,
            "jwt_secret_key": api["jwt_secret_key"],
            "ws_token": api["ws_token"],
            "CORS_origins": [],
            "username": api["username"],
            "password": api["password"],
        },
        "initial_state": "running",
        "force_entry_enable": False,
        "internals": {"process_throttle_secs": bot["process_throttle_secs"]},
    }
    if keys and keys.get("password"):
        cfg["exchange"]["password"] = keys["password"]
    if keys and keys.get("uid"):
        cfg["exchange"]["uid"] = keys["uid"]
    if bot["timeframe"]:
        cfg["timeframe"] = bot["timeframe"]
    if db_url:
        cfg["db_url"] = db_url
    return cfg


def load_or_create_api_credentials(path: Path, environ=None) -> dict:
    """Freqtrade's API login, generated once (mode 0600). FREQTRADE_API_USERNAME / FREQTRADE_API_PASSWORD in the
    environment override the generated pair (for a Freqtrade you configured yourself)."""
    environ = os.environ if environ is None else environ
    creds: dict = {}
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    changed = False
    for k, make in (("username", lambda: "arbbot"), ("password", lambda: secrets.token_urlsafe(24)),
                    ("jwt_secret_key", lambda: secrets.token_hex(32)), ("ws_token", lambda: secrets.token_urlsafe(25))):
        if not isinstance(creds.get(k), str) or not creds[k]:
            creds[k], changed = make(), True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
    if environ.get("FREQTRADE_API_USERNAME") and environ.get("FREQTRADE_API_PASSWORD"):
        creds = dict(creds, username=environ["FREQTRADE_API_USERNAME"], password=environ["FREQTRADE_API_PASSWORD"])
    return creds


def write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def tail_lines(path: Path, limit: int = 200, max_bytes: int = 256_000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]               # the first line is most likely cut
    return lines[-limit:]


# ---------------------------------------------------------------------------------------------------------------------
# runners: how a Freqtrade process is started, watched and stopped
# ---------------------------------------------------------------------------------------------------------------------
class LocalRunner:
    """The freqtrade binary of a separate venv (pip install freqtrade), started in its own session."""
    name = "local"

    def __init__(self, executable: Path, user_data: Path):
        self.executable, self.user_data = Path(executable), Path(user_data)

    def check(self) -> str | None:
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            return (f"Freqtrade is not installed: {self.executable} is missing. Install it with "
                    f"`python3 -m venv .venv-freqtrade && .venv-freqtrade/bin/pip install freqtrade` "
                    f"(see docs/FREQTRADE.md), or set freqtrade.runner = \"docker\" in config.toml.")
        return None

    def command(self, kind: str, config_path: Path) -> list[str]:
        logfile = self.user_data / "logs" / f"freqtrade-{kind}.log"
        return [str(self.executable), kind, "--userdir", str(self.user_data), "--config", str(config_path),
                "--logfile", str(logfile)]

    async def spawn(self, kind: str, config_path: Path, out_path: Path) -> dict:
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k) or k.startswith("FREQTRADE__")}
        env["PYTHONUNBUFFERED"] = "1"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "ab") as out:
            out.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {shlex.join(self.command(kind, config_path))}\n"
                      .encode())
            proc = subprocess.Popen(self.command(kind, config_path), cwd=str(self.user_data), env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        return {"pid": proc.pid, "popen": proc}

    async def alive(self, handle: dict) -> bool:
        popen = handle.get("popen")
        if popen is not None:
            return popen.poll() is None
        pid = handle.get("pid")
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:                                 # Linux: make sure the pid was not recycled by an unrelated program
            cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
            return b"freqtrade" in cmd
        except OSError:
            return True

    async def exit_code(self, handle: dict) -> int | None:
        popen = handle.get("popen")
        return popen.poll() if popen is not None else None

    async def terminate(self, handle: dict, hard: bool = False) -> None:
        pid = handle.get("pid")
        if not pid:
            return
        try:
            os.kill(int(pid), signal.SIGKILL if hard else signal.SIGTERM)
        except ProcessLookupError:
            pass

    @staticmethod
    def handle_to_json(handle: dict) -> dict:
        return {"pid": handle.get("pid")}

    @staticmethod
    def handle_from_json(d: dict) -> dict:
        return {"pid": d.get("pid")}


class DockerRunner:
    """The official image through `docker compose -f deploy/docker-compose.freqtrade.yml` (services `freqtrade` and
    `freqtrade-webserver`). user_data is mounted into the container, so the same config files are used."""
    name = "docker"
    SERVICE = {"trade": "freqtrade", "webserver": "freqtrade-webserver"}

    def __init__(self, compose_file: Path, user_data: Path, api_port: int, webserver_port: int):
        self.compose_file, self.user_data = Path(compose_file), Path(user_data)
        self.env = dict(os.environ, FT_USER_DATA=str(self.user_data), FT_API_PORT=str(api_port),
                        FT_WEBSERVER_PORT=str(webserver_port))

    def check(self) -> str | None:
        if not self.compose_file.is_file():
            return f"compose file not found: {self.compose_file}"
        return None

    async def _compose(self, *args: str, timeout_s: float = 120.0) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", str(self.compose_file), *args, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return 1, "docker compose timed out"
        return proc.returncode or 0, out.decode("utf-8", "replace")

    async def spawn(self, kind: str, config_path: Path, out_path: Path) -> dict:
        svc = self.SERVICE[kind]
        code, out = await self._compose("up", "-d", "--force-recreate", svc)
        with open(out_path, "ab") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} docker compose up -d {svc}\n{out}\n".encode())
        if code != 0:
            raise RuntimeError(f"docker compose up failed: {out.strip()[-400:]}")
        return {"service": svc}

    async def alive(self, handle: dict) -> bool:
        svc = handle.get("service")
        if not svc:
            return False
        code, out = await self._compose("ps", "--format", "json", svc, timeout_s=30.0)
        if code != 0:
            return False
        for line in out.splitlines():      # one JSON object per line (compose v2.21+), or a JSON list
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            for item in (obj if isinstance(obj, list) else [obj]):
                if isinstance(item, dict) and str(item.get("State", "")).lower() == "running":
                    return True
        return False

    async def exit_code(self, handle: dict) -> int | None:
        return None

    async def terminate(self, handle: dict, hard: bool = False) -> None:
        svc = handle.get("service")
        if svc:
            await self._compose("kill" if hard else "stop", svc)

    @staticmethod
    def handle_to_json(handle: dict) -> dict:
        return {"service": handle.get("service")}

    @staticmethod
    def handle_from_json(d: dict) -> dict:
        return {"service": d.get("service")}


# ---------------------------------------------------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Proc:
    kind: str
    port: int
    api: FreqtradeClient
    pidfile: Path
    config_path: Path
    out_path: Path
    handle: dict | None = None
    state: str = "stopped"          # stopped | starting | running | error
    error: str = ""
    started_at: float = 0.0
    adopted: bool = False
    api_ok: bool = False
    api_fail: int = 0
    dry_run: bool | None = None     # what the running process was started with (trade only)
    version: str = ""
    extra: dict = field(default_factory=dict)   # trade: last show_config; webserver: its pair list

    def view(self) -> dict:
        return {"state": self.state, "error": self.error, "api_ok": self.api_ok, "adopted": self.adopted,
                "uptime_s": (time.time() - self.started_at) if self.started_at and self.state in ("starting", "running")
                else 0, "pid": (self.handle or {}).get("pid"), "service": (self.handle or {}).get("service"),
                "port": self.port, "version": self.version, "dry_run": self.dry_run}


class FreqtradeManager:
    def __init__(self, root: Path, cfg, store: FreqStore, session, environ=None):
        self.root, self.cfg, self.store, self.session = Path(root), cfg, store, session
        self.environ = os.environ if environ is None else environ
        self.user_data = (self.root / cfg.user_data).resolve()
        for sub in ("strategies", "data", "logs", "backtest_results"):
            (self.user_data / sub).mkdir(parents=True, exist_ok=True)
        self.creds = load_or_create_api_credentials(self.user_data / "api_credentials.json", self.environ)
        if cfg.runner == "docker":
            self.runner: LocalRunner | DockerRunner = DockerRunner(self.root / cfg.compose_file, self.user_data,
                                                                   cfg.api_port, cfg.webserver_port)
        else:
            self.runner = LocalRunner(self.root / cfg.executable, self.user_data)
        self.procs: dict[str, Proc] = {}
        for kind, port in (("trade", cfg.api_port), ("webserver", cfg.webserver_port)):
            api = FreqtradeClient(session, f"http://{cfg.api_host}:{port}", self.creds["username"],
                                  self.creds["password"])
            self.procs[kind] = Proc(kind, port, api, self.user_data / f"{kind}.pid",
                                    self.user_data / ("config.json" if kind == "trade" else "config.webserver.json"),
                                    self.user_data / "logs" / f"{kind}.out")
        self.trade, self.webserver = self.procs["trade"], self.procs["webserver"]
        self.cache: dict[str, Any] = {"status": [], "profit": None, "balance": None, "count": None, "health": None,
                                      "config": None, "strategies": [], "strategies_at": 0.0, "balance_at": 0.0,
                                      "snapshot_at": 0.0, "errors": {}}
        self.backtest: dict[str, Any] = {"state": "idle", "step": "", "progress": 0.0, "message": "", "request": None,
                                         "result": None, "started_at": 0.0, "finished_at": 0.0}
        self._bt_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self.started = False

    # ---- helpers ---------------------------------------------------------------------------------------------------
    def event(self, level: str, msg: str) -> None:
        self.store.add_event(level, msg)
        (log.warning if level in ("warn", "error") else log.info)("%s", msg)

    def live_ack_present(self) -> bool:
        return self.environ.get(LIVE_ACK_VAR, "") == LIVE_ACK

    def db_url(self, dry_run: bool) -> str:
        name = "tradesv3.dryrun.sqlite" if dry_run else "tradesv3.sqlite"
        base = CONTAINER_USER_DATA if self.cfg.runner == "docker" else str(self.user_data)
        return f"sqlite:///{base}/{name}" if self.cfg.runner == "docker" else f"sqlite:///{self.user_data / name}"

    def listen_ip(self) -> str:
        return "0.0.0.0" if self.cfg.runner == "docker" else self.cfg.api_host   # docker maps it to 127.0.0.1

    def effective_mode(self) -> tuple[bool, str]:
        """(dry_run, why). Live only when every arming rule holds."""
        bot = self.store.bot_config()
        if self.store.mode() != "live":
            return True, "dry-run mode (the default)"
        if not self.live_ack_present():
            return True, f"live was requested but {LIVE_ACK_VAR} is not set in .env: starting DRY-RUN"
        if not self.store.exchange_keys(bot["exchange"]):
            return True, f"live was requested but no API keys are stored for {bot['exchange']}: starting DRY-RUN"
        return False, "LIVE mode: real orders on " + bot["exchange"]

    # ---- lifecycle of the manager itself ---------------------------------------------------------------------------
    async def run(self) -> None:
        """Background task: adopt running instances, start the webserver, then poll."""
        self.started = True
        err = self.runner.check()
        if err:
            self.event("error", err)
        for proc in self.procs.values():
            await self._adopt(proc)
        if self.store.mode() == "live" and self.trade.state == "stopped":
            self.store.set_mode("dry_run")
            self.event("warn", "live mode was armed before the restart but no bot is running: back to DRY-RUN "
                               "(arm it again from the page if you really want live)")
        if self.cfg.autostart_webserver and self.webserver.state == "stopped" and not err:
            try:
                await self.start_webserver()
            except Exception as e:
                self.event("error", f"could not start the backtesting webserver: {type(e).__name__}: {e}")
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:                # the poll must never die
                log.exception("freqkit poll failed: %s", e)
            await asyncio.sleep(self.cfg.poll_s)

    async def _adopt(self, proc: Proc) -> None:
        try:
            saved = json.loads(proc.pidfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        handle = self.runner.handle_from_json(saved)
        if await self.runner.alive(handle):
            proc.handle, proc.adopted, proc.started_at = handle, True, float(saved.get("started_at") or time.time())
            proc.dry_run = saved.get("dry_run")
            proc.state = "running" if await proc.api.ping() else "starting"
            self.event("info", f"adopted the running Freqtrade {proc.kind} process"
                               f"{' (LIVE)' if proc.dry_run is False else ''}")
        else:
            proc.pidfile.unlink(missing_ok=True)

    def _save_pidfile(self, proc: Proc) -> None:
        write_private_json(proc.pidfile, dict(self.runner.handle_to_json(proc.handle or {}), started_at=proc.started_at,
                                              dry_run=proc.dry_run, runner=self.runner.name))

    # ---- polling -------------------------------------------------------------------------------------------------
    async def _poll(self) -> None:
        for proc in self.procs.values():
            await self._check(proc)
        if self.trade.state == "running" and self.trade.api_ok:
            await self._refresh_trade_cache()
        if self.webserver.state == "running" and self.webserver.api_ok \
                and time.time() - self.cache["strategies_at"] > 60:
            try:
                self.cache["strategies"] = sorted((await self.webserver.api.strategies()).get("strategies", []))
                self.cache["strategies_at"] = time.time()
            except FreqtradeError as e:
                self.cache["errors"]["strategies"] = str(e)[:200]

    async def _check(self, proc: Proc) -> None:
        if proc.state == "stopped":
            return
        alive = await self.runner.alive(proc.handle or {})
        if not alive:
            if proc.state in ("starting", "running"):
                code = await self.runner.exit_code(proc.handle or {})
                tail = [ln for ln in tail_lines(proc.out_path, 40) if "ERROR" in ln or "Exception" in ln
                        or "Error" in ln or ln.startswith("freqtrade.")]
                why = (tail[-1][-300:] if tail else "see the log")
                if code in (130, 143, -2, -15) and not tail:   # SIGINT / SIGTERM from outside: a stop, not a fault
                    proc.state, proc.api_ok, proc.error, proc.handle = "stopped", False, "", None
                    proc.pidfile.unlink(missing_ok=True)
                    self.event("info", f"Freqtrade {proc.kind} was stopped from outside the dashboard "
                                       f"(python -m freqkit stop, or a signal)")
                    if proc.kind == "trade":
                        self.cache.update(status=[], count=None, health=None, config=None)
                    return
                proc.state, proc.api_ok = "error", False
                proc.error = f"the {proc.kind} process exited" + (f" with code {code}" if code is not None else "") + \
                             f": {why}"
                proc.pidfile.unlink(missing_ok=True)
                self.event("error", f"Freqtrade {proc.kind}: {proc.error}")
                if proc.kind == "trade":
                    self.cache.update(status=[], count=None, health=None, config=None)
            return
        ok = await proc.api.ping()
        if proc.state == "error":           # e.g. docker restarted it
            proc.state, proc.error = "starting", ""
        if proc.state == "starting":
            if ok:
                proc.state, proc.api_ok, proc.api_fail = "running", True, 0
                try:
                    proc.version = str((await proc.api.version()).get("version", ""))
                except FreqtradeError:
                    pass
                self.event("info", f"Freqtrade {proc.kind} is up (API answering on port {proc.port}"
                                   f"{', version ' + proc.version if proc.version else ''})")
            elif time.time() - proc.started_at > self.cfg.start_timeout_s:
                proc.state, proc.error = "error", f"no answer from the API after {self.cfg.start_timeout_s:.0f}s"
                self.event("error", f"Freqtrade {proc.kind}: {proc.error}; stopping it")
                await self.runner.terminate(proc.handle or {})
            return
        if ok:
            proc.api_ok, proc.api_fail = True, 0
        else:
            proc.api_fail += 1
            if proc.api_fail >= 3 and proc.api_ok:
                proc.api_ok = False
                self.event("warn", f"Freqtrade {proc.kind} is running but its API stopped answering")

    async def _refresh_trade_cache(self) -> None:
        api, now, errs = self.trade.api, time.time(), self.cache["errors"]
        for name, coro in (("config", api.show_config()), ("health", api.health()), ("status", api.status()),
                           ("count", api.count()), ("profit", api.profit())):
            try:
                self.cache[name] = await coro
                errs.pop(name, None)
            except FreqtradeApiError as e:
                if name == "status" and e.status == 404:    # Freqtrade answers 404 when there is no open trade
                    self.cache["status"], _ = [], errs.pop(name, None)
                else:
                    errs[name] = e.detail
            except FreqtradeUnavailable as e:
                errs[name] = str(e)[:200]
                return
        cfg = self.cache.get("config") or {}
        if cfg:
            self.trade.dry_run = bool(cfg.get("dry_run"))
            if not self.trade.extra or self.trade.extra.get("state") != cfg.get("state"):
                if self.trade.extra:
                    self.event("info", f"bot state: {cfg.get('state')}")
            self.trade.extra = cfg
        if now - self.cache["balance_at"] > 30:
            try:
                self.cache["balance"] = await api.balance()
                self.cache["balance_at"] = now
                errs.pop("balance", None)
            except FreqtradeError as e:
                errs["balance"] = str(getattr(e, "detail", e))[:200]
        if now - self.cache["snapshot_at"] > 60 and self.cache.get("profit"):
            p, b = self.cache["profit"], self.cache.get("balance") or {}
            self.store.add_perf_snapshot("dry_run" if self.trade.dry_run else "live", {
                "profit_closed": p.get("profit_closed_coin", 0.0), "profit_all": p.get("profit_all_coin", 0.0),
                "closed": p.get("closed_trade_count", 0), "open": (p.get("trade_count", 0) - p.get("closed_trade_count", 0)),
                "winrate": p.get("winrate", 0.0), "balance": b.get("total_bot", b.get("total", 0.0)),
                "stake": b.get("stake", cfg.get("stake_currency", ""))})
            self.cache["snapshot_at"] = now

    # ---- the trade process -----------------------------------------------------------------------------------------
    async def start_trade(self) -> dict:
        async with self._lock:
            if self.trade.state in ("starting", "running"):
                raise ValueError("the trading bot is already running")
            err = self.runner.check()
            if err:
                raise ValueError(err)
            bot = self.store.bot_config()
            if self.cache["strategies"] and bot["strategy"] not in self.cache["strategies"]:
                raise ValueError(f"strategy {bot['strategy']} is not in {self.user_data / 'strategies'} "
                                 f"(available: {', '.join(self.cache['strategies']) or 'none'})")
            dry_run, why = self.effective_mode()
            keys = None if dry_run else self.store.exchange_keys(bot["exchange"])
            cfg = build_freqtrade_config(bot, api=self.creds, listen_ip=self.listen_ip(), listen_port=self.trade.port,
                                         dry_run=dry_run, keys=keys, db_url=self.db_url(dry_run))
            write_private_json(self.trade.config_path, cfg)
            self.trade.handle = await self.runner.spawn("trade", self.trade.config_path, self.trade.out_path)
            self.trade.state, self.trade.error, self.trade.started_at = "starting", "", time.time()
            self.trade.adopted, self.trade.api_ok, self.trade.api_fail, self.trade.dry_run = False, False, 0, dry_run
            self.trade.extra = {}
            self._save_pidfile(self.trade)
            self.event("warn" if not dry_run else "info",
                       f"starting Freqtrade trade: {bot['strategy']} on {bot['exchange']} {bot['trading_mode']}, "
                       f"{len(bot['pairs']) if bot['pairlist'] == 'static' else bot['volume_assets']} pairs, "
                       f"stake {bot['stake_amount']} {bot['stake_currency']} · {why}")
            return self.status()

    async def stop_trade(self) -> dict:
        async with self._lock:
            return await self._stop(self.trade)

    async def _stop(self, proc: Proc) -> dict:
        if proc.state == "stopped" or not proc.handle:
            proc.state, proc.error = "stopped", ""
            return self.status()
        self.event("info", f"stopping Freqtrade {proc.kind}…")
        await self.runner.terminate(proc.handle)
        for _ in range(60):                       # Freqtrade finishes its iteration and closes the database
            if not await self.runner.alive(proc.handle):
                break
            await asyncio.sleep(0.5)
        else:
            self.event("warn", f"Freqtrade {proc.kind} did not stop in 30s: killing it")
            await self.runner.terminate(proc.handle, hard=True)
            await asyncio.sleep(0.5)
        proc.state, proc.api_ok, proc.error, proc.handle, proc.adopted = "stopped", False, "", None, False
        proc.pidfile.unlink(missing_ok=True)
        if proc.kind == "trade":
            self.cache.update(status=[], count=None, health=None, config=None)
        self.event("info", f"Freqtrade {proc.kind} stopped")
        return self.status()

    async def restart_trade(self) -> dict:
        await self.stop_trade()
        return await self.start_trade()

    # ---- the webserver process -------------------------------------------------------------------------------------
    async def start_webserver(self, pairs: list[str] | None = None) -> dict:
        async with self._lock:
            if self.webserver.state in ("starting", "running"):
                raise ValueError("the webserver is already running")
            err = self.runner.check()
            if err:
                raise ValueError(err)
            bot = self.store.bot_config()
            pairs = pairs or bot["pairs"]
            cfg = build_freqtrade_config(bot, api=self.creds, listen_ip=self.listen_ip(),
                                         listen_port=self.webserver.port, dry_run=True, keys=None, db_url=None,
                                         pairs=pairs, static_only=True, bot_name="arbbot-freqtrade-webserver")
            write_private_json(self.webserver.config_path, cfg)
            self.webserver.handle = await self.runner.spawn("webserver", self.webserver.config_path,
                                                            self.webserver.out_path)
            self.webserver.state, self.webserver.error, self.webserver.started_at = "starting", "", time.time()
            self.webserver.adopted, self.webserver.api_ok, self.webserver.api_fail = False, False, 0
            self.webserver.extra = {"pairs": list(pairs), "exchange": bot["exchange"],
                                    "trading_mode": bot["trading_mode"]}
            self._save_pidfile(self.webserver)
            self.event("info", "starting the Freqtrade webserver (backtesting, data, strategies; it never trades)")
            return self.status()

    async def stop_webserver(self) -> dict:
        async with self._lock:
            return await self._stop(self.webserver)

    async def ensure_webserver(self, pairs: list[str] | None = None, timeout_s: float = 90.0) -> None:
        """Make sure the webserver is up with these pairs in its whitelist (restarting it if they changed)."""
        bot = self.store.bot_config()
        pairs = pairs or bot["pairs"]
        current = self.webserver.extra if self.webserver.state in ("starting", "running") else None
        if current is not None and (sorted(current.get("pairs", [])) != sorted(pairs)
                                    or current.get("exchange") != bot["exchange"]
                                    or current.get("trading_mode") != bot["trading_mode"]):
            await self.stop_webserver()
            current = None
        if current is None and self.webserver.state not in ("starting", "running"):
            await self.start_webserver(pairs)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await self._check(self.webserver)
            if self.webserver.state == "running" and self.webserver.api_ok:
                return
            if self.webserver.state == "error":
                raise ValueError(f"the webserver failed: {self.webserver.error}")
            await asyncio.sleep(1.0)
        raise ValueError("the webserver did not come up in time")

    # ---- bot control through the API ---------------------------------------------------------------------------------
    async def bot_action(self, action: str) -> dict:
        if self.trade.state != "running":
            raise ValueError("the Freqtrade process is not running: launch it first")
        calls = {"start": self.trade.api.start, "stop": self.trade.api.stop, "pause": self.trade.api.pause,
                 "reload": self.trade.api.reload_config}
        if action not in calls:
            raise ValueError("action must be start, stop, pause or reload")
        res = await calls[action]()
        self.event("info", f"bot {action}: {res.get('status', res)}")
        self.cache["snapshot_at"] = 0.0
        return res

    async def forceexit(self, trade_id, ordertype: str | None = None) -> dict:
        if self.trade.state != "running":
            raise ValueError("the Freqtrade process is not running")
        tid = str(trade_id).strip()
        if tid != "all" and not tid.isdigit():
            raise ValueError("tradeid must be a trade number or 'all'")
        if ordertype not in (None, "", "market", "limit"):
            raise ValueError("ordertype must be market or limit")
        res = await self.trade.api.forceexit(tid, ordertype or None)
        self.event("warn", f"force exit of trade {tid} requested ({'LIVE' if self.trade.dry_run is False else 'dry-run'})"
                           f": {res.get('result', res)}")
        return res

    # ---- settings -------------------------------------------------------------------------------------------------------
    def save_config(self, raw: dict) -> dict:
        cfg = validate_bot_config(raw, self.store.bot_config())
        self.store.save_bot_config(cfg)
        self.event("info", f"settings saved: {cfg['strategy']} on {cfg['exchange']} {cfg['trading_mode']}, "
                           f"stake {cfg['stake_amount']} {cfg['stake_currency']}, max {cfg['max_open_trades']} trades"
                   + (" (the running bot keeps its old settings until it is relaunched)"
                      if self.trade.state in ("starting", "running") else ""))
        return cfg

    def save_exchange_keys(self, exchange: str, key: str, secret: str, password: str = "", uid: str = "") -> None:
        exchange = str(exchange).strip().lower()
        if not EXCHANGE_RE.match(exchange):
            raise ValueError("exchange must be a lowercase ccxt id such as binance")
        key, secret = str(key).strip(), str(secret).strip()
        if len(key) < 8 or len(secret) < 8:
            raise ValueError("key and secret look too short")
        if any(c.isspace() for c in key + secret):
            raise ValueError("key and secret must not contain spaces")
        self.store.set_exchange_keys(exchange, key, secret, str(password or "").strip(), str(uid or "").strip())
        self.event("info", f"API keys for {exchange} saved (they are only ever written into Freqtrade's config file)")

    def delete_exchange_keys(self, exchange: str) -> bool:
        ok = self.store.delete_exchange_keys(str(exchange).strip().lower())
        if ok:
            self.event("info", f"API keys for {exchange} deleted")
            if self.store.mode() == "live":
                self.store.set_mode("dry_run")
                self.event("warn", "live mode disarmed: its exchange keys were deleted")
        return ok

    def set_mode(self, mode: str, confirm: str = "") -> str:
        if mode not in MODES:
            raise ValueError("mode must be dry_run or live")
        if self.trade.state in ("starting", "running"):
            raise ValueError("stop the trading bot before changing its mode (dry-run and live use separate databases)")
        if mode == "live":
            if confirm != LIVE_CONFIRM:
                raise ValueError(f"to arm live trading type exactly: {LIVE_CONFIRM}")
            if not self.live_ack_present():
                raise ValueError(f"live trading also needs {LIVE_ACK_VAR}={LIVE_ACK} in .env (then restart the dashboard)")
            bot = self.store.bot_config()
            if not self.store.exchange_keys(bot["exchange"]):
                raise ValueError(f"no API keys stored for {bot['exchange']}: add them first")
            self.store.set_mode("live")
            self.event("warn", f"LIVE mode ARMED for {bot['exchange']}: the next launch sends REAL orders")
        else:
            self.store.set_mode("dry_run")
            self.event("info", "dry-run mode set (simulated fills, no real orders)")
        return self.store.mode()

    # ---- status for the page ------------------------------------------------------------------------------------------
    def status(self) -> dict:
        bot = self.store.bot_config()
        dry_run, why = self.effective_mode()
        cfg = self.cache.get("config") or {}
        return {
            "enabled": True, "runner": self.runner.name, "mode": self.store.mode(),
            "next_launch_dry_run": dry_run, "mode_note": why, "live_ack_env": self.live_ack_present(),
            "live_confirm_phrase": LIVE_CONFIRM, "live_ack_var": LIVE_ACK_VAR,
            "engine": self.trade.view(), "webserver": self.webserver.view(),
            "bot": {"state": cfg.get("state"), "strategy": cfg.get("strategy"), "exchange": cfg.get("exchange"),
                    "trading_mode": cfg.get("trading_mode"), "timeframe": cfg.get("timeframe"),
                    "max_open_trades": cfg.get("max_open_trades"), "dry_run": cfg.get("dry_run"),
                    "stake_currency": cfg.get("stake_currency"), "stake_amount": cfg.get("stake_amount"),
                    "bot_name": cfg.get("bot_name"), "version": cfg.get("version"), "runmode": cfg.get("runmode"),
                    "dry_run_wallet": cfg.get("dry_run_wallet")} if cfg else None,
            "config": bot, "keys": self.store.exchanges_with_keys(), "strategies": self.cache["strategies"],
            "user_data": str(self.user_data), "api_url": self.trade.api.base_url, "errors": dict(self.cache["errors"]),
            "backtest": self.backtest_view(), "runner_error": self.runner.check(),
        }

    def overview(self) -> dict:
        """Everything the page's top half needs, from the cache (one request, never waits on Freqtrade)."""
        return dict(self.status(), health=self.cache.get("health"), profit=self.cache.get("profit"),
                    balance=self.cache.get("balance"), count=self.cache.get("count"),
                    open_trades=[trim_trade(t) for t in (self.cache.get("status") or [])],
                    events=self.store.events(40), now=time.time())

    def logs(self, kind: str = "trade", limit: int = 200) -> list[str]:
        if kind not in KINDS:
            raise ValueError("kind must be trade or webserver")
        return tail_lines(self.procs[kind].out_path, limit)

    # ---- backtesting (through the webserver) ----------------------------------------------------------------------------
    def backtest_view(self) -> dict:
        v = dict(self.backtest)
        v["running"] = self._bt_task is not None and not self._bt_task.done()
        return v

    def validate_backtest_request(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("expected a JSON object")
        bot = self.store.bot_config()
        strategy = str(raw.get("strategy") or bot["strategy"]).strip()
        if not NAME_RE.match(strategy):
            raise ValueError("strategy must be a class name")
        tf = str(raw.get("timeframe") or "").strip()
        if tf and tf not in TIMEFRAMES:
            raise ValueError("timeframe must be one of " + " ".join(TIMEFRAMES))
        timerange = str(raw.get("timerange") or "").strip()
        if not TIMERANGE_RE.match(timerange) or timerange == "-":
            raise ValueError("timerange must look like 20250101-20250301 (start-end, UTC days; one side may be empty)")
        pairs_raw = raw.get("pairs") or bot["pairs"]
        if isinstance(pairs_raw, str):
            pairs_raw = re.split(r"[\s,;]+", pairs_raw)
        pairs: list[str] = []
        for p in pairs_raw:
            if str(p).strip():
                n = normalise_pair(p, bot["trading_mode"], bot["stake_currency"])
                if n not in pairs:
                    pairs.append(n)
        if not pairs:
            raise ValueError("choose at least one pair")
        if len(pairs) > 50:
            raise ValueError("at most 50 pairs per backtest")
        mot = raw.get("max_open_trades", bot["max_open_trades"])
        try:
            mot = int(mot)
        except (TypeError, ValueError):
            raise ValueError("max_open_trades must be a whole number")
        if mot == 0 or mot < -1:
            raise ValueError("max_open_trades must be at least 1 (or -1 = unlimited)")
        sa = raw.get("stake_amount", bot["stake_amount"])
        if not (isinstance(sa, str) and sa.lower() == "unlimited"):
            try:
                sa = float(sa)
            except (TypeError, ValueError):
                raise ValueError("stake_amount must be a number or 'unlimited'")
            if sa <= 0:
                raise ValueError("stake_amount must be above 0")
        else:
            sa = "unlimited"
        try:
            wallet = float(raw.get("dry_run_wallet", bot["dry_run_wallet"]))
        except (TypeError, ValueError):
            raise ValueError("dry_run_wallet must be a number")
        if wallet < 1:
            raise ValueError("dry_run_wallet must be at least 1")
        return {"strategy": strategy, "timeframe": tf, "timerange": timerange, "pairs": pairs, "max_open_trades": mot,
                "stake_amount": sa, "dry_run_wallet": wallet, "download": bool(raw.get("download", True)),
                "enable_protections": bool(raw.get("enable_protections", False))}

    async def start_backtest(self, raw: dict) -> dict:
        if self._bt_task is not None and not self._bt_task.done():
            raise ValueError("a backtest is already running")
        req = self.validate_backtest_request(raw)
        self.backtest = {"state": "starting", "step": "webserver", "progress": 0.0, "message": "starting the webserver",
                         "request": req, "result": None, "started_at": time.time(), "finished_at": 0.0}
        self._bt_task = asyncio.create_task(self._run_backtest(req))
        return self.backtest_view()

    async def abort_backtest(self) -> dict:
        if self._bt_task is None or self._bt_task.done():
            raise ValueError("no backtest is running")
        self.backtest.update(state="aborting", message="aborting…")
        try:
            await self.webserver.api.backtest_abort()
        except FreqtradeError:
            pass
        self._bt_task.cancel()
        return self.backtest_view()

    async def _run_backtest(self, req: dict) -> None:
        bt, api = self.backtest, self.webserver.api
        try:
            await self.ensure_webserver(req["pairs"])
            tf = req["timeframe"]
            if not tf:
                bt.update(step="strategy", message=f"reading {req['strategy']}")
                tf = (await api.strategy(req["strategy"])).get("timeframe") or "5m"
            if req["download"]:
                bot = self.store.bot_config()
                bt.update(state="downloading", step="download", message=f"downloading {tf} candles for "
                          f"{len(req['pairs'])} pair(s) from {bot['exchange']}", progress=0.0)
                job = await api.download_data(req["pairs"], [tf], timerange=req["timerange"].rstrip("-") + "-"
                                              if req["timerange"].endswith("-") else req["timerange"],
                                              exchange=bot["exchange"], trading_mode=bot["trading_mode"],
                                              margin_mode=bot["margin_mode"] or None)
                job_id = job.get("job_id")
                while True:
                    await asyncio.sleep(1.5)
                    st = await api.background_job(job_id)
                    bt["progress"] = float(st.get("progress") or 0.0)
                    if not st.get("running"):
                        if st.get("status") != "success":
                            raise ValueError(f"data download failed: {st.get('error') or st.get('status')}")
                        break
                # Freqtrade reports the job as a success even when the exchange refused some pairs (it logs and goes
                # on), so check what is really on disk before backtesting on nothing.
                avail = await api.available_pairs(timeframe=tf, stake_currency=bot["stake_currency"])
                missing = [p for p in req["pairs"] if p not in (avail.get("pairs") or [])]
                if missing:
                    raise ValueError(f"no {tf} candles for {', '.join(missing)} after the download: the exchange "
                                     f"refused the request (see the webserver log; some exchanges serve only a few "
                                     f"weeks of small candles, so try a shorter time range or a larger timeframe)")
            bt.update(state="backtesting", step="backtest", message="backtesting…", progress=0.0)
            payload = {"strategy": req["strategy"], "timeframe": tf, "timerange": req["timerange"],
                       "max_open_trades": req["max_open_trades"], "stake_amount": req["stake_amount"],
                       "enable_protections": req["enable_protections"], "dry_run_wallet": req["dry_run_wallet"],
                       "backtest_cache": "none"}
            try:
                await api.backtest_reset()
            except FreqtradeError:
                pass
            await api.backtest_start(payload)
            while True:
                await asyncio.sleep(1.5)
                st = await api.backtest_status()
                bt["progress"] = float(st.get("progress") or 0.0)
                bt["message"] = f"{st.get('step') or 'running'} · {st.get('trade_count') or 0} trades so far"
                if not st.get("running"):
                    if st.get("status") != "ended":
                        raise ValueError(st.get("status_msg") or "backtest failed")
                    result = summarise_backtest(st.get("backtest_result") or {}, req["strategy"])
                    break
            bt.update(state="done", step="done", message="finished", progress=1.0, result=result,
                      finished_at=time.time())
            self.store.add_backtest(req["strategy"], req["timerange"], tf, {k: v for k, v in result.items()
                                                                             if k not in ("trades", "per_pair")})
            self.event("info", f"backtest {req['strategy']} {req['timerange']} {tf}: {result.get('total_trades', 0)} "
                               f"trades, {100 * (result.get('profit_total') or 0):+.2f}%")
        except asyncio.CancelledError:
            bt.update(state="aborted", message="aborted", finished_at=time.time())
            raise
        except FreqtradeError as e:
            bt.update(state="error", message=f"Freqtrade: {getattr(e, 'detail', e)}", finished_at=time.time())
            self.event("error", f"backtest failed: {getattr(e, 'detail', e)}")
        except Exception as e:
            bt.update(state="error", message=f"{type(e).__name__}: {str(e)[:300]}", finished_at=time.time())
            self.event("error", f"backtest failed: {type(e).__name__}: {str(e)[:200]}")

    async def backtest_history(self) -> list:
        if self.webserver.state != "running":
            return []
        return await self.webserver.api.backtest_history()

    async def backtest_result(self, filename: str, strategy: str) -> dict:
        if not re.match(r"^[A-Za-z0-9_.\-]{1,200}$", filename) or not NAME_RE.match(strategy):
            raise ValueError("bad filename or strategy")
        if self.webserver.state != "running":
            raise ValueError("the webserver is not running")
        res = await self.webserver.api.backtest_history_result(filename, strategy)
        return summarise_backtest(res.get("backtest_result") or {}, strategy)


# ---------------------------------------------------------------------------------------------------------------------
# shaping Freqtrade's answers for the page
# ---------------------------------------------------------------------------------------------------------------------
TRADE_FIELDS = ("trade_id", "pair", "base_currency", "quote_currency", "is_open", "is_short", "exchange", "strategy",
                "timeframe", "amount", "stake_amount", "leverage", "open_date", "open_timestamp", "open_rate",
                "close_date", "close_timestamp", "close_rate", "current_rate", "profit_ratio", "profit_pct",
                "profit_abs", "total_profit_abs", "total_profit_ratio", "realized_profit", "exit_reason", "enter_tag",
                "stop_loss_abs", "stop_loss_pct", "stoploss_current_dist_ratio", "trade_duration", "trade_duration_s",
                "fee_open_cost", "fee_close_cost", "has_open_orders", "open_order_id", "funding_fees")


def trim_trade(t: dict) -> dict:
    return {k: t.get(k) for k in TRADE_FIELDS if k in t}


def summarise_backtest(result: dict, strategy: str) -> dict:
    """The parts of Freqtrade's backtest result the page shows (its full result runs to megabytes)."""
    strat = (result.get("strategy") or {}).get(strategy) or {}
    if not strat:
        comp = result.get("strategy_comparison") or []
        strat = (result.get("strategy") or {}).get(comp[0]["key"]) if comp else {}
        strat = strat or {}
    keys = ("total_trades", "trade_count_long", "trade_count_short", "profit_mean", "profit_median", "profit_total",
            "profit_total_abs", "wins", "draws", "losses", "winrate", "cagr", "expectancy", "expectancy_ratio",
            "sortino", "sharpe", "calmar", "profit_factor", "max_drawdown_account", "max_drawdown_abs",
            "max_relative_drawdown", "backtest_start", "backtest_end", "backtest_days", "trades_per_day",
            "market_change", "stake_currency", "starting_balance", "final_balance", "timeframe", "best_pair",
            "worst_pair", "holding_avg", "winner_holding_avg", "loser_holding_avg", "max_open_trades",
            "stoploss", "minimal_roi", "trailing_stop", "pairlist", "drawdown_start", "drawdown_end")
    out: dict[str, Any] = {k: strat.get(k) for k in keys if k in strat}
    out["strategy"] = strategy
    for k in ("best_pair", "worst_pair"):
        if isinstance(out.get(k), dict):
            out[k] = {kk: out[k].get(kk) for kk in ("key", "profit_total", "profit_total_abs", "trades")}
    for k in ("holding_avg", "winner_holding_avg", "loser_holding_avg"):
        if out.get(k) is not None and not isinstance(out[k], (str, int, float)):
            out[k] = str(out[k])
    per_pair = []
    for row in strat.get("results_per_pair") or []:
        per_pair.append({k: row.get(k) for k in ("key", "trades", "profit_mean", "profit_mean_pct", "profit_total",
                                                  "profit_total_abs", "profit_total_pct", "wins", "draws", "losses",
                                                  "winrate", "duration_avg")})
    out["per_pair"] = per_pair
    out["exit_reasons"] = [dict({k: r.get(k) for k in ("trades", "profit_total_abs", "profit_total", "wins", "draws",
                                                        "losses")}, exit_reason=r.get("exit_reason", r.get("key")))
                           for r in (strat.get("exit_reason_summary") or [])]
    out["daily_profit"] = strat.get("daily_profit") or []
    trades = []
    for t in (strat.get("trades") or [])[:1000]:
        trades.append({k: t.get(k) for k in ("pair", "is_short", "open_date", "close_date", "open_rate", "close_rate",
                                             "amount", "stake_amount", "profit_ratio", "profit_abs", "exit_reason",
                                             "enter_tag", "trade_duration", "open_timestamp", "close_timestamp")})
    out["trades"] = trades
    out["trades_truncated"] = len(strat.get("trades") or []) > 1000
    return out
