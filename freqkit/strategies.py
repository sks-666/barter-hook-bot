"""Strategy files of the Freqtrade subsystem: list, read, create from a template, save with validation, duplicate,
delete. A strategy is one Python file in ft_user_data/strategies/ holding a class that extends IStrategy; the class
name is what Freqtrade (and the page) call the strategy.

Validation of a save goes through Freqtrade itself: the candidate is written, the webserver is asked to load the class
(GET /strategy/<name>, which imports the file afresh), and if Freqtrade refuses it the previous content is put back, so
a broken edit can never be the file the bot loads next. Nothing outside the strategies folder can be touched.
"""
import ast
import os
import re
import time
from pathlib import Path

NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,80}\.py$")
MAX_BYTES = 512_000

TEMPLATE = '''# {name}: a Freqtrade strategy created from the dashboard on {date}.
# Docs: https://www.freqtrade.io/en/stable/strategy-customization/
# flake8: noqa: F401
# isort: skip_file
from datetime import datetime

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

import freqtrade.vendor.qtpylib.indicators as qtpylib
from freqtrade.strategy import IStrategy, IntParameter


class {name}(IStrategy):
    """{description}"""

    INTERFACE_VERSION = 3
    timeframe = "{timeframe}"
    can_short = False

    # Exit when the trade has made this much (minutes since entry -> profit ratio); 0 = any time
    minimal_roi = {roi}
    stoploss = {stoploss}
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 50

    order_types = {{"entry": "limit", "exit": "limit", "stoploss": "market", "stoploss_on_exchange": False}}
    order_time_in_force = {{"entry": "GTC", "exit": "GTC"}}

    rsi_buy = IntParameter(20, 45, default=30, space="buy")
    rsi_sell = IntParameter(55, 85, default=70, space="sell")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] < self.rsi_buy.value)
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] > self.rsi_sell.value)
            & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
'''


def strategy_classes(code: str) -> list[str]:
    """Class names in the file that extend IStrategy (by name; a syntax error gives [])."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [getattr(b, "id", None) or getattr(b, "attr", None) for b in node.bases]
            if "IStrategy" in bases:
                out.append(node.name)
    return out


def syntax_error(code: str) -> str | None:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    return None


class StrategyFiles:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def path_of(self, filename: str) -> Path:
        if not FILE_RE.match(filename):
            raise ValueError("filename must be a plain name ending in .py (letters, digits, _ and -)")
        path = (self.folder / filename).resolve()
        if path.parent != self.folder.resolve():
            raise ValueError("bad filename")
        return path

    def list(self) -> list[dict]:
        rows = []
        for path in sorted(self.folder.glob("*.py")):
            if path.name.startswith("_") or path.name.startswith("."):
                continue
            try:
                code = path.read_text(encoding="utf-8")
            except OSError:
                continue
            st = path.stat()
            classes = strategy_classes(code)
            tf = re.search(r"^\s*timeframe\s*=\s*[\"']([^\"']+)[\"']", code, re.M)
            rows.append({"file": path.name, "classes": classes, "name": classes[0] if classes else None,
                         "timeframe": tf.group(1) if tf else None, "size": st.st_size, "mtime": st.st_mtime,
                         "lines": code.count("\n") + 1, "syntax_error": syntax_error(code)})
        return rows

    def file_for(self, name: str) -> str | None:
        for row in self.list():
            if name in row["classes"]:
                return row["file"]
        return None

    def read(self, filename: str) -> str:
        path = self.path_of(filename)
        if not path.is_file():
            raise ValueError(f"{filename} does not exist")
        return path.read_text(encoding="utf-8")

    def check_code(self, code: str, expect_name: str | None = None) -> str:
        """Static checks before Freqtrade sees the file. Returns the strategy class name."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("the file is empty")
        if len(code.encode("utf-8")) > MAX_BYTES:
            raise ValueError("the file is too large (limit 512 kB)")
        err = syntax_error(code)
        if err:
            raise ValueError(f"syntax error at {err}")
        classes = strategy_classes(code)
        if not classes:
            raise ValueError("no class extending IStrategy found: a strategy file needs `class Name(IStrategy):`")
        if expect_name and expect_name not in classes:
            raise ValueError(f"the file must keep a class named {expect_name} (found {', '.join(classes)})")
        return expect_name or classes[0]

    def write(self, filename: str, code: str) -> Path:
        path = self.path_of(filename)
        tmp = path.with_suffix(".py.tmp")
        tmp.write_text(code, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def render_template(self, name: str, timeframe: str = "5m", stoploss: float = -0.10,
                        roi: dict | None = None, description: str = "") -> str:
        if not NAME_RE.match(name):
            raise ValueError("the strategy name must be a Python class name starting with a capital letter, e.g. MyTrend")
        try:
            stoploss = float(stoploss)
        except (TypeError, ValueError):
            raise ValueError("stoploss must be a number such as -0.10 (= -10%)")
        if not -1.0 < stoploss < 0:
            raise ValueError("stoploss must be negative and above -1.0, e.g. -0.10 for 10%")
        roi = roi or {"0": 0.04, "30": 0.02, "120": 0.01}
        clean: dict[str, float] = {}
        for k, v in roi.items():
            try:
                clean[str(int(k))] = float(v)
            except (TypeError, ValueError):
                raise ValueError("minimal_roi must map minutes to a profit ratio, e.g. {\"0\": 0.04, \"60\": 0.01}")
        roi_src = "{" + ", ".join(f'"{k}": {v}' for k, v in sorted(clean.items(), key=lambda kv: int(kv[0]))) + "}"
        return TEMPLATE.format(name=name, timeframe=timeframe, stoploss=stoploss, roi=roi_src,
                               description=(description or f"{name}: RSI pullback inside an EMA uptrend, "
                                            "created from the dashboard. Edit freely.").replace('"""', "'"),
                               date=time.strftime("%Y-%m-%d"))

    def filename_for(self, name: str) -> str:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        return f"{snake}.py"

    def delete(self, filename: str) -> None:
        path = self.path_of(filename)
        if not path.is_file():
            raise ValueError(f"{filename} does not exist")
        path.unlink()
