"""Step 3 study: which entry trigger works, after fees, and do the trend and zone filters help it?

    python research/step3_triggers.py [--final]

Six triggers (swingkit/triggers.py) on 1h candles, long and short, six coins, five years. Every signal is traded the same
way (next open, stop 1.5 ATR, target 2R, 48h time exit, taker fees + slippage + flat funding), so triggers differ only in
WHEN they enter. Each trigger is run alone and with the daily trend filter (T2 agrees with the side), the zone filter
(price at a 4h support/resistance zone) and both. The baseline is the same trades from RANDOM entries under the same filter.
One trade at a time per coin and side. Time split 60% design / 20% check / 20% final (hidden unless --final).
"""
import argparse
import math
import random
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swingkit import triggers as T  # noqa: E402
from swingkit import zones as Z  # noqa: E402
from swingkit.data import cache_path, load_csv, to_series  # noqa: E402
from swingkit.indicators import atr  # noqa: E402
from swingkit.mtf import BAR_MS, align  # noqa: E402
from swingkit.trend import t2  # noqa: E402

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
FILTERS = ("none", "trend", "zone", "both")
EXIT = dict(stop_atr=1.5, rr=2.0, max_hold=48)   # the common exit; --stop-atr/--rr/--hold change it
RANDOM_P = 0.05                      # share of candles a random entry may start on


def load(sym: str, iv: str):
    return to_series(load_csv(cache_path(Path("data/klines"), "futures", sym, iv)))


def gather(seed: int = 3):
    """trades[(trigger, filter)] = [(entry_ts, r, bps, reason)] pooled over coins and both sides."""
    rnd, trades = random.Random(seed), {}
    for c in COINS:
        s1o, s4o, s1d = load(c, "1h"), load(c, "4h"), load(c, "1d")
        lab_d = t2(s1d)
        d_idx = align(s1d.ts, BAR_MS["1d"], s1o.ts, BAR_MS["1h"])
        f_idx = align(s4o.ts, BAR_MS["4h"], s1o.ts, BAR_MS["1h"])
        for side in ("long", "short"):
            s1 = s1o if side == "long" else s1o.mirrored()
            s4 = s4o if side == "long" else s4o.mirrored()
            a1, a4 = atr(s1.h, s1.l, s1.c, 14), atr(s4.h, s4.l, s4.c, 14)
            bands = Z.active_bands(s4, a4, Z.build(s4, a4))
            want = "up" if side == "long" else "down"
            memo = {}

            def passes(f, i, s1=s1, a4=a4, bands=bands, want=want, memo=memo, d_idx=d_idx, f_idx=f_idx, lab_d=lab_d):
                if f in ("trend", "both") and not (d_idx[i] >= 0 and lab_d[d_idx[i]] == want):
                    return False
                if f in ("zone", "both"):
                    if i not in memo:
                        j = f_idx[i]
                        pad = 0.5 * a4[j] if j >= 0 and not math.isnan(a4[j]) else 0
                        memo[i] = j >= 0 and any(s1.l[i] <= hi + pad and s1.c[i] >= lo - pad for lo, hi in bands[j])
                    return memo[i]
                return True

            fires = T.fire_all(s1)
            fires["random"] = [rnd.random() < RANDOM_P for _ in range(len(s1))]
            for name, flags in fires.items():
                signals = [i for i, x in enumerate(flags) if x]
                for f in FILTERS:
                    busy = -1
                    for i in signals:
                        if i < busy or not passes(f, i):
                            continue
                        res = T.simulate(s1, a1, i, **EXIT)
                        if res is None:
                            continue
                        busy = res.exit_i
                        trades.setdefault((name, f), []).append((s1.ts[res.entry_i], res.r, res.bps, res.reason))
    return trades


def summary(rows) -> str:
    n = len(rows)
    if n < 20:
        return f"{n:>5}  {'–':>5} {'–':>6} {'–':>6} {'–':>5}"
    r = [x[1] for x in rows]
    m, sd = st.mean(r), st.pstdev(r)
    return (f"{n:>5}  {sum(x > 0 for x in r) / n * 100:4.0f}% {m:+6.2f} {st.mean(x[2] for x in rows):+6.0f} "
            f"{(m / (sd / math.sqrt(n)) if sd else float('nan')):5.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--hold", type=int, default=48, help="time exit, in 1h candles")
    args = ap.parse_args()
    EXIT.update(stop_atr=args.stop_atr, rr=args.rr, max_hold=args.hold)
    default = (args.stop_atr, args.rr, args.hold) == (1.5, 2.0, 48)
    trades = gather()
    lo = min(load(c, "1h").ts[0] for c in COINS)
    hi = max(load(c, "1h").ts[-1] for c in COINS)
    cut = lambda f: lo + (hi - lo) * f  # noqa: E731
    periods = {"design": (cut(0), cut(0.6)), "check": (cut(0.6), cut(0.8))}
    if args.final:
        periods["FINAL"] = (cut(0.8), hi + 1)
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")  # noqa: E731
    inp = lambda rows, p: [x for x in rows if periods[p][0] <= x[0] < periods[p][1]]  # noqa: E731
    head = f"{'trigger':<12}{'filter':<7}" + "".join(f"| {p:<6} n   win%      R   bps     t " for p in periods)

    out = ["# Step 3: entry triggers after fees, alone and with filters\n",
           f"Data: {', '.join(c[:-4] for c in COINS)}, USD-M perpetual 1h candles {day(lo)} to {day(hi)}, long and short. Every trade: enter at the next open, "
           f"stop {EXIT['stop_atr']:g} ATR(1h) away, target {EXIT['rr']:g}R, exit after {EXIT['max_hold']} h, taker fee 5 bps + slippage 3 bps per fill (no slippage on the target), funding 1 bp per 8 h. "
           "R = result in units of the initial risk after all costs; bps = net result per trade; t = mean R over its standard error "
           "(trades of different coins overlap in time, so treat |t| under 3 as noise). `random` = entries at random candles under the same filter and exits: the baseline.\n",
           "```", head]
    for name in ("random", *T.TRIGGERS):
        for f in FILTERS:
            rows = trades.get((name, f), [])
            out.append(f"{name:<12}{f:<7}" + "".join(f"| {summary(inp(rows, p)):<34}" for p in periods))
        out.append("")
    out.append("```\n")

    out.append("## Does a filter help? Mean R, design + check pooled (n in brackets)\n\n```\ntrigger      " + "".join(f"{f:>16}" for f in FILTERS))
    for name in ("random", *T.TRIGGERS):
        cells = []
        for f in FILTERS:
            rows = [x for p in periods for x in inp(trades.get((name, f), []), p)]
            cells.append(f"{st.mean(x[1] for x in rows):+.2f} ({len(rows)})" if len(rows) >= 20 else "–")
        out.append(f"{name:<12}" + "".join(f"{c:>16}" for c in cells))
    out.append("```\n")

    out.append("## How the trades ended (no filter, design + check)\n\n```\ntrigger       stop   target     time")
    for name in ("random", *T.TRIGGERS):
        rows = [x for p in periods for x in inp(trades.get((name, "none"), []), p)]
        if rows:
            out.append(f"{name:<12}" + "".join(f"{sum(x[3] == w for x in rows) / len(rows) * 100:7.0f}%" for w in ("stop", "target", "time")))
    out.append("```")
    text = "\n".join(out)
    Path("research/results").mkdir(parents=True, exist_ok=True)
    Path("research/results/step3-triggers.md" if default else f"research/results/step3-triggers-{args.stop_atr:g}atr-{args.rr:g}r-{args.hold}h.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
