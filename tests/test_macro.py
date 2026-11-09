"""The terminal's MACRO / NEWS panel (arbbot/macro.py): parsing untrusted feeds and series, and failing source by source."""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from arbbot import macro
from arbbot.macro import MacroNews, parse_feed, parse_fred, parse_yahoo, tags_of
from arbbot.server import Dashboard

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Fed holds rates as inflation cools; Bitcoin jumps</title><link>https://example.com/a</link>
  <pubDate>Sun, 08 Nov 2026 12:00:00 GMT</pubDate><description><![CDATA[<p>The <b>FOMC</b> kept rates.</p>]]></description></item>
<item><title>SEC sues exchange</title><link>javascript:alert(1)</link><pubDate>Sun, 08 Nov 2026 13:00:00 GMT</pubDate></item>
<item><title>No date</title><link>https://example.com/c</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Solana outage</title>
<link href="https://example.com/s"/><updated>2026-11-08T10:00:00Z</updated></entry></feed>"""
BOMB = """<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;"><!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
<!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;"><!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;"><!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;">
]><rss><channel><item><title>&h;</title></item></channel></rss>"""


class ParseTests(unittest.TestCase):
    def test_rss_items_keep_plain_text_and_safe_links_only(self):
        items = parse_feed(RSS, "Src", "macro")
        self.assertEqual([i["title"] for i in items], ["Fed holds rates as inflation cools; Bitcoin jumps", "SEC sues exchange"])
        first, second = items
        self.assertEqual(first["link"], "https://example.com/a")
        self.assertEqual(second["link"], "")                                # javascript: link dropped
        self.assertEqual(first["summary"], "The FOMC kept rates.")          # markup stripped
        self.assertEqual(first["ts"], 1794139200000)
        self.assertEqual((first["topics"], first["coins"]), (["fed", "inflation"], ["BTC"]))
        self.assertIn("regulation", second["topics"])

    def test_atom_entries(self):
        (item,) = parse_feed(ATOM, "Src", "crypto")
        self.assertEqual((item["title"], item["link"], item["coins"]), ("Solana outage", "https://example.com/s", ["SOL"]))

    def test_an_entity_bomb_is_refused_not_expanded(self):
        with self.assertRaises(Exception):
            parse_feed(BOMB, "Src", "crypto")

    def test_tags_need_whole_words(self):
        self.assertEqual(tags_of("Federation feds ether"), ([], ["ETH"]))   # "Federation" is not the Fed
        self.assertEqual(tags_of("The Fed and CPI"), (["fed", "inflation"], []))

    def test_fred_csv_and_yahoo_json(self):
        self.assertEqual(parse_fred("observation_date,DFF\n2026-11-02,3.88\n2026-11-03,.\n"), [[1793577600000, 3.88]])
        body = {"chart": {"result": [{"timestamp": [1, 2, 3], "indicators": {"quote": [{"close": [10.0, None, 12.5]}]}}]}}
        self.assertEqual(parse_yahoo(body), [[1000, 10.0], [3000, 12.5]])
        with self.assertRaises(ValueError):
            parse_yahoo({"chart": {"result": None, "error": {"code": "Not Found"}}})


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body, self.charset = status, body.encode(), "utf-8"
        self.content = self

    async def iter_chunked(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self):
        self.urls, self.headers = [], []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers)
        if "fredgraph" in url:
            return FakeResponse(200, "observation_date,X\n2026-10-01,1.5\n") if "DFF" in url else FakeResponse(500, "")
        if "yahoo" in url:
            return FakeResponse(200, '{"chart":{"result":[{"timestamp":[1],"indicators":{"quote":[{"close":[5.0]}]}}]}}')
        if "cointelegraph" in url:
            return FakeResponse(200, "<rss><channel><item><title>broken")      # a truncated feed
        return FakeResponse(200, RSS)


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_source_is_reported_and_the_rest_still_load(self):
        s, now = FakeSession(), [0.0]
        m = MacroNews(s, clock=lambda: now[0])
        mac, news = await m.macro(), await m.news()
        self.assertEqual(mac["series"]["DFF"]["rows"], [[1790812800000, 1.5]])
        self.assertIn("HTTP 500", mac["series"]["CPIAUCSL"]["error"])
        self.assertEqual(mac["series"]["^TNX"]["rows"], [[1000, 5.0]])
        bad = [x for x in news["sources"] if not x["ok"]]
        self.assertEqual([x["source"] for x in bad], ["Cointelegraph"])
        self.assertEqual(len(news["items"]), 2)                                      # same titles from 4 feeds: once
        self.assertTrue(all(h["User-Agent"] == macro.UA for h in s.headers))         # one honest client name
        n = len(s.urls)
        await asyncio.gather(m.macro(), m.news(), m.news())
        self.assertEqual(len(s.urls), n)                                             # cached
        now[0] += macro.NEWS_TTL + 1
        await m.news()
        self.assertGreater(len(s.urls), n)

    async def test_an_oversized_answer_is_refused(self):
        class Big(FakeSession):
            def get(self, url, headers=None, timeout=None):
                return FakeResponse(200, "x" * (macro.MAX_BYTES + 10))
        news = await MacroNews(Big()).news()
        self.assertTrue(all("too large" in x["error"] for x in news["sources"]))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes(self):
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        dash = Dashboard(SimpleNamespace(swing=None), cfg, Path("web"), macro=MacroNews(FakeSession()))
        server = TestServer(dash.make_app())
        await server.start_server()
        dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        client = TestClient(server)
        await client.start_server()
        try:
            r = await client.get("/api/market/news")
            self.assertEqual((r.status, len((await r.json())["items"])), (200, 2))
            r = await client.get("/api/market/macro")
            self.assertEqual(r.status, 200)
            dash.macro = None
            self.assertEqual((await client.get("/api/market/news")).status, 404)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
