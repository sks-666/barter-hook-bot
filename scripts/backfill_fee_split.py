"""Fill in the fee split (entry/exit, spot/futures) of closed trades saved before the bot recorded it.

The paper bot charges fixed rates per fill, so the split can be worked out from the total: entry and exit cost about the
same, and the spot and futures legs split the fee in the ratio of their rates. These are ESTIMATES: each row is marked
fee_estimated, and the dashboard shows it with a leading "≈". Rows that already have a split are never touched, so it is
safe to run twice. Stop the bot first (it keeps its recent trades in memory) and keep a backup.

    python scripts/backfill_fee_split.py data/paper-mainnet-real.db [--spot-bps 10] [--futures-bps 5]
"""
import argparse
import json
import sqlite3


def backfill(path: str, spot_bps: float, futures_bps: float) -> list[tuple]:
    db = sqlite3.connect(path, isolation_level=None)
    out = []
    for rid, sym, js in db.execute("SELECT id, symbol, json FROM trades ORDER BY id").fetchall():
        d = json.loads(js)
        total = float(d.get("fees_usd") or 0)
        if total <= 0 or any(d.get(k) for k in ("fee_entry_usd", "fee_exit_usd", "fee_spot_usd", "fee_fut_usd")):
            continue
        spot = total * spot_bps / (spot_bps + futures_bps)
        d.update(fee_entry_usd=total / 2, fee_exit_usd=total / 2, fee_spot_usd=spot, fee_fut_usd=total - spot,
                 fee_estimated=True)
        db.execute("UPDATE trades SET json = ? WHERE id = ?", (json.dumps(d), rid))
        out.append((sym, total, spot, total - spot))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db")
    ap.add_argument("--spot-bps", type=float, default=10.0)
    ap.add_argument("--futures-bps", type=float, default=5.0)
    a = ap.parse_args()
    rows = backfill(a.db, a.spot_bps, a.futures_bps)
    for sym, total, spot, fut in rows:
        print(f"{sym}: total ${total:.3f} -> about spot ${spot:.3f} + futures ${fut:.3f}, in/out ${total / 2:.3f} each")
    print(f"{len(rows)} trade(s) estimated")
