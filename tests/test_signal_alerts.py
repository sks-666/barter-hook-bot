"""Telegram alerts when a signal turns TRADE (arbbot/signal_alerts.py): only new TRADE coins on a watched timeframe,
nothing for coins already TRADE when watching starts or the bot restarts, no repeat while it stays TRADE or flickers."""
import tempfile
import unittest
from pathlib import Path

from arbbot.signal_alerts import SignalAlerts, message


def row(sym="SOLUSDT", bias="long", verdict="trade"):
    return dict(symbol=sym, bias=bias, verdict=verdict, mark=140.0, entry=139.2, to_entry_pct=-0.57, stop=136.5,
                tp1=144.1, rr1=1.8, target=148.0, rr=3.3, confidence=72, score=0.42, signal_type_name="Trend pullback",
                quality={"quality": 70, "regime": {"name": "TRENDING_UP"}, "premortem": {"text": "funding is high"}})


class Desk:
    def __init__(self):
        self.rows, self.asked = {}, []

    async def view(self, w, symbol=None):
        self.asked.append(w)
        return {"rows": list(self.rows.get(w, []))}


class Alerts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now, self.sent, self.desk = [1_000_000.0], [], Desk()
        self.path = Path(self.tmp.name) / "a.json"
        self.a = self.make()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def make(self):
        return SignalAlerts(self.desk, self.sent.append, self.path, clock=lambda: self.now[0], venue="REAL MONEY bot")

    async def test_a_coin_newly_trade_is_sent_once(self):
        self.desk.rows[3600] = [row("ETHUSDT")]                   # already TRADE when you switch the alert on
        now = await self.a.set_window(3600, True)
        self.assertEqual([r["symbol"] for r in now], ["ETHUSDT"])
        self.assertEqual(len(self.sent), 1)                       # only the "ON" reply, listing ETH
        self.assertIn("TRADE now: ▲ETH", self.sent[0])
        self.desk.rows[3600] = [row("ETHUSDT"), row("SOLUSDT"), row("BTCUSDT", verdict="no_trade"), row("XRPUSDT", bias="wait")]
        await self.a.step()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("🔔 TRADE · 1h · ▲ LONG SOLUSDT · REAL MONEY bot", self.sent[1])
        await self.a.step()                                       # still TRADE: no repeat
        self.assertEqual(len(self.sent), 2)

    async def test_a_flicker_within_a_timeframe_is_not_repeated(self):
        await self.a.set_window(900, True)
        self.desk.rows[900] = [row()]
        await self.a.step()
        self.assertEqual(len(self.sent), 2)
        self.desk.rows[900] = []                                  # leaves TRADE...
        await self.a.step()
        self.desk.rows[900] = [row()]                             # ...and comes back 2 minutes later
        self.now[0] += 120
        await self.a.step()
        self.assertEqual(len(self.sent), 2)
        self.desk.rows[900] = []
        await self.a.step()
        self.now[0] += 900                                        # a timeframe later: a new message
        self.desk.rows[900] = [row()]
        await self.a.step()
        self.assertEqual(len(self.sent), 3)
        self.desk.rows[900] = [row(bias="short")]                 # flips side within the timeframe: its own key
        await self.a.step()
        self.assertIn("▼ SHORT SOLUSDT", self.sent[-1])

    async def test_saved_and_quiet_after_a_restart(self):
        await self.a.set_window(3600, True)
        self.assertEqual(self.make().windows, [3600])             # survives a restart
        b = self.make()
        self.desk.rows[3600] = [row()]                            # TRADE when the bot comes back: noted, not sent
        await b.step()
        self.assertEqual(len(self.sent), 1)
        self.desk.rows[3600] = [row(), row("ADAUSDT")]
        await b.step()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("ADAUSDT", self.sent[-1])
        await b.set_window(3600, False)
        self.desk.asked.clear()
        await b.step()
        self.assertEqual(self.desk.asked, [])                     # nothing watched: nothing read
        for bad in ((123, True), (3600, "yes")):
            with self.assertRaises(ValueError):
                await b.set_window(*bad)

    def test_the_message(self):
        m = message(row(), 3600, "REAL MONEY bot")
        for part in ("▲ LONG SOLUSDT", "Trend pullback", "quality 70", "conf 72", "entry 139.2 (limit, -0.57% from now)",
                     "Stop 136.5 -1.94%", "TP1 144.1 +3.52% (1.8R, half closes)", "TP2 148 +6.32% (3.3R)",
                     "Pre-mortem: funding is high", "Not advice"):
            self.assertIn(part, m)


if __name__ == "__main__":
    unittest.main()
