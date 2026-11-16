"""Signal types (arbbot/signal_types.py): the built-in ones, custom types from the condition menu, classification order,
on/off, saving, the quality check using each type's rules, the auto-trader's per-type risk and results, the API and
Telegram's /types."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arbbot import quality as q
from arbbot import signal_types as st
from arbbot.signal_types import TypeBook
from tests.test_quality import candles, kw, trend  # noqa: F401


def custom(**over):
    t = {"name": "Big volume", "when": [{"k": "rvol", "op": ">=", "v": 2.5}], "require": [],
         "entry": "price", "stop_atr": 1.5, "tp1_r": 2.0, "tp2_r": 4.0, "min_quality": 40, "risk_mult": 0.5}
    t.update(over)
    return t


class Book(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "types.json"
        self.sent = []
        self.book = TypeBook(self.path, notify=self.sent.append)

    def tearDown(self):
        self.dir.cleanup()

    def test_built_in_types_in_order_ending_with_the_catch_all(self):
        keys = [t["key"] for t in self.book.resolved()]
        self.assertEqual(keys, ["funding_squeeze", "sweep_reversal", "breakout", "trend_pullback", "candle_reversal",
                                "mean_reversion"])
        self.assertEqual(self.book.resolved()[-1]["when"], [])

    def test_classification_takes_the_first_match(self):
        f = {"funding": 0.01, "sweep": False, "breakout": True, "htf": "with"}
        self.assertEqual(st.classify(f, self.book.resolved())["key"], "breakout")       # before trend pullback
        f.update(breakout=False)
        self.assertEqual(st.classify(f, self.book.resolved())["key"], "trend_pullback")
        f.update(htf="against")
        self.assertEqual(st.classify(f, self.book.resolved())["key"], "mean_reversion")  # the catch-all
        f.update(funding=-0.05)
        self.assertEqual(st.classify(f, self.book.resolved())["key"], "funding_squeeze")
        f.update(funding=None)                                         # unknown: a condition on it fails
        self.assertEqual(st.classify(f, self.book.resolved())["key"], "mean_reversion")

    def test_unmet_says_what_and_the_value_now(self):
        mr = self.book.get("mean_reversion")
        self.assertEqual(st.unmet({"rsi": 30.0, "vwap_dist": -1.0}, mr), [])
        why = st.unmet({"rsi": 52.0, "vwap_dist": None}, mr)
        self.assertEqual(len(why), 2)
        self.assertIn("RSI read for the trade's side", why[0])
        self.assertIn("(now 52)", why[0])
        self.assertIn("(now unknown)", why[1])

    def test_a_custom_type_is_checked_first_saved_and_survives_a_restart(self):
        t = self.book.save(custom())
        self.assertEqual(t["key"], "custom_big_volume")
        self.assertEqual(self.book.resolved()[0]["key"], "custom_big_volume")
        self.assertEqual(st.classify({"rvol": 3.0, "breakout": True}, self.book.resolved())["key"], "custom_big_volume")
        again = TypeBook(self.path)
        self.assertEqual(again.get("custom_big_volume")["risk_mult"], 0.5)
        self.assertIn("Big volume saved", self.sent[-1])

    def test_editing_deleting_and_switching(self):
        self.book.save(custom())
        self.book.save(custom(key="custom_big_volume", min_quality=70))       # same key: replaced
        self.assertEqual(len(self.book.s["custom"]), 1)
        self.assertEqual(self.book.get("custom_big_volume")["min_quality"], 70)
        with self.assertRaises(ValueError):
            self.book.save(custom())                                   # a NEW one with the same name
        self.book.set_enabled("custom_big_volume", False)              # a switched-off custom type is skipped
        self.assertNotIn("custom_big_volume", [t["key"] for t in self.book.resolved()])
        self.book.set_enabled("breakout", False)                       # a switched-off built-in still classifies
        bo = next(t for t in self.book.resolved() if t["key"] == "breakout")
        self.assertFalse(bo["enabled"])
        self.assertEqual(TypeBook(self.path).s["off"], ["breakout"])
        self.book.delete("custom_big_volume")
        self.assertIsNone(self.book.get("custom_big_volume"))
        for bad in (lambda: self.book.delete("breakout"), lambda: self.book.save(dict(custom(), key="breakout")),
                    lambda: self.book.set_enabled("nope", True)):
            with self.assertRaises(ValueError):
                bad()

    def test_validation_refuses_anything_off_the_menu(self):
        for bad, why in ((custom(name=""), "name"), (custom(when=[]), "at least one"),
                         (custom(when=[{"k": "__import__", "op": ">=", "v": 1}]), "unknown condition"),
                         (custom(when=[{"k": "rvol", "op": "is", "v": 1}]), "takes"),
                         (custom(when=[{"k": "rsi", "op": ">=", "v": 150}]), "0 to 100"),
                         (custom(when=[{"k": "regime", "op": "is", "v": "MOON"}]), "one of"),
                         (custom(entry="anywhere"), "entry"), (custom(tp1_r=3.0, tp2_r=2.0), "TP2"),
                         (custom(risk_mult=5), "Risk ×"), (custom(when=[{"k": "bos", "op": "is"}] * 9), "at most")):
            with self.assertRaises(ValueError) as e:
                st.validate(bad)
            self.assertIn(why, str(e.exception))

    def test_find_by_key_or_name(self):
        self.book.save(custom())
        self.assertEqual(self.book.find("breakout")["key"], "breakout")
        self.assertEqual(self.book.find("trend")["key"], "trend_pullback")
        self.assertEqual(self.book.find("Funding squeeze")["key"], "funding_squeeze")
        self.assertEqual(self.book.find("big volume")["key"], "custom_big_volume")
        self.assertIsNone(self.book.find("zzz"))


class TypeRules(unittest.TestCase):
    def test_the_type_sets_targets_and_its_requirements(self):
        r = q.assess(**kw())
        self.assertEqual(r["type"]["key"], "breakout")                 # the fixture's trigger breaks the range
        self.assertEqual(r["features"]["breakout"], True)
        book = TypeBook(None)
        book.save(custom(when=[{"k": "breakout", "op": "is"}], tp1_r=2.5, tp2_r=4.5))
        r = q.assess(**kw(types=book.resolved()))
        self.assertEqual(r["type"]["key"], "custom_big_volume")
        self.assertGreaterEqual(r["levels"]["rr1"], 2.5)
        self.assertGreaterEqual(r["levels"]["rr2"], 4.5)
        self.assertEqual(r["type"]["risk_mult"], 0.5)
        book.save(custom(key="custom_big_volume", when=[{"k": "breakout", "op": "is"}],
                         require=[{"k": "rsi", "op": "<=", "v": 10}]))
        r = q.assess(**kw(types=book.resolved()))
        self.assertEqual(r["verdict"], "no_trade")
        self.assertTrue(any(g.startswith("Big volume needs RSI") for g in r["gates"]), r["gates"])

    def test_a_switched_off_built_in_blocks_its_ideas(self):
        book = TypeBook(None)
        book.set_enabled("breakout", False)
        r = q.assess(**kw(types=book.resolved()))
        self.assertEqual(r["type"]["key"], "breakout")
        self.assertIn("Breakout trades are switched off", r["gates"])

    def test_a_short_against_the_uptrend_is_mean_reversion_with_its_higher_bar(self):
        r = q.assess(**kw(side=-1))
        self.assertEqual(r["type"]["key"], "mean_reversion")
        self.assertEqual(r["features"]["htf"], "against")
        self.assertIn(f"quality {r['quality']}/100 (Mean reversion needs 60)", r["gates"])

    def test_entry_styles(self):
        up = q.closed(candles(trend(301, 100, 0.2)))
        a = q.atr(up)
        e21 = q.ema([k[4] for k in up], 21)[-1]
        mark = e21 + a                                                 # 1 ATR above the EMA: a pullback entry
        self.assertAlmostEqual(q.type_entry("ema21", long=True, mark=mark, whale_entry=mark, ltf=up, a=a, level=None), e21)
        self.assertEqual(q.type_entry("ema21", long=True, mark=e21 + 3 * a, whale_entry=0, ltf=up, a=a, level=None), e21 + 3 * a)
        self.assertAlmostEqual(q.type_entry("retest", long=True, mark=100.5, whale_entry=0, ltf=up, a=1.0, level=100.0), 100.1)
        self.assertEqual(q.type_entry("retest", long=True, mark=105, whale_entry=0, ltf=up, a=1.0, level=100.0), 105)
        self.assertEqual(q.type_entry("whales", long=True, mark=105, whale_entry=104, ltf=up, a=1.0, level=None), 104)
        self.assertEqual(q.type_entry("price", long=False, mark=105, whale_entry=106, ltf=up, a=1.0, level=None), 105)


class TraderByType(unittest.IsolatedAsyncioTestCase):
    async def test_risk_multiplier_and_results_per_type(self):
        from arbbot.signal_trader import SignalTrader
        from tests.test_signal_trader import NOW, Desk, Ticket, row
        r = row()
        r.update(signal_type="custom_x", signal_type_name="X", quality={"quality": 70, "type": {"risk_mult": 0.5}})
        ticket, now = Ticket(), [NOW]
        t = SignalTrader(Desk([r]), ticket, 3600, risk_usd=20, max_usd=5000, clock=lambda: now[0])
        await t.step()
        o = ticket.sent[0]
        self.assertAlmostEqual(o["qty"] * (101.01 - 98.0 * 1.01), 10.0)     # $20 x 0.5 at the stop
        self.assertEqual(t.s["open"]["SOLUSDT"]["type"], "custom_x")
        ticket.pos["SOLUSDT"] = 0.0                                     # the stop filled
        ticket.fills["SOLUSDT"] = [{"side": "SELL", "time": int(NOW * 1000) + 5, "qty": "1", "price": "99",
                                    "realizedPnl": "-10", "commission": "0", "orderId": 9}]
        now[0] += 60
        await t.step()
        await t.step()
        s = t.stats_by_type()["custom_x"]
        self.assertEqual((s["name"], s["closed"], s["wins"], s["pnl"]), ("X", 1, 0, -10.0))
        self.assertAlmostEqual(s["avg_r"], -1.0, places=1)
        self.assertIn("stats_by_type", t.view())


class Api(unittest.IsolatedAsyncioTestCase):
    async def test_read_save_toggle_delete_with_the_token(self):
        from aiohttp.test_utils import TestClient, TestServer
        from arbbot.server import Dashboard
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        dash = Dashboard(SimpleNamespace(swing=None, snapshot=lambda: {}), cfg, Path("web"),
                         signal_desk=SimpleNamespace(types=TypeBook(None)))
        server = TestServer(dash.make_app())
        await server.start_server()
        dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        dash.cfg.server.port = server.port
        h = {"Host": f"127.0.0.1:{server.port}"}
        async with TestClient(server) as c:
            r = await c.get("/api/signal-types", headers=h)
            d = await r.json()
            self.assertEqual(r.status, 200)
            self.assertEqual(len(d["types"]), 6)
            self.assertIn("rvol", d["fields"])
            self.assertIsNone(d["stats"])                               # no auto-trader in this bot
            r = await c.post("/api/signal-types", json={"action": "save", "type": custom()}, headers=h)
            self.assertEqual(r.status, 403)                             # no token: refused
            ht = dict(h, **{"X-Token": dash.token})
            r = await c.post("/api/signal-types", json={"action": "save", "type": custom()}, headers=ht)
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())["types"][0]["key"], "custom_big_volume")
            r = await c.post("/api/signal-types", json={"action": "save", "type": custom(entry="x")}, headers=ht)
            self.assertEqual(r.status, 400)
            self.assertIn("entry", (await r.json())["error"])
            r = await c.post("/api/signal-types", json={"action": "toggle", "key": "breakout", "enabled": False}, headers=ht)
            self.assertFalse(next(t for t in (await r.json())["types"] if t["key"] == "breakout")["enabled"])
            r = await c.post("/api/signal-types", json={"action": "delete", "key": "custom_big_volume"}, headers=ht)
            self.assertEqual(len((await r.json())["types"]), 6)


class Telegram(unittest.IsolatedAsyncioTestCase):
    async def test_types_lists_and_switches(self):
        from arbbot.commands import CommandHandler
        from tests.test_commands import build
        h = CommandHandler(build(trading=False), "TESTNET")
        h.signal_desk = SimpleNamespace(types=TypeBook(None))
        text = await h.reply("/types")
        self.assertIn("Breakout", text)
        self.assertIn("✅ Mean reversion · no trades yet", text)
        self.assertIn("now OFF", await h.reply("/types off breakout"))
        self.assertIn("⛔ Breakout", await h.reply("/types"))
        self.assertIn("now ON", await h.reply("/types on breakout"))
        self.assertIn("No signal type", await h.reply("/types off zzz"))


if __name__ == "__main__":
    unittest.main()
