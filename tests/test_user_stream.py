"""The testnet ticket's user-data stream (arbbot/testnet_ticket.py): pushes wake the account read, a quiet stream means
reads every STREAM_POLL_S instead of every poll_s, the auto-traders skip a read that is still current, and a stream that
fails or expires falls back to polling and reconnects."""
import asyncio
import json
import unittest

import aiohttp

from arbbot import testnet_ticket as tt
from arbbot.testnet_ticket import TestnetTicket


class Msg:
    def __init__(self, data, type_=aiohttp.WSMsgType.TEXT):
        self.type, self.data = type_, json.dumps(data) if type_ == aiohttp.WSMsgType.TEXT else data


class WS:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.msgs:
            await asyncio.Event().wait()                        # an open, quiet stream
        m = self.msgs.pop(0)
        if isinstance(m, Exception):
            raise m
        return m


class Session:
    def __init__(self, scripts):
        self.scripts, self.urls = list(scripts), []

    def ws_connect(self, url, **kw):
        self.urls.append(url)
        s = self.scripts.pop(0) if self.scripts else []
        if isinstance(s, Exception):
            raise s
        return WS(s)


class Client:
    hosts = {"fapi": "https://testnet.binancefuture.com"}

    def __init__(self, session):
        self.session, self.requests, self.reads = session, [], 0

    def paused_for(self):
        return 0.0

    async def request(self, venue, method, path, params=None, signed=False, timeout=None):
        self.requests.append((method, path))
        return {"listenKey": f"key{len(self.requests)}"}


class Stream(unittest.IsolatedAsyncioTestCase):
    def make(self, scripts):
        self.client = Client(Session(scripts))
        t = TestnetTicket(self.client, None, poll_s=4.0)
        t.now = 1000.0
        t.clock = lambda: t.now
        reads = []

        async def refresh():
            reads.append(t.now)
            t.refreshed_at = t.now
        t.refresh = refresh
        return t, reads

    async def test_a_push_wakes_the_reader_and_marks_the_fills_due(self):
        t, _ = self.make([])
        t._fills_due = False
        t.on_stream({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT"}})
        self.assertTrue(t._kick.is_set() and t._fills_due)
        self.assertEqual(t.stream_events, 1)
        t._kick.clear()
        t.on_stream({"e": "something_else"})
        self.assertFalse(t._kick.is_set())

    async def test_a_current_read_is_reused_only_while_the_stream_is_up_and_quiet(self):
        t, reads = self.make([])
        t.refreshed_at = t.now - 5
        await t.refresh_if_stale()
        self.assertEqual(len(reads), 1)                         # stream down: always read
        t.stream_ok, t.refreshed_at = True, t.now - 5
        await t.refresh_if_stale()
        self.assertEqual(len(reads), 1)                         # up, quiet, fresh: reuse
        t._kick.set()
        await t.refresh_if_stale()
        self.assertEqual(len(reads), 2)                         # a push not read yet: read
        t._kick.clear()
        t.now += tt.STREAM_POLL_S + 6
        await t.refresh_if_stale()
        self.assertEqual(len(reads), 3)                         # too old: read

    async def test_the_stream_connects_on_the_listen_key_and_reconnects_after_it_expires(self):
        t, _ = self.make([[Msg({"e": "ACCOUNT_UPDATE"}), Msg({"e": "listenKeyExpired"})], []])
        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            sleeps.append(s)
            await real_sleep(0)
        tt.asyncio.sleep = fast_sleep
        try:
            task = asyncio.create_task(t.stream())
            for _ in range(50):
                await real_sleep(0)
            self.assertEqual(self.client.session.urls,
                             ["wss://fstream.binancefuture.com/ws/key1", "wss://fstream.binancefuture.com/ws/key2"])
            self.assertEqual([r for r in self.client.requests if r[0] == "POST"], [("POST", "/fapi/v1/listenKey")] * 2)
            self.assertIn(("PUT", "/fapi/v1/listenKey"), self.client.requests)   # the keepalive (sleeps are instant here)
            self.assertTrue(t.stream_ok)                            # reconnected and quiet
            self.assertEqual(t.stream_events, 1)
            self.assertIn(5.0, sleeps)                              # the pause before reconnecting
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(t.stream_ok)
        finally:
            tt.asyncio.sleep = real_sleep

    async def test_a_refused_path_tries_the_routed_one_and_failures_fall_back_to_polling(self):
        refused = aiohttp.WSServerHandshakeError(None, (), status=404, message="Not Found")
        t, _ = self.make([refused, []])
        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            await real_sleep(0)
        tt.asyncio.sleep = fast_sleep
        try:
            task = asyncio.create_task(t.stream())
            for _ in range(50):
                await real_sleep(0)
            self.assertEqual(self.client.session.urls[1], "wss://fstream.binancefuture.com/private/ws/key2")
            self.assertTrue(t.stream_ok)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            tt.asyncio.sleep = real_sleep

    async def test_the_reader_polls_slowly_while_the_stream_is_up_and_wakes_on_a_push(self):
        t, reads = self.make([])
        waits = []
        real_wait_for = asyncio.wait_for

        async def wait_for(aw, timeout):
            waits.append(timeout)
            if len(waits) >= 3:
                aw.close()
                raise asyncio.CancelledError
            return await real_wait_for(aw, 0.01)
        tt.asyncio.wait_for = wait_for
        try:
            t.stream_ok = True
            with self.assertRaises(asyncio.CancelledError):
                await t._run(0.0, {})
            self.assertEqual(waits[0], tt.STREAM_POLL_S)
            t.stream_ok = False
            waits.clear()
            with self.assertRaises(asyncio.CancelledError):
                await t._run(0.0, {})
            self.assertEqual(waits[0], 4.0)                         # stream down: the old 4 s polling
        finally:
            tt.asyncio.wait_for = real_wait_for


if __name__ == "__main__":
    unittest.main()
