"""Step 4 study: stops and targets. Can any exit turn a no-edge entry into a profitable trade, after costs?

    python research/step4_exits.py [--final]

Entries: the two best step-3 triggers (volume thrust, break of structure), the RSI cross, and RANDOM entries as the
baseline. Exits: 5 stops x 6 target/management styles x 2 time limits (swingkit/exits.py), all with fees, slippage and
funding. The winner of each entry set is chosen on the DESIGN period only, and only then looked at on the CHECK period
and under stress: doubled slippage, entry one candle late, and (the best case) limit-order targets at the maker fee.
Time split 60% design / 20% check / 20% final (hidden unless --final).
"""
import argparse
import math
import random
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swingkit import exits as X  # noqa: E402
from swingkit import triggers as T  # noqa: E402
from swingkit.data import cache_path, load_csv, to_series  # noqa: E402
from swingkit.indicators import atr  # noqa: E402

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
SETS = ("random", "vol_thrust", "bos", "rsi_cross")
STOPS = {"atr1.0": ("atr", 1.0), "atr1.5": ("atr", 1.5), "atr2.0": ("atr", 2.0), "atr3.0": ("atr", 3.0), "swing": ("swing",)}
TARGETS = {"rr1.5": ("rr", 1.5), "rr2": ("rr", 2.0), "rr3": ("rr", 3.0), "half+rr3": ("partial", 3.0),
           "trail": ("trail",), "ema50": ("ema",)}
HOLDS = (48, 96)
BASE = X.XCosts()


def load(sym: str, iv: str):
    return to_series(load_csv(cache_path(Path("data/klines"), "futures", sym, iv)))


def prepare(seed: int = 5):
    rnd, units = random.Random(seed), []
    for c in COINS:
        s0 = load(c, "1h")
        for side in ("long", "short"):
            s = s0 if side == "long" else s0.mirrored()
            fires = T.fire_all(s)
            fires["random"] = [rnd.random() < 0.04 for _ in range(len(s))]
            units.append((s, atr(s.h, s.l, s.c, 14), X.context(s), {k: [i for i, x in enumerate(fires[k]) if x] for k in SETS}))
    return units


def trades_for(units, name: str, spec: X.Spec, costs: X.XCosts = BASE, delay: int = 0):
    out = []
    for s, a, ctx, sig in units:
        busy = -1
        for i in sig[name]:
            if i < busy:
                continue
            res = X.run(s, a, ctx, i, spec, costs, delay)
            if res is not None:
                busy = res.exit_i
                out.append((s.ts[res.entry_i], res.r, res.bps))
    return out


def stat(rows) -> tuple[int, float, float, float]:
    n = len(rows)
    if n < 30:
        return n, float("nan"), float("nan"), float("nan")
    r = [x[1] for x in rows]
    m, sd = st.mean(r), st.pstdev(r)
    return n, m, st.mean(x[2] for x in rows), (m / (sd / math.sqrt(n)) if sd else float("nan"))


def f(v: float, spec: str = "+.2f") -> str:
    return "–" if math.isnan(v) else format(v, spec)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true")
    args = ap.parse_args()
    units = prepare()
    lo = min(u[0].ts[0] for u in units)
    hi = max(u[0].ts[-1] for u in units)
    cut = lambda q: lo + (hi - lo) * q  # noqa: E731
    periods = {"design": (cut(0), cut(0.6)), "check": (cut(0.6), cut(0.8))}
    if args.final:
        periods["FINAL"] = (cut(0.8), hi + 1)
    inp = lambda rows, p: [x for x in rows if periods[p][0] <= x[0] < periods[p][1]]  # noqa: E731
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")  # noqa: E731

    out = ["# Step 4: exits after costs\n",
           f"Data: {', '.join(c[:-4] for c in COINS)}, USD-M perpetual 1h candles {day(lo)} to {day(hi)}, long and short. Entry at the next open; "
           "taker fee 5 bps + slippage 3 bps per fill, no slippage on a limit target, funding 1 bp per 8 h. R = net result in units of the initial risk. "
           "Stops: atrN = N x ATR(1h) below the entry; swing = under the last confirmed swing low (1-4 ATR). Targets: rrN = N x risk; half+rr3 = half at 1R, "
           "stop to break-even, rest at 3R; trail = stop follows each new confirmed swing low; ema50 = leave after a close under the 1h EMA50. "
           "random = entries at random candles (the baseline).\n"]
    book = {}
    for name in SETS:
        for hold in HOLDS:
            out.append(f"## {name} · time exit after {hold} h · mean R, design / check\n\n```\n{'':<8}" + "".join(f"{t:>14}" for t in TARGETS))
            for sn, stop in STOPS.items():
                cells = []
                for tn, tgt in TARGETS.items():
                    rows = trades_for(units, name, X.Spec(stop, tgt, hold))
                    book[(name, sn, tn, hold)] = rows
                    d, c = stat(inp(rows, "design")), stat(inp(rows, "check"))
                    cells.append(f"{f(d[1])}/{f(c[1])}")
                out.append(f"{sn:<8}" + "".join(f"{x:>14}" for x in cells))
            out.append("```\n")

    out.append("## The design-period winner of each entry set, then stressed on the check period\n\n"
               "Chosen by mean R on the design period only (at least 300 trades). 60 exits were tried per entry set, so the best of them is flattered by "
               "luck: the check period is the honest number.\n\n```\n"
               f"{'entry':<11}{'exit (stop, target, hold)':<28}{'design R':>9}{'check R':>9}{'  n':>6}{'win%':>6}"
               f"{'slip x2':>9}{'late 1':>8}{'maker tgt':>10}{'t':>6}")
    for name in SETS:
        cands = [(stat(inp(rows, "design")), key) for key, rows in book.items() if key[0] == name]
        cands = [(s_, k) for s_, k in cands if s_[0] >= 300 and not math.isnan(s_[1])]
        (dn, dm, _, _), key = max(cands, key=lambda x: x[0][1])
        spec = X.Spec(STOPS[key[1]], TARGETS[key[2]], key[3])
        check = inp(book[key], "check")
        n, cm, _, ct = stat(check)
        win = sum(x[1] > 0 for x in check) / n * 100 if n else float("nan")
        slow = stat(inp(trades_for(units, name, spec, X.XCosts(5, 5, 6, 1)), "check"))[1]
        late = stat(inp(trades_for(units, name, spec, BASE, 1), "check"))[1]
        maker = stat(inp(trades_for(units, name, spec, X.XCosts(5, 2, 3, 1)), "check"))[1]
        out.append(f"{name:<11}{key[1] + ', ' + key[2] + ', ' + str(key[3]) + 'h':<28}{f(dm):>9}{f(cm):>9}{n:>6}{f(win, '.0f'):>6}"
                   f"{f(slow):>9}{f(late):>8}{f(maker):>10}{f(ct, '.1f'):>6}")
    out.append("```\n")

    out.append("## The cost floor: what fees and slippage take out of a trade, by stop width\n\n"
               "Round trip = 2 x 5 bps fee + 2 x 3 bps slippage = 16 bps (+ funding). Cost as a share of the amount risked, using each coin's median 1h ATR.\n\n```\n"
               f"{'coin':<7}{'median ATR':>11}" + "".join(f"{'stop ' + str(n) + ' ATR':>14}" for n in (1.0, 1.5, 2.0, 3.0)))
    for c in COINS:
        s = load(c, "1h")
        a = atr(s.h, s.l, s.c, 14)
        bps = st.median(a[i] / s.c[i] * 1e4 for i in range(14, len(s)))
        out.append(f"{c[:-4]:<7}{bps:>8.0f} bps" + "".join(f"{16 / (n * bps) * 100:>13.0f}%" for n in (1.0, 1.5, 2.0, 3.0)))
    out.append("```")
    text = "\n".join(out)
    Path("research/results").mkdir(parents=True, exist_ok=True)
    Path("research/results/step4-exits.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
