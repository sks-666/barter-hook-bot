"""SQLite store for the Freqtrade subsystem: bot settings, exchange credentials, events, performance snapshots and
backtest summaries. Its own file (data/freqtrade.db, mode 0600) next to the arbitrage databases, same conventions as
arbbot/portfolio.py (autocommit, WAL, JSON blobs for the flexible parts).

Freqtrade keeps the trades themselves in its own databases (ft_user_data/tradesv3*.sqlite): nothing is duplicated
here, this store holds only what Freqtrade does not: who configured what, when, and the dashboard's history.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_BOT_CONFIG: dict = {
    "exchange": "binance",
    "trading_mode": "spot",        # "spot" | "futures"
    "margin_mode": "",             # futures: "isolated" (Freqtrade's supported mode on Binance / Bybit / OKX...)
    "stake_currency": "USDT",
    "stake_amount": 100.0,         # per trade, in stake currency; "unlimited" spreads the wallet over max_open_trades
    "max_open_trades": 3,
    "dry_run_wallet": 1000.0,      # the simulated wallet in dry-run mode
    "pairlist": "static",          # "static" = the pairs below | "volume" = the top `volume_assets` by 24h volume
    "pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "volume_assets": 20,
    "blacklist": [],
    "strategy": "SampleStrategy",
    "timeframe": "",               # empty = the strategy's own timeframe
    "process_throttle_secs": 5,
}
MODES = ("dry_run", "live")
MASK = "********"


class FreqStore:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)   # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS exchange_keys (exchange TEXT PRIMARY KEY, key TEXT NOT NULL, "
                        "secret TEXT NOT NULL, password TEXT NOT NULL DEFAULT '', uid TEXT NOT NULL DEFAULT '', "
                        "updated_at REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                        "level TEXT NOT NULL, source TEXT NOT NULL, msg TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS perf_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "ts REAL NOT NULL, mode TEXT NOT NULL, json TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS perf_ts ON perf_snapshots(ts)")
        self.db.execute("CREATE TABLE IF NOT EXISTS backtests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                        "strategy TEXT NOT NULL, timerange TEXT NOT NULL, timeframe TEXT NOT NULL, json TEXT NOT NULL)")
        if not existed:
            try:
                os.chmod(path, 0o600)          # it holds exchange credentials
            except OSError:
                pass

    # ---- settings -------------------------------------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def bot_config(self) -> dict:
        saved = self.get_setting("bot_config", {}) or {}
        cfg = {**DEFAULT_BOT_CONFIG, **{k: v for k, v in saved.items() if k in DEFAULT_BOT_CONFIG}}
        cfg["pairs"], cfg["blacklist"] = list(cfg["pairs"]), list(cfg["blacklist"])
        return cfg

    def save_bot_config(self, cfg: dict) -> None:
        self.set_setting("bot_config", {k: cfg[k] for k in DEFAULT_BOT_CONFIG if k in cfg})

    def mode(self) -> str:
        m = self.get_setting("mode", "dry_run")
        return m if m in MODES else "dry_run"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.set_setting("mode", mode)

    # ---- exchange credentials (never leave this process except into Freqtrade's config file) ---------------------
    def set_exchange_keys(self, exchange: str, key: str, secret: str, password: str = "", uid: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO exchange_keys(exchange, key, secret, password, uid, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (exchange, key, secret, password or "", uid or "", time.time()))

    def exchange_keys(self, exchange: str) -> dict | None:
        row = self.db.execute("SELECT key, secret, password, uid, updated_at FROM exchange_keys WHERE exchange = ?",
                              (exchange,)).fetchone()
        if not row:
            return None
        return {"key": row[0], "secret": row[1], "password": row[2], "uid": row[3], "updated_at": row[4]}

    def delete_exchange_keys(self, exchange: str) -> bool:
        return self.db.execute("DELETE FROM exchange_keys WHERE exchange = ?", (exchange,)).rowcount > 0

    def exchanges_with_keys(self) -> dict[str, dict]:
        """What the page may know: which exchanges have keys, since when, and the key's first characters."""
        out = {}
        for ex, key, ts in self.db.execute("SELECT exchange, key, updated_at FROM exchange_keys"):
            out[ex] = {"key_hint": (key[:4] + "…") if len(key) > 8 else MASK, "updated_at": ts}
        return out

    # ---- events ---------------------------------------------------------------------------------------------------
    def add_event(self, level: str, msg: str, source: str = "manager", ts: float | None = None) -> None:
        self.db.execute("INSERT INTO events(ts, level, source, msg) VALUES (?, ?, ?, ?)",
                        (ts or time.time(), level, source, msg[:1000]))
        self.db.execute("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 5000")   # keep the last 5000

    def events(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT ts, level, source, msg FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [{"ts": r[0], "lvl": r[1], "source": r[2], "msg": r[3]} for r in rows]

    # ---- performance history (for the chart when Freqtrade's own history is short or the bot is down) -------------
    def add_perf_snapshot(self, mode: str, data: dict, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO perf_snapshots(ts, mode, json) VALUES (?, ?, ?)",
                        (ts or time.time(), mode, json.dumps(data)))

    def perf_history(self, since_ts: float, mode: str | None = None, limit: int = 2000) -> list[dict]:
        q, args = "SELECT ts, mode, json FROM perf_snapshots WHERE ts >= ?", [since_ts]
        if mode:
            q, args = q + " AND mode = ?", args + [mode]
        rows = self.db.execute(q + " ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(json.loads(r[2]), ts=r[0], mode=r[1]) for r in reversed(rows)]

    def last_perf_snapshot(self, mode: str) -> dict | None:
        row = self.db.execute("SELECT ts, json FROM perf_snapshots WHERE mode = ? ORDER BY id DESC LIMIT 1",
                              (mode,)).fetchone()
        return dict(json.loads(row[1]), ts=row[0]) if row else None

    # ---- backtests --------------------------------------------------------------------------------------------------
    def add_backtest(self, strategy: str, timerange: str, timeframe: str, summary: dict) -> int:
        cur = self.db.execute("INSERT INTO backtests(ts, strategy, timerange, timeframe, json) VALUES (?, ?, ?, ?, ?)",
                              (time.time(), strategy, timerange, timeframe, json.dumps(summary)))
        return cur.lastrowid

    def backtests(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT id, ts, strategy, timerange, timeframe, json FROM backtests ORDER BY id DESC "
                               "LIMIT ?", (limit,))
        return [dict(json.loads(r[5]), id=r[0], ts=r[1], strategy=r[2], timerange=r[3], timeframe=r[4]) for r in rows]

    def close(self) -> None:
        self.db.close()
