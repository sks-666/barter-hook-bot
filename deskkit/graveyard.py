"""The graveyard: every configuration ever tested, with its out-of-sample statistics.

Two jobs. It gives the deflated Sharpe ratio its number of trials N and the variance of the trials' Sharpe ratios (a retest
is a NEW trial and is counted, so the same idea tested ten times is ten trials), and it lets the desk notice an idea it has
already tested (same fingerprint) before it spends time on it again."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .stats import std


def fingerprint(**parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Graveyard:
    def __init__(self, path: Path | str, clock=time.time):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS trials (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            "rules_version TEXT, family TEXT NOT NULL, fingerprint TEXT NOT NULL, label TEXT, params TEXT, "
            "symbols TEXT, interval TEXT, market TEXT, sr REAL, n_obs INTEGER, skew REAL, kurt REAL, net_pct REAL, "
            "oos_trades INTEGER, outcome TEXT)")

    def add(self, *, rules_version: str, family: str, fp: str, label: str, params: dict, symbols: list[str],
            interval: str, market: str, sr: float, n_obs: int, skew: float, kurt: float, net_pct: float,
            oos_trades: int, outcome: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO trials (ts, rules_version, family, fingerprint, label, params, symbols, interval, market, sr, "
            "n_obs, skew, kurt, net_pct, oos_trades, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(self.clock() * 1000), rules_version, family, fp, label, json.dumps(params, sort_keys=True),
             ",".join(symbols), interval, market, sr, n_obs, skew, kurt, net_pct, oos_trades, outcome))
        return int(cur.lastrowid)

    def set_outcome(self, trial_id: int, outcome: str) -> None:
        self.db.execute("UPDATE trials SET outcome = ? WHERE id = ?", (outcome, trial_id))

    def trials(self, family: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM trials WHERE family = ? ORDER BY id", (family,))]

    def count_and_variance(self, family: str) -> tuple[int, float]:
        """N and the sample variance of the trials' per-period Sharpe ratios (0 until there are two trials)."""
        sr = [r["sr"] for r in self.trials(family)]
        return len(sr), std(sr) ** 2

    def seen(self, fp: str) -> int:
        """How many times this exact configuration was tested before."""
        return int(self.db.execute("SELECT COUNT(*) FROM trials WHERE fingerprint = ?", (fp,)).fetchone()[0])

    def families(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT family, COUNT(*) FROM trials GROUP BY family")}
