"""The promotion ladder (Table VI): paper -> live in five rungs, advancing on trade count and clean behaviour.

  1 Paper          >= 20 trades and >= 14 days, strategy passed the research gates         -> advances by itself
  2 Match          >= 20 NEW paper trades, consistency z > -2 against the backtest (eq. 19) -> operator promotes
  3 Live tiny      20 live trades, no breaches (10% of planned capital)                      -> operator promotes
  4 Approval gate  20 live trades, the operator approves each order                          -> operator promotes
  5 Full rules     4 weeks without a breach

Only rung 1 -> 2 happens by itself, because it moves no money. Every step towards real capital needs `promote`: the ladder
reports that a strategy is eligible, a person decides. Rungs 1-2 count the journal's PAPER trades, rungs 3-5 its LIVE trades
(real capital: the testnet mirror is test money and does not count).

Protective rules (applied by `check`): a losing streak of L_max (eq. 20) pauses the strategy; live results below the
bootstrap 5th percentile of its own out-of-sample trades demote it to rung 1 (or send a rung-1 strategy back to research);
a failed consistency test sends it back to research; a strategy that cannot reach 20 trades in 8 weeks is retired."""
import json
import os
import time
from pathlib import Path

from . import stats
from .rules import DeskRules

DAY_MS = stats.DAY_MS
RUNGS = {1: "Paper", 2: "Match", 3: "Live tiny", 4: "Approval gate", 5: "Full rules"}
CAPITAL = {1: "simulated", 2: "simulated", 3: "10% of plan", 4: "planned size", 5: "planned size"}
MODE = {1: "paper", 2: "paper", 3: "live", 4: "live", 5: "live"}


class Ladder:
    def __init__(self, path: Path | str | None, rules: DeskRules | None = None, clock=time.time, readonly: bool = False):
        self.path, self.rules, self.clock, self.readonly = Path(path) if path else None, rules or DeskRules(), clock, readonly
        try:
            s = json.loads(self.path.read_text(encoding="utf-8")) if self.path and self.path.exists() else {}
        except (OSError, ValueError):
            s = {}
        self.state: dict = s if isinstance(s, dict) and "strategies" in s else {"strategies": {}}

    # ---- state -------------------------------------------------------------------------------------
    def _save(self) -> None:
        if self.readonly or not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def ensure(self, name: str, first_ms: int | None = None) -> dict:
        """The strategy's ladder entry. A new one starts at rung 1 from the time of its first paper trade (all logged)."""
        e = self.state["strategies"].get(name)
        if e is None:
            e = {"rung": 1, "status": "active", "rung_start": int(first_ms or self.now_ms()), "history": []}
            self.state["strategies"][name] = e
            self._log(e, "registered at rung 1 (paper)")
        return e

    def _log(self, e: dict, text: str, kind: str = "info") -> None:
        e["history"].append({"ts": self.now_ms(), "kind": kind, "text": text})
        e["history"] = e["history"][-100:]

    def _set_rung(self, e: dict, rung: int, text: str, kind: str = "info") -> None:
        e["rung"], e["rung_start"] = rung, self.now_ms()
        self._log(e, text, kind)

    # ---- assessment (pure: nothing is changed) -----------------------------------------------------------
    def assess(self, name: str, trades: dict[str, list[dict]], baseline: dict | None, approved: bool | None) -> dict:
        e = self.state["strategies"][name]
        rung, rules, now = e["rung"], self.rules, self.now_ms()
        mine = sorted((t for t in trades.get(MODE[rung], []) if t.get("strategy") == name and (t.get("closed_at") or 0) >= e["rung_start"]),
                      key=lambda t: t["closed_at"])
        n, days = len(mine), (now - e["rung_start"]) / DAY_MS
        r = [t["r_multiple"] for t in mine]
        out = dict(strategy=name, rung=rung, rung_name=RUNGS[rung], capital=CAPITAL[rung], status=e["status"], trades=n,
                   days=round(days, 1), checks=[], eligible=False, actions=[], z=None, streak=0, streak_limit=None, drift=None,
                   history=e["history"][-6:])
        chk = lambda label, ok, value, need: out["checks"].append(dict(name=label, ok=bool(ok), value=value, need=need))  # noqa: E731
        breaches = sum(1 for h in e["history"] if h["kind"] == "breach" and h["ts"] >= e["rung_start"])

        if rung == 1:
            chk("Passed the research gates", approved is True, "yes" if approved else "no" if approved is False else "not run", "approved")
            chk("Paper trades", n >= rules.rung_min_trades, f"{n}", f">= {rules.rung_min_trades}")
            chk("Days on paper", days >= rules.rung1_min_days, f"{days:.0f}", f">= {rules.rung1_min_days}")
        elif rung == 2:
            chk("Backtest baseline", baseline is not None and baseline.get("n", 0) >= 2, "found" if baseline else "missing", "run desk.py research")
            chk("New paper trades", n >= rules.rung_min_trades, f"{n}", f">= {rules.rung_min_trades}")
            if baseline and n >= 2:
                out["z"] = stats.consistency_z(r, baseline["mean_r"], baseline["std_r"])
            if n >= rules.rung_min_trades and out["z"] is not None:
                chk("Consistency test z", out["z"] > -2, f"{out['z']:+.2f}", "> -2")
                if out["z"] <= -2:
                    out["actions"].append(("needs_research", f"consistency test failed: z = {out['z']:+.2f} over {n} paper trades"))
        elif rung in (3, 4):
            chk("Live trades", n >= rules.rung_min_trades, f"{n}", f">= {rules.rung_min_trades}")
            chk("No breaches", breaches == 0, f"{breaches}", "0")
        else:
            chk("Days without a breach", days >= rules.rung5_clean_days and breaches == 0, f"{days:.0f} ({breaches} breaches)",
                f">= {rules.rung5_clean_days}")
        out["eligible"] = e["status"] == "active" and all(c["ok"] for c in out["checks"]) and rung < 5

        if baseline and n:                                          # health: the protective rules
            p = baseline.get("win_rate", 0.0)
            streak = 0
            for t in reversed(mine):
                if t["pnl_usd"] > 0:
                    break
                streak += 1
            lim = stats.streak_limit(p)
            out["streak"], out["streak_limit"] = streak, lim
            if streak >= lim:
                out["actions"].append(("pause", f"{streak} losses in a row (limit {lim:g} for a {p * 100:.0f}% win rate)"))
            if n >= rules.drift_min_trades and baseline.get("r"):
                d = stats.bootstrap_drift(baseline["r"], r)
                out["drift"] = d
                if d and d["breach"]:
                    out["actions"].append(("demote", f"live result {d['live']:+.1f}R is below the bootstrap 5th percentile {d['p5']:+.1f}R"))
        if days > rules.retire_days and n < rules.rung_min_trades:
            out["actions"].append(("retire", f"only {n} trades in {days:.0f} days at rung {rung} (needs {rules.rung_min_trades} within {rules.retire_days})"))
        if out["eligible"] and rung == 1:
            out["actions"].append(("advance", "20 paper trades, 14 days, gates passed"))
        return out

    # ---- applying -----------------------------------------------------------------------------------
    def check(self, names: list[str], trades: dict[str, list[dict]], research: dict, apply: bool = True) -> list[dict]:
        """Assess every strategy and apply the protective rules and the automatic rung 1 -> 2 step."""
        out = []
        for name in names:
            first = min((t["opened_at"] for t in trades.get("paper", []) if t.get("strategy") == name and t.get("opened_at")), default=None)
            e = self.ensure(name, first)
            rs = research.get("strategies", {}).get(name)
            a = self.assess(name, trades, rs.get("baseline") if rs else None, rs.get("approved") if rs else None)
            if apply and e["status"] == "active":
                for action, why in a["actions"]:
                    if action == "advance":
                        self._set_rung(e, 2, f"advanced to rung 2 (match): {why}")
                        a = self.assess(name, trades, rs.get("baseline") if rs else None, rs.get("approved") if rs else None)
                    elif action == "pause":
                        e["status"] = "paused"; self._log(e, f"paused: {why}", "pause")
                        break
                    elif action == "demote":
                        if e["rung"] > 1:
                            self._set_rung(e, 1, f"demoted to rung 1: {why}", "demote")
                        else:
                            e["status"] = "needs_research"; self._log(e, f"back to research: {why}", "demote")
                        break
                    elif action == "needs_research":
                        e["status"] = "needs_research"; self._log(e, f"back to research: {why}", "demote")
                        break
                    elif action == "retire":
                        e["status"] = "retired"; self._log(e, f"retired: {why}", "retire")
                        break
                a["status"], a["rung"], a["rung_name"] = e["status"], e["rung"], RUNGS[e["rung"]]
            out.append(a)
        if apply:
            self._save()
        return out

    def promote(self, name: str, trades: dict, research: dict, note: str = "") -> dict:
        """The operator's decision to climb one rung. Refused unless every check of the current rung passes."""
        if name not in self.state["strategies"]:
            raise ValueError(f"{name} is not on the ladder yet: run `desk.py ladder check`")
        e = self.state["strategies"][name]
        rs = research.get("strategies", {}).get(name)
        a = self.assess(name, trades, rs.get("baseline") if rs else None, rs.get("approved") if rs else None)
        if e["status"] != "active":
            raise ValueError(f"{name} is {e['status']}: {self._why(e)}")
        if e["rung"] >= 5:
            raise ValueError(f"{name} is already at the top rung")
        if not a["eligible"]:
            todo = "; ".join(f"{c['name']} {c['value']} (need {c['need']})" for c in a["checks"] if not c["ok"])
            raise ValueError(f"{name} is not eligible for rung {e['rung'] + 1}: {todo}")
        self._set_rung(e, e["rung"] + 1, f"promoted by the operator to rung {e['rung'] + 1} ({RUNGS[e['rung'] + 1]})" + (f": {note}" if note else ""), "promote")
        self._save()
        return e

    def fail(self, name: str, reason: str) -> dict:
        """A breach reported by the operator or a monitor. Rungs 1-2: back to research. Rungs 3-5: one rung down."""
        e = self.ensure(name)
        if e["rung"] <= 2:
            e["status"] = "needs_research"
            self._log(e, f"back to research: {reason}", "breach")
        else:
            self._set_rung(e, e["rung"] - 1, f"dropped to rung {e['rung'] - 1}: {reason}", "breach")
        self._save()
        return e

    def resume(self, name: str, rung: int | None = None, note: str = "") -> dict:
        """Operator review done: put a paused / sent-back strategy to work again (optionally at a given rung)."""
        e = self.ensure(name)
        if rung is not None:
            if rung not in RUNGS:
                raise ValueError("rung must be 1 to 5")
            e["rung"] = rung
        e["status"], e["rung_start"] = "active", self.now_ms()
        self._log(e, f"resumed by the operator at rung {e['rung']}" + (f": {note}" if note else ""), "info")
        self._save()
        return e

    @staticmethod
    def _why(e: dict) -> str:
        last = next((h["text"] for h in reversed(e["history"]) if h["kind"] in ("pause", "demote", "retire", "breach")), "")
        return last or "review it first (desk.py ladder resume)"
