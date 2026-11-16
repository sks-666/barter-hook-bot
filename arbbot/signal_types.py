"""Signal types: what KIND of trade an idea is, and the rules that kind of trade must meet.

Every LONG / SHORT idea is classified by the quality check (arbbot/quality.py) into the FIRST type whose "when"
conditions all hold: your custom types first (in the order you made them), then the built-in ones in this order:

  funding_squeeze  the other side is paying a lot of funding (a crowded side about to be squeezed)
  sweep_reversal   a swing's stops were just swept and price closed back inside
  breakout         the trigger candle closed past the 20-candle range
  trend_pullback   with the HTF trend
  candle_reversal  a reversal candlestick (engulfing, hammer / shooting star, morning / evening star) at a support /
                   resistance, against the trend or in a range
  mean_reversion   everything else (against the trend, or in a range): the catch-all

The type then sets the trade's own rules: its extra "require" conditions (one failing = NO_TRADE, with the reason),
how it enters (the price now, the whales' price, a pullback to the LTF EMA 21, or a retest of the broken level), the
stop's minimum distance in LTF ATRs, TP1 / TP2 in R, the quality score it needs, and a risk multiplier for the
auto-trader's size. The global guardrails ([REGIME], [FAKEOUT], [FUNDING], [SESSION], the HTF level, the pre-mortem)
apply to every type.

A switched-off BUILT-IN type keeps classifying (so a switched-off breakout is not traded as something else) but its
signals are NO_TRADE. A switched-off CUSTOM type is skipped, as if it did not exist.

Custom types are built from a fixed menu of conditions (FIELDS): no code, nothing evaluated. They are saved in
data/signal-types-<env>.json, made and edited on the terminal's Signals panel, switched on/off from Telegram (/types).
"""
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("signal-types")

REGIMES = ("TRENDING_BULL", "TRENDING_BEAR", "RANGING_MEAN_REVERSION", "VOLATILE_CHOP")
# the condition menu: key -> (label, kind, details). kind "num": (lo, hi); "bool": -; "choice": options
FIELDS = {
    "regime": ("HTF regime", "choice", REGIMES),
    "htf": ("HTF trend vs the trade", "choice", ("with", "against", "flat")),
    "btc": ("BTC (for BTC: S&P / dollar) vs the trade", "choice", ("with", "against", "flat")),
    "breakout": ("Trigger candle broke the 20-candle range", "bool", None),
    "bos": ("Break of structure its way", "bool", None),
    "sweep": ("Liquidity sweep of a swing", "bool", None),
    "fvg": ("Retesting a fair value gap", "bool", None),
    "candle": ("A candlestick pattern its way (last 2 candles)", "bool", None),
    "candle_at_level": ("A reversal candlestick at a support / resistance", "bool", None),
    "candle_pattern": ("Candlestick pattern its way", "choice",
                       ("engulfing", "pin_bar", "star", "three_soldiers", "inside_break", "none")),
    "candle_against": ("A candlestick pattern against it", "bool", None),
    "squeeze": ("ATR contracted before the trigger", "bool", None),
    "vwap_side": ("Price on the trade's side of the 24h VWAP", "bool", None),
    "rvol": ("RVOL of the trigger candle (x average)", "num", (0.0, 20.0)),
    "rsi": ("RSI read for the trade's side (short: 100 - RSI)", "num", (0.0, 100.0)),
    "adx_htf": ("ADX on the HTF candles", "num", (0.0, 100.0)),
    "adx_mid": ("ADX on the MID candles", "num", (0.0, 100.0)),
    "vwap_dist": ("Distance from the 24h VWAP in LTF ATRs (+ = on the trade's side)", "num", (-20.0, 20.0)),
    "funding": ("Funding % per 8h the trade's side pays (- = the other side pays)", "num", (-1.0, 1.0)),
    "oi": ("Open interest change % over the timeframe", "num", (-100.0, 100.0)),
    "quality": ("Quality score (0-100)", "num", (0.0, 100.0)),
    "score": ("Combined score strength (0-100)", "num", (0.0, 100.0)),
}
OPS = {"num": (">=", "<="), "bool": ("is", "not"), "choice": ("is", "not")}
ENTRIES = {"whales": "the price, or a pullback to the whales' price (at most 1 ATR)",
           "price": "the price now (market)",
           "ema21": "a pullback to the LTF EMA 21 (when it is within 1.5 ATR; else the price)",
           "retest": "a retest of the broken level (when it is within 1 ATR; else the price)"}
PARAMS = {                     # key -> (lo, hi, default)
    "stop_atr": (0.5, 5.0, 1.0),      # the stop at least this many LTF ATRs from the entry (plus the 0.25 ATR buffer)
    "tp1_r": (1.0, 10.0, 1.8),
    "tp2_r": (1.5, 20.0, 3.0),
    "min_quality": (0, 100, 55),
    "risk_mult": (0.25, 2.0, 1.0),    # the auto-trader's risk per trade x this
}
PARAM_LABELS = {"stop_atr": "Stop ≥ ATR", "tp1_r": "TP1 R", "tp2_r": "TP2 R", "min_quality": "Min quality",
                "risk_mult": "Risk ×"}
MAX_CONDITIONS = 8
MAX_CUSTOM = 20


def _c(k, op, v=None):
    return {"k": k, "op": op, "v": v}


BUILTIN = [
    {"key": "funding_squeeze", "name": "Funding squeeze",
     "about": "The other side pays heavy funding: it is crowded and can be forced out. Needs a break of structure.",
     "when": [_c("funding", "<=", -0.03)], "require": [_c("bos", "is")],
     "entry": "price", "stop_atr": 1.0, "tp1_r": 1.8, "tp2_r": 3.0, "min_quality": 50, "risk_mult": 1.0},
    {"key": "sweep_reversal", "name": "Sweep reversal",
     "about": "A swing's stops were just taken and price closed back inside. Needs at least average volume.",
     "when": [_c("sweep", "is"), _c("breakout", "not")], "require": [_c("rvol", ">=", 1.0)],
     "entry": "price", "stop_atr": 1.0, "tp1_r": 2.0, "tp2_r": 3.5, "min_quality": 55, "risk_mult": 1.0},
    {"key": "breakout", "name": "Breakout",
     "about": "The trigger candle closed past the 20-candle range (needs 1.5x volume: [FAKEOUT]). Enters on the retest.",
     "when": [_c("breakout", "is")], "require": [_c("vwap_side", "is")],
     "entry": "retest", "stop_atr": 1.0, "tp1_r": 1.8, "tp2_r": 3.0, "min_quality": 55, "risk_mult": 1.0},
    {"key": "trend_pullback", "name": "Trend pullback",
     "about": "With the HTF trend, not chasing a stretched RSI. Enters on a pullback to the LTF EMA 21.",
     "when": [_c("htf", "is", "with")], "require": [_c("rsi", "<=", 70)],
     "entry": "ema21", "stop_atr": 1.0, "tp1_r": 1.8, "tp2_r": 3.0, "min_quality": 55, "risk_mult": 1.0},
    {"key": "candle_reversal", "name": "Candle reversal",
     "about": "A reversal candlestick at a support (long) / resistance (short): bullish / bearish engulfing, hammer / "
              "shooting star, morning / evening star. Against the trend or in a range. Needs average volume and no "
              "pattern the other way; the stop goes past the pattern's low / high.",
     "when": [_c("candle_at_level", "is")], "require": [_c("rvol", ">=", 1.0), _c("candle_against", "not")],
     "entry": "price", "stop_atr": 1.0, "tp1_r": 1.8, "tp2_r": 3.0, "min_quality": 55, "risk_mult": 1.0},
    {"key": "mean_reversion", "name": "Mean reversion",
     "about": "Against the trend or in a range (the catch-all): only when stretched, RSI 45 or less for its side and "
              "at least 0.5 ATR past the 24h VWAP. Wider stop, higher quality bar.",
     "when": [], "require": [_c("rsi", "<=", 45), _c("vwap_dist", "<=", -0.5)],
     "entry": "price", "stop_atr": 1.25, "tp1_r": 1.8, "tp2_r": 3.0, "min_quality": 60, "risk_mult": 1.0},
]
BUILTIN_KEYS = tuple(t["key"] for t in BUILTIN)


def check(features: dict, cond: dict) -> bool:
    """One condition against an idea's features; a missing feature (None) fails it."""
    v = features.get(cond["k"])
    if v is None:
        return False
    kind = FIELDS[cond["k"]][1]
    if kind == "num":
        return v >= cond["v"] if cond["op"] == ">=" else v <= cond["v"]
    if kind == "bool":
        return bool(v) if cond["op"] == "is" else not v
    return v == cond["v"] if cond["op"] == "is" else v != cond["v"]


def describe(cond: dict) -> str:
    label, kind, _ = FIELDS[cond["k"]]
    if kind == "bool":
        return label if cond["op"] == "is" else f"not: {label.lower()}"
    if kind == "choice":
        return f"{label} {'is' if cond['op'] == 'is' else 'is not'} {cond['v']}"
    return f"{label} {'≥' if cond['op'] == '>=' else '≤'} {cond['v']:g}"


def classify(features: dict, types: list[dict]) -> dict:
    """The first type (in `types` order, see resolved()) whose `when` conditions all hold."""
    for t in types:
        if all(check(features, c) for c in t["when"]):
            return t
    return types[-1]                                                  # the catch-all is last


def unmet(features: dict, t: dict) -> list[str]:
    """Why this type's own `require` conditions fail ([] = they hold)."""
    out = []
    for c in t["require"]:
        if not check(features, c):
            v = features.get(c["k"])
            now = ("unknown" if v is None else ("yes" if v else "no") if isinstance(v, bool) else
                   f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
            out.append(f"{t['name']} needs {describe(c)} (now {now})")
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "type"
    return "custom_" + s


def validate(t: dict, existing: set[str] | None = None) -> dict:
    """A custom type as sent from the page, checked field by field; raises ValueError with a plain reason."""
    if not isinstance(t, dict):
        raise ValueError("a signal type must be an object")
    name = str(t.get("name") or "").strip()
    if not 1 <= len(name) <= 30:
        raise ValueError("the name must be 1 to 30 characters")
    key = t.get("key") or _slug(name)
    if not isinstance(key, str) or not re.fullmatch(r"custom_[a-z0-9_]{1,24}", key):
        raise ValueError("a custom type's key must look like custom_name")
    out = {"key": key, "name": name, "about": str(t.get("about") or "")[:200], "custom": True,
           "enabled": bool(t.get("enabled", True))}
    for part in ("when", "require"):
        conds = t.get(part) or []
        if not isinstance(conds, list) or len(conds) > MAX_CONDITIONS:
            raise ValueError(f"'{part}' takes at most {MAX_CONDITIONS} conditions")
        clean = []
        for c in conds:
            k, op = (c or {}).get("k"), (c or {}).get("op")
            if k not in FIELDS:
                raise ValueError(f"unknown condition '{k}'")
            label, kind, det = FIELDS[k]
            if op not in OPS[kind]:
                raise ValueError(f"'{label}' takes {' or '.join(OPS[kind])}")
            v = c.get("v")
            if kind == "num":
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not det[0] <= v <= det[1]:
                    raise ValueError(f"'{label}' needs a number from {det[0]:g} to {det[1]:g}")
                v = float(v)
            elif kind == "choice":
                if v not in det:
                    raise ValueError(f"'{label}' is one of {', '.join(det)}")
            else:
                v = None
            clean.append({"k": k, "op": op, "v": v})
        out[part] = clean
    if not out["when"]:
        raise ValueError("a custom type needs at least one 'when' condition (the catch-all is Mean reversion)")
    entry = t.get("entry", "whales")
    if entry not in ENTRIES:
        raise ValueError(f"the entry is one of {', '.join(ENTRIES)}")
    out["entry"] = entry
    for p, (lo, hi, default) in PARAMS.items():
        v = t.get(p, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{PARAM_LABELS[p]} must be a number from {lo:g} to {hi:g}")
        out[p] = int(v) if p == "min_quality" else float(v)
    if out["tp2_r"] <= out["tp1_r"]:
        raise ValueError("TP2 must be further than TP1")
    return out


class TypeBook:
    """The built-in types (switch on/off) and your custom ones, saved in one JSON file."""

    def __init__(self, path: Path | None = None, notify=None):
        self.path, self.notify = path, notify or (lambda text: None)
        self.s = {"off": [], "custom": []}
        if path and Path(path).exists():
            try:
                s = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(s, dict):
                    self.s["off"] = [k for k in s.get("off", []) if k in BUILTIN_KEYS]
                    for t in s.get("custom", []):
                        try:
                            self.s["custom"].append(validate(t, set()))
                        except ValueError as e:                       # a hand-edited file: drop the bad one, say so
                            log.warning("signal types: dropped a saved custom type: %s", e)
            except (OSError, ValueError) as e:
                log.warning("signal types: could not read %s (%s); using the built-in types", path, e)

    def _save(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def resolved(self) -> list[dict]:
        """Classification order: switched-on custom types, then every built-in (each with `enabled`)."""
        off = set(self.s["off"])
        return ([dict(t) for t in self.s["custom"] if t["enabled"]] +
                [dict(t, custom=False, enabled=t["key"] not in off) for t in BUILTIN])

    def all(self) -> list[dict]:
        off = set(self.s["off"])
        return ([dict(t) for t in self.s["custom"]] +
                [dict(t, custom=False, enabled=t["key"] not in off) for t in BUILTIN])

    def get(self, key: str) -> dict | None:
        return next((t for t in self.all() if t["key"] == key), None)

    def find(self, word: str) -> dict | None:
        """A type by key or by (start of) name, for Telegram: 'breakout', 'trend', 'Funding squeeze'."""
        w = word.strip().lower().replace(" ", "_")
        ts = self.all()
        return (next((t for t in ts if t["key"] == w or t["key"] == "custom_" + w), None) or
                next((t for t in ts if t["name"].lower().replace(" ", "_").startswith(w)), None) or
                next((t for t in ts if t["key"].startswith(w)), None))

    def set_enabled(self, key: str, on: bool) -> dict:
        t = self.get(key)
        if t is None:
            raise ValueError(f"no signal type '{key}'")
        if t["custom"]:
            next(c for c in self.s["custom"] if c["key"] == key)["enabled"] = bool(on)
        else:
            off = set(self.s["off"])
            (off.discard if on else off.add)(key)
            self.s["off"] = sorted(off)
        self._save()
        log.info("signal types: %s %s", t["name"], "ON" if on else "OFF")
        self.notify(f"🧩 Signal type {t['name']}: {'ON' if on else 'OFF'}")
        return self.get(key)

    def save(self, t: dict) -> dict:
        """Add or replace a custom type (by key); the built-in ones cannot be edited, only copied."""
        if (t or {}).get("key") in BUILTIN_KEYS:
            raise ValueError("the built-in types can only be switched on or off: copy one to change its rules")
        clean = validate(t, {c["key"] for c in self.s["custom"]})
        i = next((n for n, c in enumerate(self.s["custom"]) if c["key"] == clean["key"]), None)
        if i is not None and not t.get("key"):                        # a new type whose name makes an existing key
            raise ValueError(f"a custom type named like '{clean['name']}' already exists")
        if i is None:
            if len(self.s["custom"]) >= MAX_CUSTOM:
                raise ValueError(f"at most {MAX_CUSTOM} custom types")
            self.s["custom"].append(clean)
        else:
            self.s["custom"][i] = clean
        self._save()
        log.info("signal types: saved %s", clean["name"])
        self.notify(f"🧩 Signal type {clean['name']} saved ({'ON' if clean['enabled'] else 'OFF'})")
        return clean

    def delete(self, key: str) -> None:
        if key in BUILTIN_KEYS:
            raise ValueError("the built-in types cannot be deleted, only switched off")
        n = len(self.s["custom"])
        self.s["custom"] = [c for c in self.s["custom"] if c["key"] != key]
        if len(self.s["custom"]) == n:
            raise ValueError(f"no custom type '{key}'")
        self._save()
        self.notify(f"🧩 Signal type {key.removeprefix('custom_')} deleted")

    def view(self) -> dict:
        return {"types": [dict(t, rules=[describe(c) for c in t["when"]], needs=[describe(c) for c in t["require"]])
                          for t in self.all()],
                "fields": {k: {"label": l, "kind": kind, "options": list(d) if kind == "choice" else None,
                               "range": list(d) if kind == "num" else None, "ops": list(OPS[kind])}
                           for k, (l, kind, d) in FIELDS.items()},
                "entries": ENTRIES, "params": {k: list(v) for k, v in PARAMS.items()},
                "max_conditions": MAX_CONDITIONS, "max_custom": MAX_CUSTOM}
