"""The signal auto-trader (arbbot/signal_trader.py) against a fake testnet account: entries, sizing, the mainnet ->
testnet price move, limit expiry, exits (exchange stop/target, signal flip, maximum hold), results and every limit."""
import unittest

from arbbot.signal_trader import SignalTrader

NOW = 1_800_000_000.0


class Whales:
    @staticmethod
    def clamp_window(w):
        return float(w)


class Desk:
    def __init__(self, rows):
        self.whales, self.rows = Whales(), rows

    async def view(self, window_s, symbol=None):
        return {"rows": self.rows}


def row(sym="SOLUSDT", bias="long", mark=100.0, entry=100.0, stop=98.0, target=104.0, conf=70, coverage=1.0,
        whale_w=0.35, score=0.4):
    return dict(symbol=sym, bias=bias, mark=mark, entry=entry, stop=stop, target=target, confidence=conf,
                coverage=coverage, score=score, rr=abs(target - entry) / abs(entry - stop),
                factors=[{"key": "whales", "weight": whale_w, "score": 0.5}])


class Ticket:
    venue = "testnet"

    def __init__(self, px=101.0):
        self.px, self.pos, self.orders, self.sent, self.closed, self.fills = px, {}, [], [], [], {}
        self.kill, self.taken = "", set()
        self.open_orders = []
        self.leverage, self.lev_set, self.client_ids, self.lookups = {}, [], {}, []

    async def set_leverage(self, sym, lev):
        self.lev_set.append((sym, lev))
        self.leverage[sym] = lev
        return lev

    async def client_order_id(self, sym, oid):
        self.lookups.append(oid)
        return self.client_ids.get(oid, "web_abc")

    def blocked(self):
        return self.kill

    def owned(self):
        return set(self.taken)

    def arb_symbols(self):
        return set()

    async def refresh(self):
        self.open_orders = [{"orderId": o["id"], "symbol": o["symbol"]} for o in self.orders]

    async def _quote(self, sym, book=False):
        q = {"bid": self.px - 0.01, "ask": self.px + 0.01, "last": self.px}
        if book:                                                     # a deep book unless a test sets self.book
            q["book"] = getattr(self, "book", None) or {
                "bids": [[self.px * (1 - i / 100), 1000.0] for i in range(10)],
                "asks": [[self.px * (1 + i / 100), 1000.0] for i in range(10)]}
        return q

    async def place_order(self, req):
        self.sent.append(req)
        oid = len(self.sent)
        if req["type"] == "market":
            self.pos[req["symbol"]] = req["qty"] * (1 if req["side"] == "buy" else -1)
            return {"id": oid, "status": "filled", "qty": req["qty"], "filled_px": self.px}
        self.orders.append({"id": oid, "symbol": req["symbol"]})
        return {"id": oid, "status": "new", "qty": req["qty"]}

    async def cancel(self, oid):
        self.orders = [o for o in self.orders if o["id"] != oid]

    def position(self, sym):
        return self.pos.get(sym, 0.0)

    def position_info(self, sym):
        return {"amount": self.pos[sym], "entry_px": self.px, "pnl": 1.5} if self.pos.get(sym) else {}

    async def close_position(self, sym):
        self.closed.append(sym)
        self.pos[sym] = 0.0

    async def fills_between(self, sym, start, end):
        return self.fills.get(sym, [])


class Trader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = [NOW]
        self.ticket = Ticket()
        self.desk = Desk([row()])

    def make(self, **kw):
        return SignalTrader(self.desk, self.ticket, 3600, risk_usd=20, max_open=kw.pop("max_open", 3),
                            min_conf=60, max_usd=kw.pop("max_usd", 5000), daily_loss_usd=50,
                            clock=lambda: self.now[0], **kw)

    def test_testnet_only(self):
        self.ticket.venue = "mainnet"
        with self.assertRaises(RuntimeError):
            self.make()

    async def test_a_signal_at_the_price_is_a_market_entry_sized_by_risk_on_testnet_prices(self):
        t = self.make()
        await t.step()
        o = self.ticket.sent[0]
        self.assertEqual((o["symbol"], o["side"], o["type"]), ("SOLUSDT", "buy", "market"))
        k = 101.0 / 100.0                                     # testnet is 1% above mainnet: every level moves with it
        self.assertAlmostEqual(o["sl"], 98.0 * k)
        self.assertAlmostEqual(o["tp"], 104.0 * k)
        self.assertAlmostEqual(o["qty"] * (101.01 - 98.0 * k), 20.0)   # $20 lost at the stop
        self.assertIn("SOLUSDT", t.s["open"])
        self.assertTrue(t.holds("SOLUSDT"))

    async def test_size_is_capped_by_the_largest_position(self):
        t = self.make(max_usd=100)
        await t.step()
        self.assertAlmostEqual(self.ticket.sent[0]["qty"] * 101.01, 100.0)

    async def test_a_pullback_entry_is_a_limit_that_fills_or_expires(self):
        self.desk.rows = [row(entry=99.0, stop=97.0, target=103.0)]
        t = self.make()
        await t.step()
        o = self.ticket.sent[0]
        self.assertEqual(o["type"], "limit")
        self.assertAlmostEqual(o["price"], 99.0 * 1.01)
        self.assertIn("SOLUSDT", t.s["pending"])
        self.now[0] += t.entry_ttl_s + 1                     # 30 min for a 1h signal, no fill: cancelled ...
        await t.step()
        self.assertEqual([o["id"] for o in self.ticket.orders], [2])   # ... and replaced at the signal's current levels
        self.assertEqual(t.s["pending"]["SOLUSDT"]["order_id"], 2)
        self.desk.rows = []                                   # the signal is gone: the next expiry is not replaced
        self.now[0] += t.entry_ttl_s + 1
        await t.step()
        self.assertEqual((t.s["pending"], self.ticket.orders), ({}, []))
        # one that does fill becomes a managed position
        self.desk.rows = [row(entry=99.0, stop=97.0, target=103.0)]
        await t.step()
        self.ticket.pos["SOLUSDT"] = 1.0
        self.ticket.orders.clear()
        await t.step()
        self.assertIn("SOLUSDT", t.s["open"])

    async def test_the_exchange_stop_or_target_closes_it_and_the_result_comes_from_the_fills(self):
        t = self.make()
        await t.step()
        self.ticket.pos["SOLUSDT"] = 0.0                     # the stop fired on Binance
        self.now[0] += 60
        self.ticket.fills["SOLUSDT"] = [
            {"side": "BUY", "time": int(NOW * 1000) + 1, "price": "101", "qty": "6", "commission": "0.3", "realizedPnl": "0"},
            {"side": "SELL", "time": int(NOW * 1000) + 50_000, "price": "98", "qty": "6", "commission": "0.3",
             "realizedPnl": "-18"}]
        await t.step()                                       # sees it closed
        await t.step()                                       # reads the result from Binance's fills
        tr = t.s["trades"][-1]
        self.assertEqual(tr["why"], "stop or target")
        self.assertAlmostEqual(tr["pnl"], -18.6)
        self.assertIn("SOLUSDT", t.s["cooldown"])
        self.assertEqual(len(self.ticket.sent), 1)          # cooling down: no re-entry straight away

    async def test_a_flip_or_the_maximum_hold_closes_it(self):
        t = self.make()
        await t.step()
        self.desk.rows = [row(bias="short", entry=100, stop=102, target=96)]
        await t.step()
        self.assertEqual(self.ticket.closed, ["SOLUSDT"])
        self.assertEqual(t.s["trades"][-1]["why"], "signal turned SHORT")
        t2 = self.make()
        self.ticket.pos, self.ticket.closed, self.desk.rows = {}, [], [row(sym="ETHUSDT")]
        t2.s = {"enabled": True, "open": {}, "pending": {}, "cooldown": {}, "trades": []}
        await t2.step()
        self.desk.rows = []
        self.now[0] += t2.max_hold_s + 1
        await t2.step()
        self.assertEqual((self.ticket.closed, t2.s["trades"][-1]["why"]), (["ETHUSDT"], "held the maximum time"))

    async def test_what_it_never_enters(self):
        cases = [row(conf=50), row(coverage=0.4), row(whale_w=0.0), row(bias="wait"), row(target=101.0)]  # rr 0.5
        for r in cases:
            self.desk.rows = [r]
            t = self.make()
            await t.step()
            self.assertEqual(self.ticket.sent, [], r)
        self.desk.rows = [row()]
        self.ticket.px = 104.0                               # testnet 4% off mainnet: not the real market
        await self.make().step()
        self.assertEqual(self.ticket.sent, [])
        self.ticket.px, self.ticket.taken = 101.0, {"SOLUSDT"}  # held by the swing mirror or the ticket
        await self.make().step()
        self.assertEqual(self.ticket.sent, [])

    async def test_limits_switch_kill_file_and_daily_loss(self):
        self.ticket.kill = "the kill file KILL is present"
        t = self.make()
        await t.step()
        self.assertEqual((self.ticket.sent, t.view()["blocked"]), ([], "the kill file KILL is present"))
        self.ticket.kill = ""
        t.set_enabled(False)
        await t.step()
        self.assertEqual(self.ticket.sent, [])
        t.set_enabled(True)
        t.s["trades"].append(dict(symbol="X", side="buy", pnl=-60.0, closed_ms=int(NOW * 1000), opened_ms=0))
        await t.step()
        self.assertIn("daily loss", t.view()["blocked"])
        t.s["trades"].clear()
        self.desk.rows = [row(sym=s) for s in ("AUSDT", "BUSDT", "CUSDT")]
        await self.make(max_open=2).step()
        self.assertEqual(len(self.ticket.sent), 2)            # max open

    async def test_state_survives_a_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "st.json"
            t = self.make(path=p)
            await t.step()
            t.set_enabled(False)
            t2 = self.make(path=p)
            self.assertIn("SOLUSDT", t2.s["open"])
            self.assertFalse(t2.s["enabled"])

    def test_the_timeframe_can_be_changed_and_survives_a_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "st.json"
            t = self.make(path=p)
            t.set_window(900)
            self.assertEqual((t.window_s, t.view()["window_s"]), (900, 900))
            self.assertEqual(self.make(path=p).window_s, 900)     # the saved timeframe wins over the command line's 1h

    def test_only_its_timeframes_are_accepted(self):
        t = self.make()
        for bad in (0, 61, 7200, -300):
            with self.assertRaises(ValueError):
                t.set_window(bad)
        self.assertEqual(t.window_s, 3600)

    def test_the_most_open_trades_can_be_set_and_survives_a_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "st.json"
            t = self.make(path=p)
            t.set_max_open(7)
            self.assertEqual((t.max_open, t.view()["max_open"]), (7, 7))
            self.assertEqual(self.make(path=p).max_open, 7)        # the saved number wins over the command line's 3
            for bad in (0, 21, 2.5, True, "5"):
                with self.assertRaises(ValueError):
                    t.set_max_open(bad)
            self.assertEqual(t.max_open, 7)

    async def test_it_sets_low_leverage_on_a_coin_before_entering_once(self):
        t = self.make(leverage=3)
        await t.step()
        self.assertEqual(self.ticket.lev_set, [("SOLUSDT", 3)])
        self.assertEqual(t.view()["leverage"], 3)
        with self.assertRaises(ValueError):
            self.make(leverage=50)

    async def test_the_leverage_can_be_set_survives_a_restart_and_is_kept_per_trade(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "st.json"
            t = self.make(path=p)                                    # 5x by default
            await t.step()                                           # SOL opens at 5x
            self.assertEqual(t.s["open"]["SOLUSDT"]["leverage"], 5)
            t.set_leverage(2)
            self.assertEqual((t.leverage, t.view()["leverage_range"]), (2, [1, 20]))
            self.assertEqual(t.s["open"]["SOLUSDT"]["leverage"], 5)  # the open trade keeps its own
            self.assertEqual(self.make(path=p).leverage, 2)          # saved: wins over the command line's
            for bad in (0, 21, 2.5, True, "3"):
                with self.assertRaises(ValueError):
                    t.set_leverage(bad)
            self.ticket.pos["SOLUSDT"] = 0.0                         # closed on the exchange
            self.now[0] += 60
            await t.step()
            self.assertEqual(t.s["trades"][-1]["leverage"], 5)       # the closed trade remembers it
            self.desk.rows = [row("ETHUSDT")]
            self.now[0] += 3600
            await t.step()
            self.assertEqual((self.ticket.lev_set[-1], t.s["open"]["ETHUSDT"]["leverage"]), (("ETHUSDT", 2), 2))

    async def _close_sol(self, t, closing_order_id, pnl="-944"):
        await t.step()
        self.ticket.pos["SOLUSDT"] = 0.0
        self.now[0] += 60
        self.ticket.fills["SOLUSDT"] = [
            {"side": "BUY", "time": int(NOW * 1000) + 1, "price": "101", "qty": "6", "commission": "0.3", "realizedPnl": "0",
             "orderId": 1},
            {"side": "SELL", "time": int(NOW * 1000) + 50_000, "price": "50", "qty": "6", "commission": "0",
             "realizedPnl": pnl, "orderId": closing_order_id}]
        await t.step()
        await t.step()
        return t.s["trades"][-1]

    async def test_an_auto_deleveraging_close_is_marked_and_left_out_of_the_daily_limit(self):
        self.ticket.client_ids[77] = "adl_autoclose"
        t = self.make()                                              # daily loss limit 50
        tr = await self._close_sol(t, 77)
        self.assertEqual((tr["forced"], tr["why"]), ("adl", "closed by Binance: auto-deleveraging"))
        self.assertAlmostEqual(tr["pnl"], -944.3)
        self.assertEqual(t.realized_today(), 0)                      # not a loss the strategy made
        self.assertEqual(t.can_enter(), "")
        st = t.stats()
        self.assertAlmostEqual(st["realized"], -944.3)               # still in the totals
        self.assertEqual(st["adl_count"], 1)
        self.assertEqual(self.ticket.lookups, [77])                  # checked once, not again on later passes
        await t.step()
        self.assertEqual(self.ticket.lookups, [77])

    async def test_a_liquidation_or_an_ordinary_close_still_counts(self):
        self.ticket.client_ids[78] = "autoclose-1794672"
        t = self.make()
        tr = await self._close_sol(t, 78, pnl="-60")
        self.assertEqual(tr["forced"], "liquidation")
        self.assertIn("daily loss limit", t.can_enter())

    async def test_trades_closed_before_the_check_existed_are_rechecked(self):
        self.ticket.client_ids[77] = "adl_autoclose"
        t = self.make()
        t.s["trades"].append(dict(symbol="SOLUSDT", side="buy", pnl=-944.3, closed_ms=int(NOW * 1000) + 50_000,
                                  opened_ms=int(NOW * 1000), why="stop or target"))
        self.ticket.fills["SOLUSDT"] = [{"side": "SELL", "time": int(NOW * 1000) + 50_000, "price": "50", "qty": "6",
                                         "realizedPnl": "-944", "orderId": 77}]
        self.assertIn("daily loss limit", t.can_enter())
        await t.step()
        self.assertEqual(t.s["trades"][0]["forced"], "adl")
        self.assertEqual(t.realized_today(), 0)

    async def test_its_stop_and_target_trigger_on_the_mark_price(self):
        await self.make().step()
        self.assertEqual(self.ticket.sent[0]["trigger"], "mark")

    async def test_a_book_too_thin_for_the_stop_skips_the_coin(self):
        """A long's stop sells into the bids: with almost nothing between the price and the stop it would fill far away."""
        t = self.make()                                              # long SOL: entry ~101, stop ~98.98, ~9.9 coins
        self.ticket.book = {"bids": [[100.99, 3.0], [99.5, 3.0], [90.0, 5000.0]], "asks": [[101.01, 1000.0]]}
        await t.step()
        self.assertEqual(self.ticket.sent, [])                       # 6 coins within reach of the stop < 2x 9.9
        self.assertIn("SOLUSDT", t.s["cooldown"])                    # tried again after the cooldown
        self.ticket.book = {"bids": [[100.99, 10.0], [98.5, 10.0]], "asks": [[101.01, 1000.0]]}   # 98.5 ≥ stop - half a risk
        t.s["cooldown"] = {}
        await t.step()
        self.assertEqual(len(self.ticket.sent), 1)

    def test_the_exit_depth_is_read_on_the_side_the_stop_closes_into(self):
        thin_asks = {"bids": [[99, 1e6]], "asks": [[101, 1.0], [150, 1e6]]}
        with self.assertRaises(ValueError):                          # a short's stop buys: the asks must be deep
            SignalTrader._check_exit_depth(thin_asks, long=False, ref=100, stop=102, qty=10)
        SignalTrader._check_exit_depth(thin_asks, long=True, ref=100, stop=98, qty=10)   # a long's stop sells: bids
        with self.assertRaises(ValueError):
            SignalTrader._check_exit_depth(None, long=True, ref=100, stop=98, qty=10)

    def test_the_risk_limits_can_be_set_and_survive_a_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "st.json"
            t = self.make(path=p)
            t.set_risk_usd(35)
            t.set_daily_loss_usd(250.5)
            t2 = self.make(path=p)                                     # the saved values win over the command line's
            self.assertEqual((t2.risk_usd, t2.daily_loss_usd), (35.0, 250.5))
            self.assertEqual((t2.view()["risk_usd"], t2.view()["risk_range"]), (35.0, [1.0, 500.0]))
            for bad in (0, 0.5, 501, -5, True, "30", None):
                with self.assertRaises(ValueError):
                    t.set_risk_usd(bad)
            for bad in (4, 5001, False):
                with self.assertRaises(ValueError):
                    t.set_daily_loss_usd(bad)
            self.assertEqual((t.risk_usd, t.daily_loss_usd), (35.0, 250.5))

    async def test_a_new_risk_sizes_the_next_trade(self):
        t = self.make()
        t.set_risk_usd(40)
        await t.step()
        o = self.ticket.sent[0]                                        # stop 2% away (testnet 101 → 98.98): ~$40 at risk
        self.assertAlmostEqual(o["qty"] * (101.01 - o["sl"]), 40, delta=0.5)

    async def test_raising_the_daily_loss_limit_allows_entries_again(self):
        t = self.make()
        t.s["trades"].append(dict(symbol="XRPUSDT", side="buy", pnl=-60.0, closed_ms=int(NOW * 1000), opened_ms=0))
        await t.step()
        self.assertIn("daily loss limit (50 USDT)", t.can_enter())
        self.assertEqual(self.ticket.sent, [])
        t.set_daily_loss_usd(100)
        await t.step()
        self.assertEqual(len(self.ticket.sent), 1)

    async def test_more_open_trades_lets_more_coins_in_and_fewer_closes_nothing(self):
        self.desk.rows = [row("SOLUSDT"), row("ETHUSDT", conf=80), row("XRPUSDT", conf=75)]
        t = self.make(max_open=1)
        await t.step()
        self.assertEqual(list(t.s["open"]), ["ETHUSDT"])               # the most confident one only
        t.set_max_open(3)
        await t.step()
        self.assertEqual(sorted(t.s["open"]), ["ETHUSDT", "SOLUSDT", "XRPUSDT"])
        t.set_max_open(1)
        await t.step()
        self.assertEqual((len(t.s["open"]), self.ticket.closed), (3, []))   # lowering it closes nothing
        self.assertIn("1 positions / entries open", t.can_enter())

    async def test_an_open_position_keeps_the_maximum_hold_of_its_own_timeframe(self):
        t = self.make()
        await t.step()                                               # opened on the 1h signal: max hold 2 h
        self.assertIn("SOLUSDT", t.s["open"])
        t.set_window(86_400)                                         # 1d: max hold 2 days for new trades
        self.desk.rows = []
        self.now[0] += 2 * 3600 + 1
        await t.step()
        self.assertEqual((self.ticket.closed, t.s["trades"][-1]["why"]), (["SOLUSDT"], "held the maximum time"))


if __name__ == "__main__":
    unittest.main()
