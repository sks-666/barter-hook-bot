"""Fixes from the code review: no blind exit retries after an unhedged close, the real-money ticket's caps, lost spot
order replies, clock re-sync on -1021, the open-position loss halt, the fill-based entry basis and the funding rate."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from arbbot.binance_client import BinanceClient, BinanceError
from arbbot.config import Config
from arbbot.executor import UnhedgedError
from arbbot.live import LiveExecutor
from arbbot.models import ClosedTrade, Market, Position, Signal
from arbbot.portfolio import Portfolio, Store
from arbbot.risk import Risk
from arbbot.testnet_ticket import TestnetTicket
from tests.test_core import set_quotes, small_cfg
from tests.test_live_orders import FakeExchange, market
from tests.test_model import engine, warm
from tests.test_testnet_ticket import FakeTestnet


async def open_paper(eng, m, t):
    set_quotes(m, 100.0, 2.0, t)
    pos = await eng.executor.open(m, eng.scanner.evaluate(m, t), 100.0, t)
    pos.manual = True                                    # only the protective exits apply: nothing closes it by itself
    eng.pf.add(pos)
    return pos


class StuckExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unhedged_close_is_not_retried_every_tick(self):
        eng, m, pf, cfg = engine(trading=False)
        t = await warm(eng, m)
        await open_paper(eng, m, t)
        calls = []

        async def fails(*a, **k):
            calls.append(a)
            raise UnhedgedError("ETHUSDT: spot sold but futures buy-back failed; SHORT PERP STILL OPEN")
        eng.executor.close = fails
        eng.request_flatten()
        for i in range(3):
            set_quotes(m, 100.0, 2.0, t + 1 + i)
            await eng.tick(now=t + 1 + i)
        self.assertEqual(len(calls), 1)                  # once, never again on a size that is no longer true
        self.assertIn("ETHUSDT", eng.stuck)
        self.assertTrue(eng.risk.fault)
        self.assertFalse(eng.flatten_requested)          # nothing else is left to flatten
        self.assertTrue(eng.snapshot()["positions"][0]["stuck"])

    async def test_reconcile_marks_disputed_positions(self):
        class C:
            async def get(self, venue, path, params=None, signed=False):
                if "positionRisk" in path:
                    return [{"symbol": "ETHUSDT", "positionAmt": "-1.0"}]
                return {"balances": [{"asset": "ETH", "free": "2", "locked": "0"}]}
        ex = LiveExecutor(C(), Config())
        pos = Position("ETHUSDT", "ETHUSDT", 1.0, 0.0, 200.0, 2.0, 2.0, 100.0, 100.0, 50.0, 4.0, live=True)
        await ex.reconcile({"ETHUSDT": pos}, {"ETHUSDT": Market("ETHUSDT", "ETHUSDT", "ETH")})
        self.assertEqual(ex.mismatched, {"ETHUSDT"})


class TicketCapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ex = FakeTestnet()
        self.ex.hosts = {"fapi": "https://fapi.binance.com", "spot": "https://api.binance.com"}
        self.t = TestnetTicket(self.ex, None, real_money=True, max_order_usd=300)

    async def test_a_sell_limit_priced_far_below_the_bid_is_capped_at_the_bid(self):
        with self.assertRaises(ValueError) as cm:        # 1 BTC at a mistyped price of 100: really ~80,000 USDT
            await self.t.place_order(dict(symbol="BTCUSDT", side="sell", type="limit", price=100, qty=1))
        self.assertIn("cap", str(cm.exception))
        self.assertEqual(self.ex.posts, [])

    async def test_a_usdt_size_is_converted_at_the_bid_not_the_mistyped_price(self):
        await self.t.place_order(dict(symbol="BTCUSDT", side="sell", type="limit", price=60000, size_usdt=200))
        self.assertEqual(self.ex.posts[-1]["quantity"], "0.002")   # 200 / 79,999.9 (it fills at the bid), not 200 / 60,000

    async def test_the_total_cap_counts_every_position_of_the_ticket(self):
        self.assertEqual(self.t.max_total_usd, 900)               # 3x the order cap by default
        for _ in range(3):
            await self.t.place_order(dict(symbol="BTCUSDT", side="buy", type="market", qty=0.003))   # ~240 each
        with self.assertRaises(ValueError) as cm:
            await self.t.place_order(dict(symbol="BTCUSDT", side="buy", type="market", qty=0.003))
        self.assertIn("total cap", str(cm.exception))
        await self.t.place_order(dict(symbol="BTCUSDT", side="sell", type="market", qty=0.003))  # reducing: allowed
        await self.t.close_position("BTCUSDT")                   # and closing always is

    async def test_the_testnet_has_no_total_cap(self):
        self.assertIsNone(TestnetTicket(FakeTestnet(), None).max_total_usd)


class LostSpotReply(FakeExchange):
    """The spot order reaches Binance but its reply is lost; GET /api/v3/order tells what really happened."""

    def __init__(self, filled=True, lookup_error=None, **kw):
        super().__init__(reply="avg", **kw)
        self.filled, self.lookup_error, self.cids = filled, lookup_error, []

    async def post(self, venue, path, params=None, signed=True):
        if path == "/api/v3/order" and not self.cids:
            self.calls.append(("POST", path, dict(params)))
            self.cids.append(params["newClientOrderId"])
            self.qty = params["quantity"]
            raise asyncio.TimeoutError()
        return await super().post(venue, path, params, signed)

    async def get(self, venue, path, params=None, signed=False):
        if path == "/api/v3/order":
            self.calls.append(("GET", path, dict(params)))
            if self.lookup_error:
                raise self.lookup_error
            assert params["origClientOrderId"] == self.cids[0]
            if not self.filled:
                raise BinanceError(400, -2013, "Order does not exist.", path)
            return {"orderId": 9, "status": "FILLED", "executedQty": self.qty,
                    "cummulativeQuoteQty": str(100.2 * float(self.qty))}
        if path == "/api/v3/myTrades":
            return [{"price": "100.2", "qty": self.qty, "commission": "0.001", "commissionAsset": "ETH"}]
        return await super().get(venue, path, params, signed)

    def fut_buys(self):
        return [c for c in self.calls if c[1] == "/fapi/v1/order" and c[2].get("side") == "BUY"]


async def open_with(ex):
    return await LiveExecutor(ex, Config()).open(market(), Signal("ETHUSDT", 0.0), 100.0, 0.0)


class LostSpotReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_filled_despite_the_lost_reply_stays_hedged_with_the_real_fee(self):
        ex = LostSpotReply(filled=True)
        with self.assertLogs("live", "ERROR"):
            pos = await open_with(ex)
        self.assertEqual(ex.fut_buys(), [])                       # the short was NOT bought back
        self.assertAlmostEqual(pos.spot_qty, float(ex.qty) - 0.001)

    async def test_never_placed_is_a_plain_failure_and_the_short_is_unwound(self):
        ex = LostSpotReply(filled=False)
        with self.assertRaises(RuntimeError) as cm:
            await open_with(ex)
        self.assertNotIsInstance(cm.exception, UnhedgedError)
        self.assertEqual(len(ex.fut_buys()), 1)
        self.assertAlmostEqual(ex.fut_position, 0.0)

    async def test_unknown_outcome_halts_without_unwinding(self):
        ex = LostSpotReply(lookup_error=asyncio.TimeoutError())
        with self.assertRaises(UnhedgedError) as cm:
            await open_with(ex)
        self.assertIn("IS OPEN", str(cm.exception))
        self.assertEqual(ex.fut_buys(), [])

    async def test_entry_basis_comes_from_the_fills(self):
        pos = await open_with(FakeExchange(reply="avg"))        # perp sold at 100.0, spot bought at 100.2
        self.assertAlmostEqual(pos.entry_basis_bps, (100.0 - 100.2) / 100.2 * 1e4)


class ClockResyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rejected_timestamp_resyncs_and_retries_once(self):
        c = BinanceClient(None)
        sent, synced = [], []

        async def request(venue, method, path, params, signed, timeout):
            sent.append(path)
            if len(sent) == 1:
                raise BinanceError(400, -1021, "Timestamp for this request is outside of the recvWindow.", path)
            return {"ok": True}

        async def sync():
            synced.append(1)
            return 1500
        c._request, c.sync_time = request, sync
        self.assertEqual(await c.post("fapi", "/fapi/v1/order", {"symbol": "X"}), {"ok": True})
        self.assertEqual((len(sent), len(synced)), (2, 1))

    async def test_other_errors_are_not_retried(self):
        c = BinanceClient(None)
        sent = []

        async def request(venue, method, path, params, signed, timeout):
            sent.append(path)
            raise BinanceError(400, -2019, "Margin is insufficient.", path)
        c._request = request
        with self.assertRaises(BinanceError):
            await c.post("fapi", "/fapi/v1/order", {})
        self.assertEqual(len(sent), 1)


class OpenLossTests(unittest.TestCase):
    def setUp(self):
        self.pf = Portfolio(Store(Path(":memory:")), 1_000_000.0)
        self.risk = Risk(small_cfg(tempfile.mkdtemp()).risk, self.pf)
        self.limit = self.risk.cfg.daily_loss_limit_usd

    def test_open_losses_block_entries_without_flattening(self):
        self.risk.refresh(1_000_001.0, unrealized=-(self.limit + 1))
        self.assertTrue(self.risk.halted and self.risk.open_loss_active)
        self.assertFalse(self.risk.flatten_required)
        self.assertIn("counting open positions", self.risk.halt_reason)
        self.risk.refresh(1_000_002.0, unrealized=-1.0)          # recovered: entries allowed again
        self.assertFalse(self.risk.halted)

    def test_realised_plus_open_losses_add_up(self):
        pos = Position("ETHUSDT", "ETHUSDT", 1.0, 0.0, 200.0, 2.0, 2.0, 100.0, 100.0, 50.0, 4.0)
        self.pf.add(pos)
        self.pf.close(pos, ClosedTrade("ETHUSDT", 0, 1_000_000.0, 200, -self.limit / 2, 0, 50, 0, 3.0, 0, "stop-loss"))
        self.risk.refresh(1_000_001.0, unrealized=-self.limit / 2)
        self.assertTrue(self.risk.open_loss_active)
        self.risk.refresh(1_000_001.0, unrealized=+100.0)        # gains on open positions never offset a realised loss
        self.assertFalse(self.risk.halted)


class FundingRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_accrual_uses_the_rate_seen_before_the_funding_time(self):
        eng, m, pf, cfg = engine(trading=False)
        t = await warm(eng, m)
        pos = await open_paper(eng, m, t)
        due = int((t + 5) * 1000)
        m.next_funding_ms, m.funding_rate, pos.next_funding_ms = due, 0.0001, due
        set_quotes(m, 100.0, 2.0, t + 1)
        await eng.tick(now=t + 1)
        m.next_funding_ms, m.funding_rate = due + 8 * 3600 * 1000, 0.0009   # the feed already shows the NEXT rate
        set_quotes(m, 100.0, 2.0, t + 6)
        await eng.tick(now=t + 6)
        self.assertAlmostEqual(pos.funding_acc, pos.perp_qty * m.perp.mid * 0.0001)


if __name__ == "__main__":
    unittest.main()
