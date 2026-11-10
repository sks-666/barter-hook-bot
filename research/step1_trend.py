"""Step 1 study: do the trend labels separate forward returns, on data the rule was not designed on?

    python research/step1_trend.py [--final]

For every daily candle a rule labels up / down / range at its CLOSE. The forward return is measured from the NEXT open
to the close h days later, so nothing is known in advance. Time is split 60% design / 20% check / 20% final; the final
20% is hidden unless --final is given, and should be looked at once, at the very end.
"""
import argparse
import math
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swingkit.data import cache_path, load_csv, to_series  # noqa: E402
from swingkit.trend import RULES, label  # noqa: E402

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
HORIZONS = (5, 10, 20)
DAY = 86_400_000


def load(sym: str):
    return to_series(load_csv(cache_path(Path("data/klines"), "futures", sym, "1d")))


def fwd(s, i: int, h: int):
    return s.c[i + h] / s.o[i + 1] - 1 if i + 1 + h - 1 < len(s) and i + h < len(s) else None


def flips(labs: list[str]) -> tuple[float, float]:
    seq = [x for x in labs if x]
    ch = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    years = len(seq) / 365.25
    return ch / years, len(seq) / (ch + 1)


def stats(vals: list[float]) -> str:
    if len(vals) < 5:
        return f"{'–':>8} {len(vals):>5}"
    return f"{st.mean(vals) * 100:+7.2f}% {len(vals):>5}"


def tstat(a: list[float], b: list[float]) -> float:
    if len(a) < 5 or len(b) < 5:
        return float("nan")
    se = math.sqrt(st.variance(a) / len(a) + st.variance(b) / len(b))
    return (st.mean(a) - st.mean(b)) / se if se else float("nan")


def fmt_t(x: float) -> str:
    return f"{'n/a':>6}" if math.isnan(x) else f"{x:6.1f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true", help="also show the untouched final 20% (look once)")
    a = ap.parse_args()
    data = {c: load(c) for c in COINS}
    lo, hi = min(s.ts[0] for s in data.values()), max(s.ts[-1] for s in data.values())
    cut = lambda f: lo + (hi - lo) * f  # noqa: E731
    periods = {"design (first 60%)": (cut(0), cut(0.6)), "check (next 20%)": (cut(0.6), cut(0.8))}
    if a.final:
        periods["FINAL (last 20%)"] = (cut(0.8), hi + 1)
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")  # noqa: E731
    out = ["# Step 1: trend labels vs forward returns\n",
           f"Data: {', '.join(c[:-4] for c in COINS)}, USD-M perpetual daily candles {day(lo)} to {day(hi)}. "
           f"Forward return = next open to close h days later, no fees (this step only names the trend). "
           f"Cells: mean return and number of days.\n"]
    for rule in RULES:
        labs = {c: label(rule, s) for c, s in data.items()}
        f_year, run = zip(*(flips(labs[c]) for c in COINS))
        out.append(f"## {rule}  ·  flips/year {st.mean(f_year):.1f}  ·  mean run {st.mean(run):.0f} days")
        for pname, (p0, p1) in periods.items():
            out.append(f"\n**{pname}**\n\n```\nh      all days            up                down              range         up−down   t(btc)  t(pool)")
            for h in HORIZONS:
                cols, groups, groups_btc = {}, {"all": [], "up": [], "down": [], "range": []}, {"up": [], "down": []}
                for c, s in data.items():
                    for i in range(len(s) - h - 1):
                        lab = labs[c][i]
                        if not lab or not (p0 <= s.ts[i] < p1):
                            continue
                        r = fwd(s, i, h)
                        if r is None:
                            continue
                        groups["all"].append(r)
                        groups[lab].append(r)
                        if i % h == 0:                      # non-overlapping samples for the t-statistics
                            groups.setdefault("np_" + lab, []).append(r)
                            if c == "BTCUSDT" and lab in groups_btc:
                                groups_btc[lab].append(r)
                diff = (st.mean(groups["up"]) - st.mean(groups["down"])) * 100 if groups["up"] and groups["down"] else float("nan")
                out.append(f"{h:<3} {stats(groups['all'])}   {stats(groups['up'])}   {stats(groups['down'])}   "
                           f"{stats(groups['range'])}   {diff:+6.2f}%  {fmt_t(tstat(groups_btc['up'], groups_btc['down']))}  "
                           f"{fmt_t(tstat(groups.get('np_up', []), groups.get('np_down', [])))}")
            out.append("```")
        out.append("")
    out.append("t(btc): up vs down, BTC only, non-overlapping samples (n/a when fewer than 5 of either). t(pool): the same for all six coins pooled; the coins move "
               "together, so treat it as an overstatement. |t| under 2 means the difference is not distinguishable from noise.")
    text = "\n".join(out)
    Path("research/results").mkdir(parents=True, exist_ok=True)
    Path("research/results/step1-trend.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
