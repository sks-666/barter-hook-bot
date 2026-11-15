"""Macro & news for the terminal's MACRO / NEWS panel: read-only public data, fetched only while a page asks for it.

  * FRED (St. Louis Fed) CSV downloads, no key: CPI, core CPI, unemployment, the fed funds rate, the 10y-2y curve.
  * Yahoo Finance daily closes: S&P 500, consumer discretionary (XLY) vs utilities (XLU), 10-year and 3-month Treasury
    yields, the dollar index, VIX, gold, oil.
  * RSS headlines: the Federal Reserve's monetary-policy releases, CNBC economy, and crypto news (CoinDesk,
    Cointelegraph, Decrypt).

Every source is asked with one honest client name, cached for minutes, and a source that fails is reported in its
place without hiding the others. Nothing is sent anywhere: the page reads, the bot never trades on any of it.
"""
import asyncio
import calendar
import csv
import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

log = logging.getLogger("macro")

UA = "arbbot-terminal/1.0 (personal dashboard)"
MAX_BYTES = 3_000_000                       # a feed or a series larger than this is refused, not read

FRED = {                                    # id -> (label, unit, how the panel reads it)
    "CPIAUCSL": ("CPI", "index", "yoy"),
    "CPILFESL": ("Core CPI", "index", "yoy"),
    "UNRATE": ("Unemployment", "%", "level"),
    "DFF": ("Fed funds rate", "%", "level"),
    "T10Y2Y": ("10y − 2y curve", "pts", "level"),
}
YAHOO = {                                   # symbol -> label
    "^GSPC": "S&P 500", "XLY": "Consumer discretionary (XLY)", "XLU": "Utilities (XLU)",
    "^TNX": "10-year yield", "^IRX": "3-month T-bill", "DX-Y.NYB": "Dollar index", "^VIX": "VIX",
    "GC=F": "Gold", "CL=F": "Oil (WTI)",
}
FEEDS = [                                   # (source, kind, url)
    ("Federal Reserve", "macro", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("CNBC Economy", "macro", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CoinDesk", "crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "crypto", "https://cointelegraph.com/rss"),
    ("Decrypt", "crypto", "https://decrypt.co/feed"),
]
MACRO_TTL, NEWS_TTL = 900.0, 180.0
# this week's economic calendar (Forex Factory's public JSON export): high-impact US releases, for the signal's
# [SESSION] guardrail (arbbot/quality.py). The export asks to be read rarely: once an hour.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_TTL = 3600.0


def parse_calendar(body) -> list[dict]:
    """High-impact USD events as [{ts (ms), title, country}], oldest first; rows without a time are skipped."""
    out = []
    for e in body if isinstance(body, list) else []:
        if not isinstance(e, dict) or e.get("impact") != "High" or e.get("country") != "USD":
            continue
        try:
            ts = datetime.fromisoformat(str(e["date"])).timestamp() * 1000
        except (KeyError, ValueError):
            continue
        out.append({"ts": int(ts), "title": str(e.get("title", ""))[:80], "country": "USD"})
    return sorted(out, key=lambda e: e["ts"])
NEWS_MAX = 120

# a headline's topics, for the panel's tags and filters (whole words, case-insensitive)
TOPICS = {
    "fed": r"fed|fomc|powell|rate cuts?|rate hikes?|interest rates?|central bank|treasury yields?|bond yields?",
    "inflation": r"inflation|cpi|pce|consumer prices|price index",
    "jobs": r"jobs|payrolls|unemployment|labor market|jobless",
    "growth": r"gdp|recession|economic growth|retail sales|consumer spending|pmi|tariffs?",
    "regulation": r"sec|cftc|regulat\w*|lawsuit|court|ban|etfs?|congress|senate|bill",
    "hack": r"hack\w*|exploit\w*|drained|stolen|breach",
}
TOPIC_RE = {k: re.compile(rf"\b(?:{v})\b", re.I) for k, v in TOPICS.items()}
COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TON", "SUI", "TRX", "DOT", "LTC")
COIN_NAMES = {"BTC": "bitcoin", "ETH": "ethereum|ether", "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
              "ADA": "cardano", "AVAX": "avalanche", "LINK": "chainlink", "DOT": "polkadot", "LTC": "litecoin"}
COIN_RE = {c: re.compile(rf"\b(?:{c}|{COIN_NAMES[c]})\b" if c in COIN_NAMES else rf"\b{c}\b", re.I) for c in COINS}
TAGS_RE = re.compile(r"<[^>]+>")


def tags_of(text: str) -> tuple[list[str], list[str]]:
    return ([k for k, rx in TOPIC_RE.items() if rx.search(text)], [c for c, rx in COIN_RE.items() if rx.search(text)])


def parse_fred(text: str) -> list[list]:
    """FRED's CSV (date, value; '.' = no value that day) -> [[epoch ms, value], ...] oldest first."""
    rows = []
    for r in list(csv.reader(io.StringIO(text)))[1:]:
        if len(r) >= 2 and r[1] not in ("", "."):
            try:
                ts = calendar.timegm(time.strptime(r[0], "%Y-%m-%d")) * 1000
                rows.append([ts, float(r[1])])
            except ValueError:
                continue
    return rows


def parse_yahoo(body: dict) -> list[list]:
    """Yahoo's chart JSON -> [[epoch ms, close], ...] oldest first, days without a close left out."""
    res = (body.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("no data")
    ts = res[0].get("timestamp") or []
    closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    return [[int(t) * 1000, float(c)] for t, c in zip(ts, closes) if c is not None]


def safe_link(url: str) -> str:
    url = (url or "").strip()
    return url if url.startswith(("https://", "http://")) and len(url) < 2000 else ""


def parse_feed(xml_text: str, source: str, kind: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, link, ts, source, kind, summary, topics, coins}]. The text is untrusted: only plain
    text is kept (tags stripped), links must be http(s), and the page escapes everything again when it shows it."""
    root = ET.fromstring(xml_text)          # expat >= 2.4.1 refuses entity-expansion bombs; no external entities
    items = root.findall("./channel/item") or root.findall("{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items[:60]:
        def get(*names):
            for n in names:
                el = it.find(n)
                if el is not None:
                    return (el.text or el.get("href") or "").strip()
            return ""
        title = TAGS_RE.sub("", get("title", "{http://www.w3.org/2005/Atom}title"))[:300]
        link = safe_link(get("link", "{http://www.w3.org/2005/Atom}link"))
        when = get("pubDate", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published")
        try:
            ts = int(parsedate_to_datetime(when).timestamp() * 1000)
        except (TypeError, ValueError, IndexError):
            try:                                        # Atom: ISO 8601
                ts = int(datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                continue
        summary = re.sub(r"\s+", " ", TAGS_RE.sub(" ", get("description", "{http://www.w3.org/2005/Atom}summary"))).strip()[:400]
        if not title:
            continue
        topics, coins = tags_of(f"{title} {summary}")
        out.append({"title": title, "link": link, "ts": ts, "source": source, "kind": kind, "summary": summary,
                    "topics": topics, "coins": coins})
    return out


class MacroNews:
    def __init__(self, session, clock=time.monotonic, wall=time.time):
        self.session, self.clock, self.wall = session, clock, wall
        self._cache: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _text(self, url: str) -> str:
        async with self.session.get(url, headers={"User-Agent": UA}, timeout=20) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            body = bytearray()
            async for chunk in r.content.iter_chunked(65536):    # all of it, but never more than MAX_BYTES
                body += chunk
                if len(body) > MAX_BYTES:
                    raise RuntimeError("answer too large")
            return bytes(body).decode(r.charset or "utf-8", errors="replace")

    async def _cached(self, key: str, ttl: float, load):
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:                                   # several tabs asking at once: one fetch
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < ttl:
                return hit[1]
            value = await load()
            self._cache[key] = (self.clock(), value)
            return value

    async def macro(self) -> dict:
        """Every series, each {label, unit, read, rows: [[ms, value]]} or {label, error}: one failing source
        never hides the rest."""
        async def load():
            start = time.strftime("%Y-%m-%d", time.gmtime(self.wall() - 3 * 365 * 86400))

            async def fred(sid):
                label, unit, read = FRED[sid]
                try:
                    rows = parse_fred(await self._text(
                        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"))
                    if not rows:
                        raise ValueError("no data")
                    return sid, {"label": label, "unit": unit, "read": read, "source": "FRED", "rows": rows}
                except Exception as e:
                    return sid, {"label": label, "source": "FRED", "error": f"{type(e).__name__}: {str(e)[:80]}"}

            async def yahoo(sym):
                try:
                    body = json.loads(await self._text(
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"))
                    return sym, {"label": YAHOO[sym], "unit": "%" if sym in ("^TNX", "^IRX") else "", "read": "level",
                                 "source": "Yahoo Finance", "rows": parse_yahoo(body)}
                except Exception as e:
                    return sym, {"label": YAHOO[sym], "source": "Yahoo Finance", "error": f"{type(e).__name__}: {str(e)[:80]}"}

            pairs = await asyncio.gather(*(fred(s) for s in FRED), *(yahoo(s) for s in YAHOO))
            return {"series": dict(pairs), "fetched": int(self.wall() * 1000)}
        return await self._cached("macro", MACRO_TTL, load)

    async def news(self) -> dict:
        """The newest headlines of every feed, newest first, duplicates (same title) dropped."""
        async def load():
            async def one(source, kind, url):
                try:
                    return source, parse_feed(await self._text(url), source, kind), None
                except Exception as e:
                    return source, [], f"{type(e).__name__}: {str(e)[:80]}"
            got = await asyncio.gather(*(one(*f) for f in FEEDS))
            seen, items = set(), []
            for _, rows, _ in got:
                for r in rows:
                    k = r["title"].lower()
                    if k not in seen:
                        seen.add(k)
                        items.append(r)
            items.sort(key=lambda r: -r["ts"])
            return {"items": items[:NEWS_MAX], "sources": [{"source": s, "ok": err is None, "error": err, "n": len(rows)}
                                                           for s, rows, err in got],
                    "fetched": int(self.wall() * 1000)}
        return await self._cached("news", NEWS_TTL, load)

    async def calendar(self) -> list[dict]:
        """This week's high-impact US economic releases (see parse_calendar)."""
        async def load():
            return parse_calendar(json.loads(await self._text(CALENDAR_URL)))
        return await self._cached("calendar", CALENDAR_TTL, load)
