"""Async client for Freqtrade's REST API (docs/rest-api.md of the official repository).

Every call goes to http://<host>:<port>/api/v1/<command> with HTTP Basic auth (the `username` / `password` of the
`api_server` block that freqkit writes into Freqtrade's config). Only `ping` is public. Two instances of Freqtrade are
addressed: the trading bot (`freqtrade trade`: status, trades, profit, start/stop...) and the webserver
(`freqtrade webserver`: strategies, data download, backtesting, pair history), because Freqtrade serves the
backtesting and data endpoints only in webserver mode.

Errors: a connection problem raises FreqtradeUnavailable (the process is down or still starting); an HTTP error raises
FreqtradeApiError with Freqtrade's own message, so the page can show why a request was refused.
"""
import asyncio
from typing import Any

import aiohttp


class FreqtradeError(Exception):
    """Base class: anything that went wrong talking to Freqtrade."""


class FreqtradeUnavailable(FreqtradeError):
    """Freqtrade's API did not answer (not started, still starting, or crashed)."""


class FreqtradeApiError(FreqtradeError):
    """Freqtrade answered with an HTTP error; `detail` is its message."""

    def __init__(self, status: int, detail: str, path: str = ""):
        super().__init__(f"HTTP {status} on {path}: {detail}")
        self.status, self.detail, self.path = status, detail, path


class FreqtradeClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, username: str, password: str,
                 timeout_s: float = 10.0):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.auth = aiohttp.BasicAuth(username, password)
        self.timeout_s = timeout_s

    def url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    async def _call(self, method: str, path: str, params: dict | None = None, json: Any = None,
                    timeout_s: float | None = None) -> Any:
        if isinstance(params, dict):   # aiohttp refuses None values and bools; Freqtrade parses "true"/"false"
            params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
        timeout = aiohttp.ClientTimeout(total=timeout_s or self.timeout_s)
        try:
            async with self.session.request(method, self.url(path), params=params, json=json, auth=self.auth,
                                            timeout=timeout) as r:
                if r.status >= 400:
                    raise FreqtradeApiError(r.status, await self._detail(r), path)
                if r.content_type == "application/json":
                    return await r.json()
                return await r.text()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            raise FreqtradeUnavailable(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from e
        except aiohttp.ClientError as e:
            raise FreqtradeUnavailable(f"{type(e).__name__}: {e}") from e

    @staticmethod
    async def _detail(r: aiohttp.ClientResponse) -> str:
        try:
            body = await r.json()
        except Exception:
            return (await r.text())[:300] or r.reason or "error"
        if isinstance(body, dict):
            d = body.get("detail", body.get("error", body))
            if isinstance(d, list):   # pydantic validation errors: [{"loc": [...], "msg": ...}]
                return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in d)[:300]
            return str(d)[:300]
        return str(body)[:300]

    # ---- public / info -----------------------------------------------------------------------------------------
    async def ping(self) -> bool:
        try:
            r = await self._call("GET", "ping", timeout_s=3.0)
        except FreqtradeError:
            return False
        return isinstance(r, dict) and r.get("status") == "pong"

    async def version(self) -> dict:
        return await self._call("GET", "version")

    async def health(self) -> dict:
        return await self._call("GET", "health")

    async def show_config(self) -> dict:
        return await self._call("GET", "show_config")

    async def sysinfo(self) -> dict:
        return await self._call("GET", "sysinfo")

    async def logs(self, limit: int = 200) -> dict:
        return await self._call("GET", "logs", params={"limit": limit})

    # ---- bot control (trade mode) -------------------------------------------------------------------------------
    async def start(self) -> dict:
        return await self._call("POST", "start")

    async def stop(self) -> dict:
        return await self._call("POST", "stop")

    async def pause(self) -> dict:
        return await self._call("POST", "pause")

    async def reload_config(self) -> dict:
        return await self._call("POST", "reload_config")

    # ---- trades ------------------------------------------------------------------------------------------------
    async def status(self) -> list:
        """Open trades, with their current rate and unrealised profit."""
        return await self._call("GET", "status")

    async def count(self) -> dict:
        return await self._call("GET", "count")

    async def trades(self, limit: int = 50, offset: int = 0, order_by_id: bool = True) -> dict:
        return await self._call("GET", "trades", params={"limit": limit, "offset": offset, "order_by_id": order_by_id})

    async def trade(self, trade_id: int) -> dict:
        return await self._call("GET", f"trade/{int(trade_id)}")

    async def forceexit(self, trade_id, ordertype: str | None = None, amount: float | None = None) -> dict:
        body: dict[str, Any] = {"tradeid": str(trade_id)}
        if ordertype:
            body["ordertype"] = ordertype
        if amount is not None:
            body["amount"] = amount
        return await self._call("POST", "forceexit", json=body, timeout_s=30.0)

    async def cancel_open_order(self, trade_id: int) -> dict:
        return await self._call("DELETE", f"trades/{int(trade_id)}/open-order", timeout_s=30.0)

    # ---- profit and statistics ---------------------------------------------------------------------------------
    async def profit(self) -> dict:
        return await self._call("GET", "profit")

    async def performance(self) -> list:
        return await self._call("GET", "performance")

    async def balance(self) -> dict:
        return await self._call("GET", "balance", timeout_s=20.0)

    async def daily(self, timescale: int = 7) -> dict:
        return await self._call("GET", "daily", params={"timescale": timescale})

    async def weekly(self, timescale: int = 4) -> dict:
        return await self._call("GET", "weekly", params={"timescale": timescale})

    async def monthly(self, timescale: int = 3) -> dict:
        return await self._call("GET", "monthly", params={"timescale": timescale})

    async def stats(self) -> dict:
        return await self._call("GET", "stats")

    async def entries(self) -> list:
        return await self._call("GET", "entries")

    async def exits(self) -> list:
        return await self._call("GET", "exits")

    # ---- pairs and locks ---------------------------------------------------------------------------------------
    async def whitelist(self) -> dict:
        return await self._call("GET", "whitelist")

    async def blacklist(self) -> dict:
        return await self._call("GET", "blacklist")

    async def blacklist_add(self, pair: str) -> dict:
        return await self._call("POST", "blacklist", json={"blacklist": [pair]})

    async def blacklist_delete(self, pairs: list[str]) -> dict:
        return await self._call("DELETE", "blacklist", params=[("pairs_to_delete", p) for p in pairs])

    async def locks(self) -> dict:
        return await self._call("GET", "locks")

    async def delete_lock(self, lock_id: int) -> dict:
        return await self._call("DELETE", f"locks/{int(lock_id)}")

    # ---- candles (trade mode: the bot's own analysed dataframe) ------------------------------------------------
    async def pair_candles(self, pair: str, timeframe: str, limit: int = 300) -> dict:
        return await self._call("GET", "pair_candles", params={"pair": pair, "timeframe": timeframe, "limit": limit},
                                timeout_s=30.0)

    # ---- webserver mode: strategies, data, backtesting ---------------------------------------------------------
    async def strategies(self) -> dict:
        return await self._call("GET", "strategies", timeout_s=30.0)

    async def strategy(self, name: str) -> dict:
        return await self._call("GET", f"strategy/{name}", timeout_s=30.0)

    async def exchanges(self) -> dict:
        return await self._call("GET", "exchanges", timeout_s=30.0)

    async def markets(self, exchange: str | None = None, quote: str | None = None, trading_mode: str | None = None,
                      margin_mode: str | None = None) -> dict:
        params = {"exchange": exchange, "quote": quote, "trading_mode": trading_mode}
        if margin_mode:
            params["margin_mode"] = margin_mode
        return await self._call("GET", "markets", params=params, timeout_s=60.0)

    async def available_pairs(self, timeframe: str | None = None, stake_currency: str | None = None) -> dict:
        return await self._call("GET", "available_pairs", params={"timeframe": timeframe,
                                                                  "stake_currency": stake_currency})

    async def pair_history(self, pair: str, timeframe: str, timerange: str, strategy: str | None = None) -> dict:
        return await self._call("GET", "pair_history", params={"pair": pair, "timeframe": timeframe,
                                                               "timerange": timerange, "strategy": strategy},
                                timeout_s=120.0)

    async def download_data(self, pairs: list[str], timeframes: list[str], days: int | None = None,
                            timerange: str | None = None, exchange: str | None = None,
                            trading_mode: str | None = None, margin_mode: str | None = None) -> dict:
        body: dict[str, Any] = {"pairs": pairs, "timeframes": timeframes, "erase": False}
        if timerange:
            body["timerange"] = timerange
        elif days:
            body["days"] = int(days)
        if exchange:
            body["exchange"] = exchange
        if trading_mode:
            body["trading_mode"] = trading_mode
        if margin_mode:
            body["margin_mode"] = margin_mode
        return await self._call("POST", "download_data", json=body)

    async def background_job(self, job_id: str) -> dict:
        return await self._call("GET", f"background/{job_id}")

    async def backtest_start(self, payload: dict) -> dict:
        return await self._call("POST", "backtest", json=payload, timeout_s=30.0)

    async def backtest_status(self) -> dict:
        return await self._call("GET", "backtest", timeout_s=60.0)

    async def backtest_abort(self) -> dict:
        return await self._call("GET", "backtest/abort")

    async def backtest_reset(self) -> dict:
        return await self._call("DELETE", "backtest")

    async def backtest_history(self) -> list:
        return await self._call("GET", "backtest/history", timeout_s=30.0)

    async def backtest_history_result(self, filename: str, strategy: str) -> dict:
        return await self._call("GET", "backtest/history/result", params={"filename": filename, "strategy": strategy},
                                timeout_s=60.0)
