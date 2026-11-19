"""The order book read: which side is pushing, per timeframe, for the terminal chart's BOOK overlay.

An order book strategy reads two kinds of orders:

  resting (limit) orders  the book: bids waiting below the price, asks above. More bids than asks near the price
                          (IMBALANCE) means buyers are lining up; one level far bigger than the rest (a WALL) is a
                          price a big player defends or wants filled: support below, resistance above.
  aggressive (market)     the trades that hit the book: taker buys lift the asks, taker sells hit the bids. Their
                          difference (DELTA) is who is actually pushing. Heavy selling while the price does not fall
                          means the bids absorb it (ABSORPTION): buyers are stronger than the tape looks.

Each timeframe (5m, 15m, 1h, 4h) gets a score from -100 (sellers in control) to +100 (buyers), from the book's
imbalance averaged over that timeframe (recorded here, so only since the coin was first opened), the taker delta of its
last two candles (from the candles themselves, so always there), absorption and the walls near the price. It is a read
of pressure, not a forecast: walls can be pulled (spoofing), and the book changes in seconds. Nothing is backtested.

The book is Binance MAINNET public data (a testnet book is thin and made up), 500 levels a side (weight 10), read every
SAMPLE_S for at most MAX_COINS coins, and only while the page asks for them (a coin not asked for in WATCH_S is not
read). Samples are kept for KEEP_S, in memory.
"""
import asyncio
import logging
import time
from collections import deque

log = logging.getLogger("orderbook")

SAMPLE_S = 10.0              # one book read per watched coin this often
WATCH_S = 600.0              # a coin the page has not asked about for this long is no longer read
KEEP_S = 86_400.0            # samples kept this long
MAX_COINS = 2                # coins read at once (the most recently asked)
MAX_TAPES = 6                # coins whose samples are kept
LIMIT = 500                  # book levels a side
NEAR = 0.005                 # the imbalance counts orders within 0.5% of the mid
WIDE = 0.01                  # ...and, for the totals, within 1%
WALL_X = 5.0                 # a wall: a level at least this many times the median level of its side within WIDE
WALL_RANGE = 0.02            # walls are looked for within 2% of the mid
FRAMES = (("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14_400))
STEP = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400, "1w": 604_800}
FRAME_TTL = 15.0             # the timeframe reads (they use candles) are reused this long
IMB_FULL = 0.4               # a book imbalance of this much counts fully (+-1)
DELTA_FULL = 0.25            # a taker delta of this share of the volume counts fully
CALL = 25                    # |score| from here is a call (UP / DOWN); under it BALANCED


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def walls(levels: list, mid: float, side: str) -> list[dict]:
    """The biggest levels of one side within WALL_RANGE of the mid that are WALL_X times its median, biggest first
    (at most 3): {price, usd, dist_pct}."""
    near = [(p, p * q) for p, q in levels if abs(p / mid - 1) <= WALL_RANGE]
    if len(near) < 5:
        return []
    usd = sorted(u for _, u in near)
    med = usd[len(usd) // 2]
    big = sorted((x for x in near if med > 0 and x[1] >= WALL_X * med), key=lambda x: -x[1])[:3]
    return [{"price": p, "usd": round(u), "dist_pct": round((p / mid - 1) * 100, 3), "side": side} for p, u in big]


def book_stats(bids: list, asks: list) -> dict | None:
    """One book read: mid, spread, bid / ask dollars within NEAR and WIDE, the imbalance (bids - asks) / (bids + asks)
    within NEAR (-1 all asks .. +1 all bids), the walls each side and how far the levels read reach (on BTC, 500 levels
    reach only about 0.1%: the imbalance then covers that)."""
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2

    def side_usd(levels, band):
        return sum(p * q for p, q in levels if abs(p / mid - 1) <= band)
    bn, an, bw, aw = side_usd(bids, NEAR), side_usd(asks, NEAR), side_usd(bids, WIDE), side_usd(asks, WIDE)
    reach = min(abs(bids[-1][0] / mid - 1), abs(asks[-1][0] / mid - 1)) * 100
    return {"mid": mid, "spread_bp": round((ba - bb) / mid * 1e4, 2),
            "bid_usd": round(bn), "ask_usd": round(an), "imb": round((bn - an) / (bn + an), 4) if bn + an else 0.0,
            "bid_usd_wide": round(bw), "ask_usd_wide": round(aw),
            "imb_wide": round((bw - aw) / (bw + aw), 4) if bw + aw else 0.0,
            "walls": walls(bids, mid, "bid") + walls(asks, mid, "ask"), "reach_pct": round(reach, 3)}


def frame_read(tf: str, secs: int, samples: list, candles: list | None, live: dict | None, now: float) -> dict:
    """One timeframe's verdict. `samples`: (ts, mid, imb) book reads; `candles`: that timeframe's klines
    ([open ms, o, h, l, c, base vol, quote vol, taker-buy quote vol]); `live`: the latest book_stats."""
    why, parts = [], []
    recent = [s for s in samples if s[0] >= now - secs]
    imb = sum(s[2] for s in recent) / len(recent) if recent else None
    cover = min(1.0, (now - recent[0][0]) / secs) if recent else 0.0
    if imb is not None:
        parts.append((0.55, _clip(imb / IMB_FULL)))
        side = "bids" if imb > 0 else "asks"
        band = min(NEAR * 100, live["reach_pct"]) if live and live.get("reach_pct") else NEAR * 100
        why.append(f"book: {side} {abs(imb) * 100:.0f}% heavier within {band:.2g}% of the price"
                   + (f" (recorded {cover * 100:.0f}% of the {tf})" if cover < 0.95 else ""))
    delta = price_pct = None
    if candles and len(candles) >= 2:
        last = candles[-2:]
        q = sum(c[6] for c in last)
        if q > 0:
            delta = (2 * sum(c[7] for c in last) - q) / q            # taker buys - sells, as a share of the volume
            price_pct = (last[-1][4] / last[0][1] - 1) * 100
            parts.append((0.45, _clip(delta / DELTA_FULL)))
            why.append(f"trades: taker {'buys' if delta > 0 else 'sells'} {abs(delta) * 100:.0f}% ahead over 2 candles, "
                       f"price {price_pct:+.2f}%")
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else 0.0
    if delta is not None and price_pct is not None:
        if delta < -0.1 and price_pct >= 0:
            score += 0.15
            why.append("absorption: sellers hit the bids but the price held (buyers soaking it up)")
        elif delta > 0.1 and price_pct <= 0:
            score -= 0.15
            why.append("absorption: buyers lifted the asks but the price did not rise (sellers soaking it up)")
    if live:
        near = [w for w in live["walls"] if abs(w["dist_pct"]) <= NEAR * 100]
        bid_w, ask_w = [w for w in near if w["side"] == "bid"], [w for w in near if w["side"] == "ask"]
        if bid_w and not ask_w:
            score += 0.1
            why.append(f"support wall {bid_w[0]['price']:.6g} (${bid_w[0]['usd'] / 1e6:.2f}M, {bid_w[0]['dist_pct']:+.2f}%)")
        elif ask_w and not bid_w:
            score -= 0.1
            why.append(f"resistance wall {ask_w[0]['price']:.6g} (${ask_w[0]['usd'] / 1e6:.2f}M, {ask_w[0]['dist_pct']:+.2f}%)")
    s = int(round(_clip(score) * 100))
    verdict = "up" if s >= CALL else "down" if s <= -CALL else "balanced"
    return {"tf": tf, "score": s, "verdict": verdict, "imb": None if imb is None else round(imb, 4),
            "coverage": round(cover, 2), "delta": None if delta is None else round(delta, 4),
            "price_pct": None if price_pct is None else round(price_pct, 3),
            "why": why or ["no book reads or candles yet"]}


class BookWatch:
    """Reads the book of the coins the terminal is looking at, keeps the reads, and answers the chart's questions."""

    def __init__(self, market, clock=time.time):
        self.market, self.clock = market, clock               # an arbbot.market.MarketData on mainnet public data
        self.tapes: dict[str, deque] = {}
        self.wanted: dict[str, float] = {}
        self.live: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self._frames: dict[str, tuple[float, list]] = {}
        self._task: asyncio.Task | None = None

    def want(self, sym: str) -> None:
        self.wanted[sym] = self.clock()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    def watching(self) -> list[str]:
        now = self.clock()
        live = [s for s, t in self.wanted.items() if now - t <= WATCH_S]
        return sorted(live, key=lambda s: -self.wanted[s])[:MAX_COINS]

    async def sample(self, sym: str) -> None:
        try:
            d = await self.market.depth(sym, LIMIT)
        except Exception as e:                                # noqa: BLE001 - say it on the chart, try again later
            self.errors[sym] = f"{type(e).__name__}: {str(e)[:120]}"
            return
        st = book_stats(d["bids"], d["asks"])
        if st is None:
            return
        now = self.clock()
        st["ts"] = now
        self.errors.pop(sym, None)
        self.live[sym] = st
        tape = self.tapes.setdefault(sym, deque())
        tape.append((now, st["mid"], st["imb"], st["imb_wide"]))
        while tape and tape[0][0] < now - KEEP_S:
            tape.popleft()
        if len(self.tapes) > MAX_TAPES:                       # forget the coin asked about longest ago
            old = min(self.tapes, key=lambda s: self.wanted.get(s, 0))
            for m in (self.tapes, self.live, self._frames):
                m.pop(old, None)

    async def run(self) -> None:
        while True:                                           # view() read the first book itself: wait first
            await asyncio.sleep(SAMPLE_S)
            syms = self.watching()
            if not syms:
                return                                        # nobody is looking: stop; want() starts it again
            await asyncio.gather(*(self.sample(s) for s in syms))

    def series(self, sym: str, interval: str) -> list[list]:
        """The book imbalance per candle of `interval`: [candle open ms, average imbalance, reads]."""
        step = STEP.get(interval, 3600) * 1000
        out: dict[int, list] = {}
        for ts, _, imb, _ in self.tapes.get(sym, ()):
            b = int(ts * 1000) // step * step
            o = out.setdefault(b, [b, 0.0, 0])
            o[1] += imb
            o[2] += 1
        return [[b, round(s / n, 4), n] for b, s, n in sorted(out.values())]

    async def frames(self, sym: str) -> list[dict]:
        hit = self._frames.get(sym)
        now = self.clock()
        if hit and now - hit[0] < FRAME_TTL:
            return hit[1]
        samples = [(ts, mid, imb) for ts, mid, imb, _ in self.tapes.get(sym, ())]

        async def candles(tf):
            try:
                return await self.market.klines(sym, tf)
            except Exception:                                 # noqa: BLE001 - that frame reads the book only
                return None
        kls = await asyncio.gather(*(candles(tf) for tf, _ in FRAMES))
        out = [frame_read(tf, secs, samples, kl, self.live.get(sym), now) for (tf, secs), kl in zip(FRAMES, kls)]
        self._frames[sym] = (now, out)
        return out

    async def view(self, sym: str, interval: str) -> dict:
        sym = self.market.check_symbol(sym)
        if interval not in STEP:
            raise ValueError(f"interval must be one of {', '.join(STEP)}")
        if sym not in self.live:
            await self.sample(sym)                            # the first answer already has a book
        self.want(sym)
        tape = self.tapes.get(sym)
        return {"symbol": sym, "source": "Binance mainnet (public)", "live": self.live.get(sym),
                "series": self.series(sym, interval), "frames": await self.frames(sym),
                "since": tape[0][0] if tape else None, "reads": len(tape) if tape else 0,
                "sample_s": SAMPLE_S, "error": self.errors.get(sym)}
