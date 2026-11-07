"""python desk.py research | status | ladder | graveyard   (see `python desk.py --help`)"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

from swingkit.backtest import CostCfg
from swingkit.cli import DEFAULT_SYMBOLS, _load_all, _load_funding, _params, _symbols
from swingkit.data import INTERVAL_MS
from swingkit.risk import RiskCfg
from swingkit.strategies import REGISTRY

from .graveyard import Graveyard
from .ladder import Ladder
from .report import desk_view, load_research, load_trades, save_research
from .research import research_strategy
from .rules import DeskRules

ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="desk", description="Research gates and promotion ladder for the swing strategies")
    p.add_argument("--rules", default=str(ROOT / "desk_rules.toml"), help="desk rules file (default desk_rules.toml)")
    p.add_argument("--desk-dir", default=str(ROOT / "data" / "desk"), help="where results, the ladder and the graveyard live")
    p.add_argument("--journal-db", action="append", help="swing journal(s) with the paper/live trades (default: the testnet and "
                   "paper journals in data/ that exist)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("research", help="walk-forward every gate on historical candles (downloads up to 3 years on first use)")
    r.add_argument("--strategy", default="all", help=f"{', '.join(REGISTRY)} or all")
    r.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    r.add_argument("--interval", default="4h", choices=list(INTERVAL_MS))
    r.add_argument("--days", type=int, default=1095, help="history to use (default 1095 = three years)")
    r.add_argument("--market", default="futures", choices=["futures", "spot"])
    r.add_argument("--cache-dir", default=str(ROOT / "data" / "klines"))
    r.add_argument("--refresh", action="store_true")
    r.add_argument("--offline", action="store_true", help="use the candle cache only")
    r.add_argument("--fit", action="store_true", help="choose each window's setting on its training data (default: the rules as written)")
    r.add_argument("--set", action="append", dest="params", metavar="strategy.param=value", help="declare a setting, e.g. trend.slow=150")
    r.add_argument("--fee-bps", type=float, default=5.0)
    r.add_argument("--slippage-bps", type=float, default=3.0)
    r.add_argument("--funding-bps", type=float, default=1.0)
    r.add_argument("--flat-funding", action="store_true")
    r.add_argument("--no-short", action="store_true")
    sub.add_parser("status", help="the ladder and the gates of every strategy")
    lad = sub.add_parser("ladder", help="check / promote / fail / resume")
    ls = lad.add_subparsers(dest="op", required=True)
    ls.add_parser("check", help="assess everyone and apply the automatic and protective steps")
    for op, h in (("promote", "climb one rung (operator decision; refused unless eligible)"),
                  ("fail", "report a breach: rungs 1-2 go back to research, rungs 3-5 drop a rung"),
                  ("resume", "put a paused or sent-back strategy to work again after review")):
        x = ls.add_parser(op, help=h)
        x.add_argument("strategy", choices=list(REGISTRY))
        x.add_argument("--note", default="")
        if op == "fail":
            x.add_argument("--reason", required=True)
        if op == "resume":
            x.add_argument("--rung", type=int, choices=range(1, 6))
    sub.add_parser("graveyard", help="how many configurations have been tested per strategy")
    return p


def _journals(args) -> list[Path]:
    if args.journal_db:
        return [Path(x) for x in args.journal_db]
    return [p for p in (ROOT / "data" / "swing-journal-testnet.db", ROOT / "data" / "swing-journal.db") if p.exists()]


def _format_gates(res: dict) -> str:
    lines = [f"=== {res['strategy']}  {'APPROVED' if res['approved'] else 'NOT APPROVED'}  (rules {res['rules_version']}) ==="]
    if res.get("error"):
        return "\n".join(lines + [f"  {res['error']}"])
    s = res["stats"]
    lines.append(f"  out-of-sample {s['net_pct']:+.1f}% over {s['windows']} windows ({s['oos_days']:.0f} days), {s['trades']} trades, "
                 f"Sharpe {s['sr_ann']:.2f}, max drawdown {s['mdd_pct']:.1f}%, deflated Sharpe {s['dsr']:.3f} "
                 f"(N={res['trials']} trials, SR0 {s['sr0']:.3f})")
    if res.get("seen_before"):
        lines.append(f"  note: this exact configuration was tested {res['seen_before']} time(s) before; each retest counts as a trial")
    for g in res["gates"]:
        lines.append(f"  [{'PASS' if g['ok'] else 'FAIL'}] {g['name']:<18} {g['value']:<44} need {g['threshold']}" + (f"  ({g['note']})" if g["note"] else ""))
    b = res["baseline"]
    if b["n"]:
        lines.append(f"  trades: win rate {b['win_rate'] * 100:.0f}%, payoff {b['payoff']:.2f}, expectancy {b['mean_r']:+.2f}R; "
                     f"sizing {res['sizing']['f'] * 100:.2f}% of equity (cap {res['sizing']['f_max'] * 100:g}%)")
    return "\n".join(lines)


def _format_ladder(a: dict) -> str:
    st = "" if a["status"] == "active" else f"  [{a['status'].upper()}]"
    lines = [f"  {a['strategy']:<6} rung {a['rung']} {a['rung_name']} ({a['capital']}){st}  {a['trades']} trades, {a['days']:.0f} days"]
    for c in a["checks"]:
        lines.append(f"           [{'ok' if c['ok'] else '..'}] {c['name']}: {c['value']} (need {c['need']})")
    if a["streak_limit"] is not None:
        lines.append(f"           losing streak {a['streak']} (pause at {a['streak_limit']:g})")
    if a["drift"]:
        lines.append(f"           drift: live {a['drift']['live']:+.1f}R vs 5th percentile {a['drift']['p5']:+.1f}R" + ("  BREACH" if a["drift"]["breach"] else ""))
    for act, why in a["actions"]:
        lines.append(f"           -> {act}: {why}")
    if a["eligible"]:
        lines.append("           eligible for the next rung" + ("" if a["rung"] == 1 else ": python desk.py ladder promote " + a["strategy"]))
    return "\n".join(lines)


def _research(args, rules: DeskRules) -> int:
    names = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    declared = _params(args.params)
    for n in names:
        if n not in REGISTRY:
            print(f"unknown strategy {n!r}", file=sys.stderr)
            return 2
    args.symbols = ",".join(_symbols(args.symbols))
    data = asyncio.run(_load_all(args))
    btc = data.get("BTCUSDT")
    if btc is None:                                    # the crash gate needs BTC even when it is not being traded
        b = argparse.Namespace(**{**vars(args), "symbols": "BTCUSDT"})
        btc = asyncio.run(_load_all(b))["BTCUSDT"]
    spot = args.market == "spot"
    funding = None if (spot or args.flat_funding) else asyncio.run(_load_funding(args, list(data)))
    costs = CostCfg(args.fee_bps, args.slippage_bps, 0.0 if spot else args.funding_bps)
    grave = Graveyard(Path(args.desk_dir) / "graveyard.db")
    results = {}
    for n in names:
        res = research_strategy(n, data, btc, funding, rules, grave, params=declared.get(n), fit=args.fit, costs=costs,
                                risk=RiskCfg(risk_pct=rules.risk_pct), bar_ms=INTERVAL_MS[args.interval], interval=args.interval,
                                market=args.market, allow_short=not (args.no_short or spot),
                                log=lambda m: print(f"  {m}", file=sys.stderr))
        results[n] = res
        print(_format_gates(res) + "\n")
    path = save_research(Path(args.desk_dir), dict(generated=int(time.time() * 1000), rules_version=rules.version,
                                                    symbols=sorted(data), interval=args.interval, market=args.market), results)
    print(f"Results saved to {path}; the terminal's Desk panel shows them.")
    return 0                                           # approval is a result, not an error


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rules = DeskRules.load(args.rules)
    except ValueError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    desk_dir = Path(args.desk_dir)
    if args.cmd == "research":
        try:
            return _research(args, rules)
        except ValueError as e:
            print(f"Refused: {e}", file=sys.stderr)
            return 2
    if args.cmd == "graveyard":
        g = Graveyard(desk_dir / "graveyard.db")
        fam = g.families()
        print("configurations tested per strategy (each one counts towards the deflated Sharpe ratio):")
        for name, n in fam.items():
            print(f"  {name:<6} {n} trials")
        if not fam:
            print("  none yet: run `python desk.py research`")
        return 0
    journals = _journals(args)
    if args.cmd == "status":
        view = desk_view(desk_dir, journals, rules, apply=False)
        for s in view["strategies"]:
            print(_format_gates(s["research"]) if s["research"] else f"=== {s['name']}: no research yet (python desk.py research) ===")
            print(_format_ladder(s["ladder"]) + "\n")
        return 0
    # ladder
    research, trades = load_research(desk_dir), load_trades(journals)
    ladder = Ladder(desk_dir / "ladder.json", rules)
    try:
        if args.op == "check":
            rows = ladder.check(list(REGISTRY), trades, research, apply=True)
            print("\n".join(_format_ladder(a) for a in rows))
        elif args.op == "promote":
            ladder.check([args.strategy], trades, research, apply=False)
            e = ladder.promote(args.strategy, trades, research, args.note)
            print(f"{args.strategy} is now at rung {e['rung']}. Moving real capital is still yours to do by hand.")
        elif args.op == "fail":
            e = ladder.fail(args.strategy, args.reason)
            print(f"{args.strategy}: rung {e['rung']}, status {e['status']}")
        else:
            e = ladder.resume(args.strategy, args.rung, args.note)
            print(f"{args.strategy}: rung {e['rung']}, status {e['status']}")
    except ValueError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    return 0
