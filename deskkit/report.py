"""What the terminal and the command line show: research results and ladder status, read from data/desk and the journals."""
import json
import math
from pathlib import Path

from swingkit.journal import Journal

from .ladder import Ladder
from .rules import DeskRules

STRATEGIES = ("trend", "swing", "smc")


def load_research(desk_dir: Path) -> dict:
    try:
        d = json.loads((Path(desk_dir) / "research.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_research(desk_dir: Path, result: dict, strategies: dict) -> Path:
    """Merge one run into research.json: running a single strategy never wipes the others' results."""
    path = Path(desk_dir) / "research.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = load_research(desk_dir)
    cur.setdefault("strategies", {}).update(strategies)
    cur.update({k: v for k, v in result.items() if k != "strategies"})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def load_trades(journal_paths: list[Path]) -> dict[str, list[dict]]:
    """Closed paper and live trades from the swing journals that exist."""
    out: dict[str, list[dict]] = {"paper": [], "live": []}
    for p in journal_paths:
        if not Path(p).exists():
            continue
        j = Journal(p)
        for mode in out:
            out[mode] += j.trades(mode=mode, status="closed", limit=None)
    return out


def _finite(x):
    """JSON has no infinity or NaN (a browser's JSON.parse refuses them): they become null."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x


def desk_view(desk_dir: Path, journal_paths: list[Path], rules: DeskRules | None = None, apply: bool = False) -> dict:
    """Everything the Desk panel shows. apply=False (the web page) never changes the ladder's saved state."""
    desk_dir, rules = Path(desk_dir), rules or DeskRules()
    research = load_research(desk_dir)
    trades = load_trades(journal_paths)
    names = list(dict.fromkeys([*STRATEGIES, *research.get("strategies", {})]))
    ladder = Ladder(desk_dir / "ladder.json", rules, readonly=not apply)
    rows = ladder.check(names, trades, research, apply=apply)
    out = []
    for a in rows:
        rs = research.get("strategies", {}).get(a["strategy"])
        slim = None
        if rs:
            slim = {k: v for k, v in rs.items() if k != "baseline"}
            slim["baseline"] = {k: v for k, v in rs.get("baseline", {}).items() if k != "r"}
        out.append(dict(name=a["strategy"], research=slim, ladder=a))
    return _finite(dict(rules_version=research.get("rules_version") or rules.version, generated=research.get("generated"),
                        symbols=research.get("symbols"), interval=research.get("interval"), strategies=out))
