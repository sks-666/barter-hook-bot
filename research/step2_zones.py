"""Step 2 study: when price comes back to a zone, does it bounce more often than at a random price level?

    python research/step2_zones.py [--final] [--controls N]

For each zone type, every TOUCH from above (support) or below (resistance, the same code on mirrored prices) is
followed forward: does price go +1 ATR in the zone's favour before -1 ATR against it, within 5 days? The same question is
asked for RANDOM zones of the same size and distance from price, which is the baseline. A zone type is only worth
anything if it beats that baseline on data it was not designed on. No fees: this measures reaction, not a trade.
Time split 60% design / 20% check / 20% final; the final 20% is hidden unless --final is given.
"""
import argparse
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swingkit import zones as Z  # noqa: E402
from swingkit.data import cache_path, load_csv, to_series  # noqa: E402
from swingkit.indicators import atr  # noqa: E402
from swingkit.mtf import BAR_MS, align  # noqa: E402
from swingkit.trend import t2  # noqa: E402

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
BAR = BAR_MS["4h"]
STATIC = ("swing_sr", "order_block", "fvg")
BAND_ATR = {"range20": 0.25, "range55": 0.25, "ma50": 1.0, "ma200": 1.0}     # a moving band's width, in ATR


def load(sym: str, iv: str):
    return to_series(load_csv(cache_path(Path("data/klines"), "futures", sym, iv)))


def ztest(p1: float, n1: int, p2: float, n2: int) -> float:
    p = (p1 * n1 + p2 * n2) / (n1 + n2) if n1 + n2 else 0.5
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if n1 and n2 and 0 < p < 1 else 0
    return (p1 - p2) / se if se else float("nan")


def collect(data, trend):
    """Real touches of every zone, both sides. Returns events and the (distance, width) of static zones at birth."""
    ev, shape = [], defaultdict(list)
    for c, s4 in data.items():
        idx = align(trend[c][0].ts, BAR_MS["1d"], s4.ts, BAR)
        for side in ("support", "resistance"):
            s = s4 if side == "support" else s4.mirrored()
            a = atr(s.h, s.l, s.c, 14)
            for z in Z.build(s, a):
                if not z.dynamic and not math.isnan(a[z.born]) and s.c[z.born] > z.hi:
                    shape[z.kind].append(((s.c[z.born] - z.hi) / a[z.born], (z.hi - z.lo) / a[z.born], c, side, z.born))
                for t in Z.touches(s, a, z):
                    out = Z.outcome(s, a, t.i)
                    if out is None:
                        continue
                    lab = trend[c][1][idx[t.i]] if idx[t.i] >= 0 else ""
                    with_trend = "" if lab in ("", "range") else ("with" if (lab == "up") == (side == "support") else "against")
                    ev.append(dict(kind=t.kind, coin=c, side=side, n=t.n, ts=s.ts[t.i], out=out, trend=with_trend or lab))
    return ev, shape


def controls(data, shape, per_coin: int, seed: int = 7):
    """Random static zones: the same widths and distances from price as the real static zones, at random times."""
    rnd, ev = random.Random(seed), defaultdict(list)
    dists = [x[0] for k in STATIC for x in shape[k]]
    for kind in Z.KINDS:
        widths = [x[1] for x in shape[kind]] if kind in STATIC else [BAND_ATR[kind]]
        for c, s4 in data.items():
            for side in ("support", "resistance"):
                s = s4 if side == "support" else s4.mirrored()
                a = atr(s.h, s.l, s.c, 14)
                for _ in range(per_coin):
                    b = rnd.randrange(250, len(s) - Z.MAX_AGE - Z.HORIZON - 1)
                    hi = s.c[b] - min(rnd.choice(dists), 12.0) * a[b]
                    z = Z.Zone("ctrl", b, hi - rnd.choice(widths) * a[b], hi)
                    for t in Z.touches(s, a, z)[:1]:                                     # first touches only
                        out = Z.outcome(s, a, t.i)
                        if out is not None:
                            ev[kind].append(dict(ts=s.ts[t.i], out=out))
    return ev


def same_time_controls(data, shape, per_zone: int = 3, seed: int = 11):
    """A stricter baseline for the static kinds: for every real zone, random zones born on the SAME candle with the SAME
    width but a random distance from price. Whatever the market was doing when that zone appeared (for instance, just
    after a rally) is then the same for the zone and for its controls."""
    rnd, ev = random.Random(seed), defaultdict(list)
    dists = [x[0] for k in STATIC for x in shape[k]]
    series = {}
    for kind in STATIC:
        for _, w, c, side, born in shape[kind]:
            if (c, side) not in series:
                s = data[c] if side == "support" else data[c].mirrored()
                series[(c, side)] = (s, atr(s.h, s.l, s.c, 14))
            s, a = series[(c, side)]
            for _ in range(per_zone):
                hi = s.c[born] - min(rnd.choice(dists), 12.0) * a[born]
                for t in Z.touches(s, a, Z.Zone("ctrl", born, hi - w * a[born], hi))[:1]:
                    out = Z.outcome(s, a, t.i)
                    if out is not None:
                        ev[kind].append(dict(ts=s.ts[t.i], out=out))
    return ev


def rate(events) -> tuple[float, int, float]:
    up = sum(e["out"] == "up" for e in events)
    dn = sum(e["out"] == "down" for e in events)
    return (up / (up + dn) if up + dn else float("nan")), up + dn, (len(events) - up - dn) / len(events) if events else 0.0


def cell(events) -> str:
    p, n, none = rate(events)
    return f"{'–':>6} {n:>5}" if n < 10 else f"{p * 100:5.1f}% {n:>5}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--controls", type=int, default=500, help="random zones per kind, coin and side")
    args = ap.parse_args()
    data = {c: load(c, "4h") for c in COINS}
    trend = {}
    for c in COINS:
        d = load(c, "1d")
        trend[c] = (d, t2(d))
    lo, hi = min(s.ts[0] for s in data.values()), max(s.ts[-1] for s in data.values())
    cut = lambda f: lo + (hi - lo) * f  # noqa: E731
    periods = {"design": (cut(0), cut(0.6)), "check": (cut(0.6), cut(0.8))}
    if args.final:
        periods["FINAL"] = (cut(0.8), hi + 1)
    inp = lambda evs, p: [e for e in evs if periods[p][0] <= e["ts"] < periods[p][1]]  # noqa: E731
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")  # noqa: E731

    real, shape = collect(data, trend)
    ctrl = controls(data, shape, args.controls)
    same = same_time_controls(data, shape)
    out = ["# Step 2: zones vs random levels\n",
           f"Data: {', '.join(c[:-4] for c in COINS)}, USD-M perpetual 4h candles {day(lo)} to {day(hi)}; trend label = rule T2 on daily candles. "
           "A touch is followed for up to 30 candles (5 days): does price go +1 ATR in the zone's favour (up for a support, down for a resistance) "
           "before -1 ATR against it? Cells: share of those that went the zone's way, and the number of touches. A random price level gives the baseline. "
           "No fees: this measures the reaction, not a trade.\n"]
    out.append("## First touch vs random levels of the same size and distance\n\n```\n"
               f"{'zone':<12}{'period':<8}{'zone: bounce%   n':<20}{'random: bounce%   n':<21}{'diff':>7}{'z':>7}   neither move")
    for kind in Z.KINDS:
        for p in periods:
            r = [e for e in inp(real, p) if e["kind"] == kind and e["n"] == 1]
            c = inp(ctrl[kind], p)
            pr, nr, none = rate(r)
            pc, nc, _ = rate(c)
            diff, z = (pr - pc) * 100, ztest(pr, nr, pc, nc)
            out.append(f"{kind:<12}{p:<8}{cell(r):<20}{cell(c):<21}{diff:+6.1f}p{z:7.1f}   {none * 100:.0f}%")
    out.append("```\n")

    out.append("## Stricter baseline: random levels born on the same candle as each real zone (static zones, first touches)\n\n"
               "Same moment, same width, random distance from price. This removes 'the zone appeared right after a rally' as an explanation.\n\n```\n"
               f"{'zone':<12}{'period':<8}{'zone: bounce%   n':<20}{'same-time random':<21}{'diff':>7}{'z':>7}")
    for kind in STATIC:
        for p in periods:
            r = [e for e in inp(real, p) if e["kind"] == kind and e["n"] == 1]
            c = inp(same[kind], p)
            pr, nr, _ = rate(r)
            pc, nc, _ = rate(c)
            out.append(f"{kind:<12}{p:<8}{cell(r):<20}{cell(c):<21}{(pr - pc) * 100:+6.1f}p{ztest(pr, nr, pc, nc):7.1f}")
    out.append("```\n")

    out.append("## Does a later touch hold as well as the first? (design + check)\n\n```\nzone        1st touch        2nd touch        3rd+ touch")
    pool = lambda evs: [e for p in periods for e in inp(evs, p)]  # noqa: E731
    for kind in Z.KINDS:
        ks = [e for e in pool(real) if e["kind"] == kind]
        out.append(f"{kind:<12}{cell([e for e in ks if e['n'] == 1]):<17}{cell([e for e in ks if e['n'] == 2]):<17}{cell([e for e in ks if e['n'] >= 3])}")
    out.append("```\n")

    out.append("## Does the daily trend change it? First touches, design + check\n\n"
               "with = a support in an up-trend or a resistance in a down-trend (T2). random = the baseline above, all periods pooled.\n\n"
               "```\nzone        with trend       against trend    daily range      random level")
    for kind in Z.KINDS:
        ks = [e for e in pool(real) if e["kind"] == kind and e["n"] == 1]
        out.append(f"{kind:<12}{cell([e for e in ks if e['trend'] == 'with']):<17}{cell([e for e in ks if e['trend'] == 'against']):<17}"
                   f"{cell([e for e in ks if e['trend'] == 'range']):<17}{cell(pool(ctrl[kind]))}")
    out.append("```\n")

    out.append("## How many zones and touches there were\n\n```\nzone         first touches  later touches")
    for kind in Z.KINDS:
        ks = [e for e in pool(real) if e["kind"] == kind]
        out.append(f"{kind:<12}{sum(e['n'] == 1 for e in ks):>12}{sum(e['n'] > 1 for e in ks):>15}")
    out.append("```\n")
    out.append("z: two-proportion z-test of zone vs random, first touches. Touches of nearby zones of different types overlap in time, and the six coins "
               "move together, so treat |z| under about 3 as not distinguishable from noise.")
    text = "\n".join(out)
    Path("research/results").mkdir(parents=True, exist_ok=True)
    Path("research/results/step2-zones.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
