"""The Freqtrade subsystem (freqkit/): settings validation, config generation, the safety rules, the store, the REST
client against a fake Freqtrade API, and the dashboard routes (token guard, live confirmation, error shapes).
No real Freqtrade process is started here."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from arbbot.config import FreqtradeCfg, load
from freqkit.client import FreqtradeApiError, FreqtradeClient, FreqtradeUnavailable
from freqkit.manager import (LIVE_ACK, LIVE_ACK_VAR, LIVE_CONFIRM, FreqtradeManager, build_freqtrade_config,
                             load_or_create_api_credentials, summarise_backtest, validate_bot_config)
from freqkit.store import DEFAULT_BOT_CONFIG, FreqStore

API = {"username": "u", "password": "p", "jwt_secret_key": "j" * 32, "ws_token": "w"}


class ValidationTests(unittest.TestCase):
    def test_defaults_pass_and_pairs_are_normalised(self):
        cfg = validate_bot_config({"pairs": "btc/usdt, eth/usdt\nbtc/usdt"})
        self.assertEqual(cfg["pairs"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(cfg["exchange"], "binance")

    def test_futures_pairs_get_the_settlement_suffix_and_isolated_margin(self):
        cfg = validate_bot_config({"trading_mode": "futures", "pairs": ["BTC/USDT"]})
        self.assertEqual((cfg["pairs"], cfg["margin_mode"]), (["BTC/USDT:USDT"], "isolated"))

    def test_bad_values_are_refused_with_the_field_named(self):
        for raw, word in (({"stake_amount": "lots"}, "stake_amount"), ({"max_open_trades": 0}, "max_open_trades"),
                          ({"pairs": ["BTCUSDT"]}, "pair"), ({"strategy": "../x"}, "strategy"),
                          ({"timeframe": "7m"}, "timeframe"), ({"exchange": "Binance!"}, "exchange"),
                          ({"pairs": ["BTC/EUR"]}, "stake currency"), ({"nope": 1}, "unknown")):
            with self.assertRaises(ValueError, msg=raw) as cm:
                validate_bot_config(raw)
            self.assertIn(word, str(cm.exception))

    def test_unlimited_stake_and_unlimited_trades(self):
        cfg = validate_bot_config({"stake_amount": "Unlimited", "max_open_trades": -1})
        self.assertEqual((cfg["stake_amount"], cfg["max_open_trades"]), ("unlimited", -1))


class ConfigBuildTests(unittest.TestCase):
    def test_dry_run_config_never_carries_keys(self):
        bot = validate_bot_config({})
        cfg = build_freqtrade_config(bot, api=API, listen_ip="127.0.0.1", listen_port=8080, dry_run=True,
                                     keys={"key": "K" * 20, "secret": "S" * 20}, db_url="sqlite:///x")
        self.assertTrue(cfg["dry_run"])
        self.assertEqual((cfg["exchange"]["key"], cfg["exchange"]["secret"]), ("", ""))
        self.assertFalse(cfg["force_entry_enable"])
        self.assertEqual(cfg["api_server"]["listen_ip_address"], "127.0.0.1")
        self.assertEqual(cfg["pairlists"], [{"method": "StaticPairList"}])
        self.assertEqual(cfg["strategy"], "SampleStrategy")

    def test_live_config_carries_keys_and_volume_pairlist(self):
        bot = validate_bot_config({"pairlist": "volume", "volume_assets": 7})
        cfg = build_freqtrade_config(bot, api=API, listen_ip="0.0.0.0", listen_port=8080, dry_run=False,
                                     keys={"key": "K" * 20, "secret": "S" * 20, "password": "pw"}, db_url=None)
        self.assertFalse(cfg["dry_run"])
        self.assertEqual(cfg["exchange"]["password"], "pw")
        self.assertEqual(cfg["pairlists"][0]["method"], "VolumePairList")
        self.assertEqual(cfg["pairlists"][0]["number_assets"], 7)
        self.assertNotIn("db_url", cfg)

    def test_webserver_config_is_static_even_for_a_volume_pairlist(self):
        bot = validate_bot_config({"pairlist": "volume"})
        cfg = build_freqtrade_config(bot, api=API, listen_ip="127.0.0.1", listen_port=8081, dry_run=True, keys=None,
                                     db_url=None, pairs=["SOL/USDT"], static_only=True)
        self.assertEqual((cfg["pairlists"], cfg["exchange"]["pair_whitelist"]), ([{"method": "StaticPairList"}], ["SOL/USDT"]))

    def test_credentials_are_generated_once_with_private_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "creds.json"
            a = load_or_create_api_credentials(path, {})
            b = load_or_create_api_credentials(path, {})
            self.assertEqual(a, b)
            self.assertGreaterEqual(len(a["jwt_secret_key"]), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            c = load_or_create_api_credentials(path, {"FREQTRADE_API_USERNAME": "me", "FREQTRADE_API_PASSWORD": "pw"})
            self.assertEqual((c["username"], c["password"]), ("me", "pw"))

    def test_backtest_summary_keeps_the_useful_parts(self):
        res = {"strategy": {"S": {"total_trades": 2, "profit_total": 0.1, "trades": [{"pair": "A/B", "profit_abs": 1}],
                                  "results_per_pair": [{"key": "A/B", "trades": 2}],
                                  "exit_reason_summary": [{"key": "roi", "trades": 2}], "daily_profit": [["d", 1]],
                                  "best_pair": {"key": "A/B", "profit_total": 0.1, "extra": 1}}}}
        s = summarise_backtest(res, "S")
        self.assertEqual((s["total_trades"], s["per_pair"][0]["key"], s["exit_reasons"][0]["exit_reason"]), (2, "A/B", "roi"))
        self.assertEqual(s["best_pair"], {"key": "A/B", "profit_total": 0.1, "profit_total_abs": None, "trades": None})
        self.assertEqual(s["trades"][0]["pair"], "A/B")


class StoreTests(unittest.TestCase):
    def test_settings_keys_events_and_snapshots(self):
        with tempfile.TemporaryDirectory() as d:
            st = FreqStore(Path(d) / "f.db")
            self.assertEqual(st.bot_config(), DEFAULT_BOT_CONFIG)
            self.assertEqual(st.mode(), "dry_run")
            st.save_bot_config(dict(DEFAULT_BOT_CONFIG, strategy="Other", junk=1))
            self.assertEqual(st.bot_config()["strategy"], "Other")
            st.set_exchange_keys("binance", "K" * 20, "S" * 20)
            self.assertEqual(st.exchange_keys("binance")["secret"], "S" * 20)
            hint = st.exchanges_with_keys()["binance"]["key_hint"]
            self.assertNotIn("S" * 8, json.dumps(st.exchanges_with_keys()))
            self.assertTrue(hint.startswith("KKKK"))
            self.assertTrue(st.delete_exchange_keys("binance"))
            st.add_event("warn", "x"); st.add_event("info", "y")
            self.assertEqual([e["msg"] for e in st.events(5)], ["y", "x"])
            st.add_perf_snapshot("dry_run", {"profit_all": 1.0}, ts=100.0)
            st.add_perf_snapshot("live", {"profit_all": 2.0}, ts=200.0)
            self.assertEqual([s["profit_all"] for s in st.perf_history(0)], [1.0, 2.0])
            self.assertEqual(st.last_perf_snapshot("live")["profit_all"], 2.0)
            self.assertEqual(stat.S_IMODE((Path(d) / "f.db").stat().st_mode), 0o600)
            st.close()


def fake_freqtrade_app(state: dict) -> web.Application:
    """Just enough of Freqtrade's API for the client and route tests (HTTP Basic u:p)."""
    async def auth(request, handler):
        if request.path.endswith("/ping"):
            return await handler(request)
        if request.headers.get("Authorization") != aiohttp.BasicAuth("u", "p").encode():
            return web.json_response({"detail": "Unauthorized"}, status=401)
        return await handler(request)
    app = web.Application(middlewares=[web.middleware(auth)])
    r = app.router
    r.add_get("/api/v1/ping", lambda rq: web.json_response({"status": "pong"}))
    r.add_get("/api/v1/version", lambda rq: web.json_response({"version": "2026.10"}))
    r.add_get("/api/v1/status", lambda rq: web.json_response(state.get("status", [])) if state.get("status")
              else web.json_response({"detail": "no active trade"}, status=404))
    r.add_get("/api/v1/profit", lambda rq: web.json_response({"profit_all_coin": 1.5, "trade_count": 3, "closed_trade_count": 2, "winrate": 0.5}))
    r.add_get("/api/v1/trades", lambda rq: web.json_response({"trades": [{"trade_id": 1, "pair": "BTC/USDT", "is_open": False, "secret_field": "x"}], "trades_count": 1, "offset": int(rq.query["offset"]), "total_trades": 1}))
    r.add_post("/api/v1/start", lambda rq: web.json_response({"status": "starting trader ..."}))
    r.add_post("/api/v1/forceexit", lambda rq: web.json_response({"result": "Created exit order"}))
    async def blacklist_delete(rq):
        return web.json_response({"removed": rq.query.getall("pairs_to_delete")})
    r.add_delete("/api/v1/blacklist", blacklist_delete)
    r.add_get("/api/v1/strategies", lambda rq: web.json_response({"strategies": ["SampleStrategy", "Other"]}))
    r.add_get("/api/v1/logs", lambda rq: web.json_response({"log_count": 1, "logs": [["t", 1, "freqtrade", "INFO", "hello"]]}))
    r.add_get("/api/v1/boom", lambda rq: web.json_response({"detail": [{"loc": ["query", "x"], "msg": "field required"}]}, status=422))
    return app


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = {}
        self.server = TestServer(fake_freqtrade_app(self.state))
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.api = FreqtradeClient(self.session, f"http://127.0.0.1:{self.server.port}", "u", "p")

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def test_calls_auth_and_errors(self):
        self.assertTrue(await self.api.ping())
        self.assertEqual((await self.api.version())["version"], "2026.10")
        self.assertEqual((await self.api.trades(10, 5))["offset"], 5)
        self.assertEqual((await self.api.blacklist_delete(["A/B", "C/D"]))["removed"], ["A/B", "C/D"])
        self.assertEqual((await self.api.forceexit("all"))["result"], "Created exit order")
        with self.assertRaises(FreqtradeApiError) as cm:
            await self.api.status()
        self.assertEqual((cm.exception.status, cm.exception.detail), (404, "no active trade"))
        with self.assertRaises(FreqtradeApiError) as cm:
            await self.api._call("GET", "boom")
        self.assertIn("x: field required", cm.exception.detail)
        bad = FreqtradeClient(self.session, self.api.base_url, "u", "wrong")
        with self.assertRaises(FreqtradeApiError) as cm:
            await bad.profit()
        self.assertEqual(cm.exception.status, 401)
        down = FreqtradeClient(self.session, "http://127.0.0.1:1", "u", "p")
        self.assertFalse(await down.ping())
        with self.assertRaises(FreqtradeUnavailable):
            await down.profit()


class ManagerSafetyTests(unittest.IsolatedAsyncioTestCase):
    """The manager without any process: mode arming, key handling, the dry-run rule, settings."""
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "bin").mkdir()
        self.session = aiohttp.ClientSession()
        self.environ = {}
        self.fm = self.make()

    def make(self, environ=None):
        cfg = FreqtradeCfg(enabled=True, executable="bin/freqtrade", user_data="ud", api_port=18080, webserver_port=18081)
        return FreqtradeManager(self.root, cfg, FreqStore(self.root / "f.db"), self.session,
                                environ=self.environ if environ is None else environ)

    async def asyncTearDown(self):
        await self.session.close()
        self.dir.cleanup()

    async def test_default_is_dry_run_and_live_needs_phrase_env_and_keys(self):
        fm = self.fm
        self.assertEqual(fm.effective_mode()[0], True)
        with self.assertRaises(ValueError) as cm:
            fm.set_mode("live", "yes please")
        self.assertIn(LIVE_CONFIRM, str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            fm.set_mode("live", LIVE_CONFIRM)
        self.assertIn(LIVE_ACK_VAR, str(cm.exception))
        self.environ[LIVE_ACK_VAR] = LIVE_ACK
        with self.assertRaises(ValueError) as cm:
            fm.set_mode("live", LIVE_CONFIRM)
        self.assertIn("no API keys", str(cm.exception))
        fm.save_exchange_keys("binance", "K" * 20, "S" * 20)
        self.assertEqual(fm.set_mode("live", LIVE_CONFIRM), "live")
        self.assertEqual(fm.effective_mode()[0], False)
        # the env ack vanishes (new shell): the next launch is dry-run again, whatever the store says
        del self.environ[LIVE_ACK_VAR]
        dry, why = fm.effective_mode()
        self.assertTrue(dry); self.assertIn(LIVE_ACK_VAR, why)
        # deleting the keys disarms live
        self.environ[LIVE_ACK_VAR] = LIVE_ACK
        fm.delete_exchange_keys("binance")
        self.assertEqual(fm.store.mode(), "dry_run")

    async def test_live_is_disarmed_on_a_restart_without_a_running_bot(self):
        self.environ[LIVE_ACK_VAR] = LIVE_ACK
        self.fm.save_exchange_keys("binance", "K" * 20, "S" * 20)
        self.fm.set_mode("live", LIVE_CONFIRM)
        fm2 = self.make()
        fm2.cfg.autostart_webserver = False
        task = asyncio.create_task(fm2.run())
        await asyncio.sleep(0.2)
        task.cancel()
        self.assertEqual(fm2.store.mode(), "dry_run")
        self.assertTrue(any("back to DRY-RUN" in e["msg"] for e in fm2.store.events(10)))

    async def test_missing_executable_is_a_clear_error_not_a_crash(self):
        with self.assertRaises(ValueError) as cm:
            await self.fm.start_trade()
        self.assertIn("not installed", str(cm.exception))
        self.assertEqual(self.fm.trade.state, "stopped")

    async def test_saving_settings_and_key_hygiene(self):
        cfg = self.fm.save_config({"strategy": "Other", "pairs": "sol/usdt"})
        self.assertEqual((cfg["strategy"], cfg["pairs"]), ("Other", ["SOL/USDT"]))
        self.assertEqual(self.fm.store.bot_config()["pairs"], ["SOL/USDT"])
        with self.assertRaises(ValueError):
            self.fm.save_exchange_keys("binance", "short", "S" * 20)
        self.fm.save_exchange_keys("binance", "K" * 20, "S" * 20)
        self.assertNotIn("S" * 20, json.dumps(self.fm.status()))
        self.assertIn("binance", self.fm.status()["keys"])

    async def test_backtest_request_validation(self):
        v = self.fm.validate_backtest_request({"timerange": "20260101-20260201", "pairs": "btc/usdt"})
        self.assertEqual((v["strategy"], v["pairs"], v["download"]), ("SampleStrategy", ["BTC/USDT"], True))
        for bad in ({"timerange": "2026-01-01"}, {"timerange": "-"}, {"timerange": "20260101-", "pairs": ["BTCUSDT"]},
                    {"timerange": "20260101-", "max_open_trades": 0}):
            with self.assertRaises(ValueError, msg=bad):
                self.fm.validate_backtest_request(bad)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard routes with the manager wired to a fake Freqtrade: token guard, shapes, errors."""
    async def asyncSetUp(self):
        from arbbot.server import Dashboard
        self.state = {}
        self.ft = TestServer(fake_freqtrade_app(self.state))
        await self.ft.start_server()
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.session = aiohttp.ClientSession()
        cfg = FreqtradeCfg(enabled=True, executable="missing", user_data="ud", api_port=self.ft.port, webserver_port=1)
        self.fm = FreqtradeManager(root, cfg, FreqStore(root / "f.db"), self.session, environ={})
        self.fm.creds.update(username="u", password="p")
        self.fm.trade.api = FreqtradeClient(self.session, f"http://127.0.0.1:{self.ft.port}", "u", "p")
        self.fm.trade.state, self.fm.trade.api_ok, self.fm.trade.dry_run = "running", True, True
        dcfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        engine = SimpleNamespace(swing=None, snapshot=lambda: {"ok": True})
        self.dash = Dashboard(engine, dcfg, Path("web"), freq=self.fm)
        server = TestServer(self.dash.make_app())
        await server.start_server()
        self.dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        self.client = TestClient(server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.ft.close()
        await self.session.close()
        self.dir.cleanup()

    def tok(self):
        return {"X-Token": self.dash.token, "Content-Type": "application/json"}

    async def test_page_and_overview(self):
        r = await self.client.get("/trading")
        body = await r.text()
        self.assertEqual(r.status, 200)
        self.assertIn(self.dash.token, body)
        self.assertIn("ENABLED = true", body)
        r = await self.client.get("/api/freqtrade/overview")
        d = await r.json()
        self.assertEqual((r.status, d["mode"], d["engine"]["state"], d["config"]["strategy"]), (200, "dry_run", "running", "SampleStrategy"))
        self.assertNotIn("password", json.dumps(d))

    async def test_trades_are_trimmed_and_tagged_dry_run(self):
        d = await (await self.client.get("/api/freqtrade/trades?limit=10&offset=0")).json()
        self.assertEqual(d["trades"][0]["pair"], "BTC/USDT")
        self.assertNotIn("secret_field", d["trades"][0])
        self.assertTrue(d["dry_run"])

    async def test_writes_need_the_token(self):
        for path, body in (("/api/freqtrade/bot", {"action": "start"}), ("/api/freqtrade/config", {"strategy": "X"}),
                           ("/api/freqtrade/mode", {"mode": "live"}), ("/api/freqtrade/forceexit", {"tradeid": 1}),
                           ("/api/freqtrade/engine", {"action": "stop"})):
            r = await self.client.post(path, json=body)
            self.assertEqual(r.status, 403, path)
            r = await self.client.post(path, json=body, headers={"X-Token": "wrong", "Content-Type": "application/json"})
            self.assertEqual(r.status, 403, path)

    async def test_bot_action_and_forceexit_through_the_api(self):
        r = await self.client.post("/api/freqtrade/bot", json={"action": "start"}, headers=self.tok())
        self.assertEqual((r.status, (await r.json())["result"]["status"]), (200, "starting trader ..."))
        r = await self.client.post("/api/freqtrade/bot", json={"action": "explode"}, headers=self.tok())
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/freqtrade/forceexit", json={"tradeid": "all"}, headers=self.tok())
        self.assertEqual((await r.json())["result"]["result"], "Created exit order")
        r = await self.client.post("/api/freqtrade/forceexit", json={"tradeid": "1; drop"}, headers=self.tok())
        self.assertEqual(r.status, 400)

    async def test_live_mode_is_refused_while_running_and_without_the_phrase(self):
        r = await self.client.post("/api/freqtrade/mode", json={"mode": "live", "confirm": LIVE_CONFIRM}, headers=self.tok())
        self.assertEqual(r.status, 400)
        self.assertIn("stop the trading bot", (await r.json())["error"])
        self.fm.trade.state = "stopped"
        r = await self.client.post("/api/freqtrade/mode", json={"mode": "live", "confirm": "nope"}, headers=self.tok())
        self.assertEqual(r.status, 400)
        self.assertIn(LIVE_CONFIRM, (await r.json())["error"])
        self.assertEqual(self.fm.store.mode(), "dry_run")

    async def test_exchange_keys_never_come_back(self):
        r = await self.client.post("/api/freqtrade/exchange", json={"exchange": "binance", "key": "K" * 20, "secret": "S" * 20}, headers=self.tok())
        d = await r.json()
        self.assertEqual(r.status, 200)
        self.assertNotIn("S" * 20, json.dumps(d))
        self.assertIn("binance", d["keys"])
        ov = await (await self.client.get("/api/freqtrade/overview")).json()
        self.assertNotIn("K" * 20, json.dumps(ov))

    async def test_config_validation_errors_are_400(self):
        r = await self.client.post("/api/freqtrade/config", json={"stake_amount": "lots"}, headers=self.tok())
        self.assertEqual(r.status, 400)
        self.assertIn("stake_amount", (await r.json())["error"])
        r = await self.client.post("/api/freqtrade/config", data="not json", headers=self.tok())
        self.assertEqual(r.status, 400)

    async def test_unreachable_freqtrade_is_503_not_a_crash(self):
        self.fm.trade.state = "stopped"
        r = await self.client.get("/api/freqtrade/trades")
        self.assertEqual(r.status, 503)
        self.assertIn("not reachable", (await r.json())["error"])
        r = await self.client.get("/api/freqtrade/overview")     # the overview always answers from the cache
        self.assertEqual(r.status, 200)

    async def test_logs_endpoint(self):
        d = await (await self.client.get("/api/freqtrade/logs?kind=trade&limit=50")).json()
        self.assertEqual(d["api"][0][4], "hello")
        self.assertEqual((await self.client.get("/api/freqtrade/logs?kind=other")).status, 400)


class ConfigFileTests(unittest.TestCase):
    def test_shipped_configs_load_and_only_the_paper_bot_manages_freqtrade(self):
        self.assertTrue(load("config.paper.toml").freqtrade.enabled)
        self.assertFalse(load("config.toml").freqtrade.enabled)
        self.assertFalse(load("config.testnet.toml").freqtrade.enabled)

    def test_port_clashes_are_refused(self):
        from arbbot.config import Config
        c = Config()
        c.freqtrade.enabled, c.freqtrade.api_port, c.freqtrade.webserver_port = True, 9000, 9000
        with self.assertRaises(ValueError):
            c.validate()
        c.freqtrade.webserver_port, c.server.port = 9001, 9000
        with self.assertRaises(ValueError):
            c.validate()


if __name__ == "__main__":
    unittest.main()


class StrategyFileTests(unittest.IsolatedAsyncioTestCase):
    """Strategy files: template, static checks, create / save / duplicate / delete with the in-use guards."""
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.session = aiohttp.ClientSession()
        cfg = FreqtradeCfg(enabled=True, executable="missing", user_data="ud", api_port=18090, webserver_port=18091)
        self.fm = FreqtradeManager(root, cfg, FreqStore(root / "f.db"), self.session, environ={})
        self.files = self.fm.strategy_files

    async def asyncTearDown(self):
        await self.session.close()
        self.dir.cleanup()

    async def test_create_edit_duplicate_delete(self):
        from freqkit.strategies import strategy_classes
        res = await self.fm.create_strategy("MyTrend", "15m", -0.08, {"0": 0.03, "60": 0.01}, "test")
        self.assertEqual((res["name"], res["file"], res["validated"]), ("MyTrend", "my_trend.py", False))
        code = self.files.read("my_trend.py")
        self.assertIn("class MyTrend(IStrategy)", code)
        self.assertIn('timeframe = "15m"', code)
        self.assertIn('"60": 0.01', code)
        self.assertEqual(strategy_classes(code), ["MyTrend"])
        rows = self.fm.list_strategies()["strategies"]
        self.assertEqual([r["name"] for r in rows], ["MyTrend"])
        self.assertEqual(rows[0]["timeframe"], "15m")
        # edit keeps working; the class must stay
        await self.fm.save_strategy("my_trend.py", code.replace("stoploss = -0.08", "stoploss = -0.05"), "MyTrend")
        self.assertIn("stoploss = -0.05", self.files.read("my_trend.py"))
        with self.assertRaises(ValueError) as cm:
            await self.fm.save_strategy("my_trend.py", code.replace("class MyTrend(", "class Other("), "MyTrend")
        self.assertIn("keep a class named MyTrend", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            await self.fm.save_strategy("my_trend.py", code + "\nthis is not python(", "MyTrend")
        self.assertIn("syntax error", str(cm.exception))
        self.assertIn("stoploss = -0.05", self.files.read("my_trend.py"))     # the bad save changed nothing
        # duplicate renames the class
        dup = await self.fm.create_strategy("MyTrendCopy", "5m", from_file="my_trend.py")
        self.assertIn("class MyTrendCopy(IStrategy)", self.files.read(dup["file"]))
        with self.assertRaises(ValueError):
            await self.fm.create_strategy("MyTrend", "5m")                    # a name may exist once
        # delete, with the guards
        self.fm.store.save_bot_config(dict(DEFAULT_BOT_CONFIG, strategy="MyTrend"))
        with self.assertRaises(ValueError) as cm:
            self.fm.delete_strategy("my_trend.py")
        self.assertIn("configured", str(cm.exception))
        self.fm.trade.state, self.fm.cache["config"] = "running", {"strategy": "MyTrendCopy"}
        with self.assertRaises(ValueError) as cm:
            self.fm.delete_strategy(dup["file"])
        self.assertIn("running bot", str(cm.exception))
        self.fm.trade.state = "stopped"
        self.fm.delete_strategy(dup["file"])
        self.assertEqual([r["name"] for r in self.fm.list_strategies()["strategies"]], ["MyTrend"])

    async def test_paths_and_names_are_checked(self):
        for bad in ("../x.py", "a/b.py", "x.txt", ".hidden.py", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.files.path_of(bad)
        for bad in ("lower", "1Abc", "Has Space", "A" * 70):
            with self.assertRaises(ValueError, msg=bad):
                await self.fm.create_strategy(bad, "5m")
        with self.assertRaises(ValueError):
            await self.fm.create_strategy("Ok", "7m")
        with self.assertRaises(ValueError):
            self.files.render_template("Ok", stoploss=0.5)
        with self.assertRaises(ValueError):
            self.fm.read_strategy_file("nope.py")

    async def test_routes(self):
        from arbbot.server import Dashboard
        dcfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        dash = Dashboard(SimpleNamespace(swing=None, snapshot=lambda: {}), dcfg, Path("web"), freq=self.fm)
        server = TestServer(dash.make_app())
        await server.start_server()
        dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        client = TestClient(server)
        await client.start_server()
        try:
            tok = {"X-Token": dash.token, "Content-Type": "application/json"}
            r = await client.post("/api/freqtrade/strategy", json={"action": "create", "name": "RouteStrat"})
            self.assertEqual(r.status, 403)
            r = await client.post("/api/freqtrade/strategy", json={"action": "create", "name": "RouteStrat"}, headers=tok)
            d = await r.json()
            self.assertEqual((r.status, d["name"], d["strategies"][0]["file"]), (200, "RouteStrat", "route_strat.py"))
            d = await (await client.get("/api/freqtrade/strategy/file?file=route_strat.py")).json()
            self.assertIn("class RouteStrat(IStrategy)", d["code"])
            self.assertEqual((await client.get("/api/freqtrade/strategy/file?file=../etc/passwd")).status, 400)
            r = await client.post("/api/freqtrade/strategy", json={"action": "save", "file": "route_strat.py", "code": "oops(", "name": "RouteStrat"}, headers=tok)
            self.assertEqual(r.status, 400)
            r = await client.post("/api/freqtrade/strategy", json={"action": "delete", "file": "route_strat.py"}, headers=tok)
            self.assertEqual((r.status, (await r.json())["deleted"]), (200, "route_strat.py"))
            self.assertEqual((await (await client.get("/api/freqtrade/strategies")).json())["strategies"], [])
        finally:
            await client.close()
