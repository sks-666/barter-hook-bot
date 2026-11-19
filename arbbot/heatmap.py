"""The liquidity heatmap (web/heatmap.html): where resting orders sat over time, price band by price band.

For each coin the page opens, a local copy of Binance's MAINNET futures order book is kept live from the diff-depth
WebSocket (<sym>@depth@500ms) on top of one 1000-level REST snapshot (weight 20, only when (re)connecting), following
Binance's sync rules (drop events before the snapshot, then every event's `pu` must be the last event's `u`, else
resync). Every SAMPLE_S the book is summed into price bands (BIN_FRAC of the price wide, +-RANGE around it) and kept
as one column, for KEEP_S, in thousands of dollars as half-precision floats (2 bytes a band: about 12 MB a coin a day). The diff stream reports changes across the whole book, so bands far from the price fill in
as their orders change: the first minutes after a coin is opened show mostly the snapshot's +-0.2% (on BTC).

The page asks for the time and price window it shows at about one column per screen pixel (RESOLUTIONS): every
timeframe gets the same fine heat. Columns coarser than SAMPLE_S are averaged into per-resolution buckets, built once
(in a thread, so the bot's loop is not held up) and then one column at a time.

A coin is recorded while the page asked about it in the last WATCH_S, at most MAX_COINS at a time, in memory only (a
restart starts over). Read-only: nothing here trades.
"""
import array
import asyncio
import base64
import json
import logging
import math
import struct
import time
from collections import deque

log = logging.getLogger("heatmap")

WS_URL = "wss://fstream.binance.com/ws/{sym}@depth@500ms"     # book streams stay on the root path (not /market)
SAMPLE_S = 5.0               # one column this often
KEEP_S = 86_400.0            # columns kept this long
WATCH_S = 6 * 3600.0         # a coin not asked for in this long stops being recorded
MAX_COINS = 3                # coins recorded at once (the most recently asked)
BIN_FRAC = 0.00015           # a band is about this share of the price, rounded (BTC 63,000 -> 10, gold 3,700 -> 0.5)
RANGE = 0.025                # columns cover +-this share of the price
BOOK_SPAN = 0.10             # levels further than this from the price are dropped from the local book
SNAPSHOT = 1000              # levels a side in the REST snapshot
MAX_ROWS = 512               # rows in one answer
MAX_COLS = 2400              # columns in one answer
RESOLUTIONS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800)     # seconds a column may cover
K = 1000.0                   # columns hold thousands of dollars (half precision tops out at 65,504)
STEPS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400}
STRENGTH_BAND = 0.005        # the strength level: bids vs asks within this share of the price


def nice_step(price: float) -> float:
    """The round band height nearest price x BIN_FRAC: 1, 2, 2.5 or 5 x 10^n (BTC at 63,000 -> 10)."""
    raw = max(price * BIN_FRAC, 1e-12)
    e = 10 ** math.floor(math.log10(raw))
    return min((m * e for m in (1, 2, 2.5, 5, 10)), key=lambda x: abs(math.log(x / raw)))


class LocalBook:
    """One coin's order book kept from a snapshot + the diff-depth events (Binance futures rules)."""

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_u: int | None = None          # the last applied event's final update id
        self.synced = False

    def load(self, snap: dict) -> None:
        self.bids = {p: q for p, q in snap["bids"] if q > 0}
        self.asks = {p: q for p, q in snap["asks"] if q > 0}
        self.last_u, self.synced = int(snap["last_update_id"]), False

    def apply(self, ev: dict) -> bool:
        """One depth event. False = out of sequence: the caller must resync from a new snapshot."""
        U, u, pu = int(ev["U"]), int(ev["u"]), int(ev.get("pu", -1))
        if self.last_u is None:
            return False
        if not self.synced:
            if u < self.last_u:
                return True                      # older than the snapshot: drop
            if U > self.last_u + 1:
                return False                     # a gap between the snapshot and the stream (Binance: U <= lastUpdateId)
            self.synced = True
        elif pu != self.last_u:
            return False
        for side, rows in ((self.bids, ev.get("b", ())), (self.asks, ev.get("a", ()))):
            for p, q in rows:
                p, q = float(p), float(q)
                if q > 0:
                    side[p] = q
                else:
                    side.pop(p, None)
        self.last_u = u
        return True

    def strength(self, mid: float) -> float:
        """Bids vs asks within STRENGTH_BAND of the mid, -1 (all asks) .. +1 (all bids)."""
        lo, hi = mid * (1 - STRENGTH_BAND), mid * (1 + STRENGTH_BAND)
        b = sum(p * q for p, q in self.bids.items() if p >= lo)
        a = sum(p * q for p, q in self.asks.items() if p <= hi)
        return (b - a) / (b + a) if b + a else 0.0

    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (max(self.bids) + min(self.asks)) / 2

    def prune(self, mid: float) -> None:
        lo, hi = mid * (1 - BOOK_SPAN), mid * (1 + BOOK_SPAN)
        for side in (self.bids, self.asks):
            for p in [p for p in side if p < lo or p > hi]:
                del side[p]

    def bands(self, step: float, lo_bin: int, n: int, side: str | None = None) -> array.array:
        """Dollars resting per band [lo_bin, lo_bin + n) (both sides, or 'bid' / 'ask')."""
        out = array.array("f", bytes(4 * n))
        for name, levels in (("bid", self.bids), ("ask", self.asks)):
            if side and side != name:
                continue
            for p, q in levels.items():
                i = int(p // step) - lo_bin
                if 0 <= i < n:
                    out[i] += p * q
        return out


class Recorder:
    """One coin: the WebSocket, the local book and its columns (t, lo_bin, band $K as half floats, strength)."""

    def __init__(self, sym: str, market, session, clock=time.time):
        self.sym, self.market, self.session, self.clock = sym, market, session, clock
        self.book = LocalBook()
        self.step: float | None = None
        self.cols: deque = deque()
        self.status, self.since, self.resyncs = "starting", None, 0
        self.rolls: dict[float, dict] = {}
        self.task: asyncio.Task | None = None

    def record(self) -> None:
        mid = self.book.mid()
        if mid is None or not self.book.synced:
            return
        if self.step is None:
            self.step = nice_step(mid)
        n = int(2 * RANGE * mid / self.step) + 1
        lo = int(mid // self.step) - n // 2
        now = self.clock()
        col, strength = half(self.book.bands(self.step, lo, n)), self.book.strength(mid)
        self.cols.append((now, lo, col, strength))
        self.since = self.since or now
        for res, roll in self.rolls.items():
            self._add(roll, res, now, lo, col, strength)
        while self.cols and self.cols[0][0] < now - KEEP_S:
            self.cols.popleft()
            for res, roll in self.rolls.items():               # buckets older than the columns kept
                for b in [b for b in roll if (b + 1) * res < now - KEEP_S]:
                    del roll[b]
        self.book.prune(mid)

    async def _snapshot(self) -> None:
        snap = await self.market.depth(self.sym, SNAPSHOT)
        self.book.load(snap)

    async def run(self) -> None:
        wait = 2.0
        while True:
            try:
                async with self.session.ws_connect(WS_URL.format(sym=self.sym.lower()), heartbeat=20,
                                                   receive_timeout=60) as ws:
                    buffered: list[dict] = []
                    snap_task = asyncio.create_task(self._snapshot())
                    self.book.last_u, self.status = None, "syncing"
                    next_col = self.clock() + SAMPLE_S
                    async for m in ws:
                        if m.type != 1:                      # aiohttp TEXT; anything else ends the connection
                            if m.type in (8, 256, 257, 258):
                                break
                            continue
                        ev = json.loads(m.data)
                        if snap_task is not None:
                            if not snap_task.done():
                                buffered.append(ev)
                                continue
                            snap_task.result()               # a failed snapshot raises: reconnect
                            snap_task, events = None, buffered + [ev]
                        else:
                            events = [ev]
                        for e in events:
                            if not self.book.apply(e):
                                self.resyncs += 1
                                raise ConnectionError("depth stream out of sequence")
                        if self.book.synced:
                            self.status, wait = "recording", 2.0
                        if self.clock() >= next_col:
                            self.record()
                            next_col += SAMPLE_S * max(1, math.ceil((self.clock() - next_col) / SAMPLE_S))
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # noqa: BLE001 - keep trying, say why on the page
                self.status = f"reconnecting ({type(e).__name__}: {str(e)[:80]})"
                log.info("heatmap %s: %s", self.sym, self.status)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60.0)

    # ---- buckets: per answer resolution, average band $K of the columns in each (built once, then kept up to date) ---
    @staticmethod
    def _add(roll: dict, res: float, t: float, lo: int, col: bytes, strength: float = 0.0) -> None:
        vals = struct.unpack(f"<{len(col) // 2}e", col)
        b = int(t // res)
        acc = roll.get(b)
        if acc is None:
            roll[b] = [lo, array.array("f", vals), 1, strength, None]
            return
        acc[3] += strength
        acc[4] = None                                         # its packed answer is out of date
        a_lo, arr = acc[0], acc[1]
        new_lo, new_hi = min(a_lo, lo), max(a_lo + len(arr), lo + len(vals))
        if new_lo != a_lo or new_hi != a_lo + len(arr):          # the price drifted: widen the bucket
            wide = array.array("f", bytes(4 * (new_hi - new_lo)))
            wide[a_lo - new_lo: a_lo - new_lo + len(arr)] = arr
            acc[0], acc[1] = new_lo, wide
            a_lo, arr = new_lo, wide
        off = lo - a_lo
        for i, v in enumerate(vals):
            if v:
                arr[off + i] += v
        acc[2] += 1

    @classmethod
    def _build(cls, cols: list, res: float) -> dict:
        roll: dict = {}
        for t, lo, col, strength in cols:
            cls._add(roll, res, t, lo, col, strength)
        return roll

    async def rollup(self, res: float) -> dict:
        """{bucket: [lo_bin, band $K sums, columns, strength sum, packed answer]} for this resolution; the first time
        it is built from every column kept, in a thread (it can take a second), then kept up to date by record()."""
        roll = self.rolls.get(res)
        if roll is None:
            cols = list(self.cols)
            roll = await asyncio.to_thread(self._build, cols, res)
            last = cols[-1][0] if cols else -1.0
            for t, lo, col, strength in list(self.cols):          # columns recorded meanwhile
                if t > last:
                    self._add(roll, res, t, lo, col, strength)
            self.rolls[res] = roll
            while len(self.rolls) > 4:                            # an old resolution nobody asks for any more
                del self.rolls[next(iter(self.rolls))]
        return roll

    async def grid(self, t0: float, t1: float, res: float, lo_bin: int, n: int) -> tuple[list[float], bytes, list]:
        """The columns (res = SAMPLE_S) or bucket averages between t0 and t1 over bands [lo_bin, lo_bin + n): their start
        times, the band $K as half-precision rows, and the strength level of each."""
        times, out, strength = [], bytearray(), []
        zero = bytes(2 * n)
        if res <= SAMPLE_S:
            for t, lo, col, st in self.cols:
                if t < t0 or t >= t1:
                    continue
                row = bytearray(zero)
                a, z = max(lo, lo_bin), min(lo + len(col) // 2, lo_bin + n)
                if z > a:
                    row[2 * (a - lo_bin): 2 * (z - lo_bin)] = col[2 * (a - lo): 2 * (z - lo)]
                times.append(t)
                strength.append(round(st, 4))
                out += row
            return times, bytes(out), strength
        roll = await self.rollup(res)
        done = self.clock() - SAMPLE_S
        for b in sorted(roll):
            t = b * res
            if t < t0 - res or t >= t1:
                continue
            acc = roll[b]
            lo, arr, k, st, packed = acc
            if packed is None or packed[0] != (lo_bin, n):
                row = array.array("f", bytes(4 * n))
                a, z = max(lo, lo_bin), min(lo + len(arr), lo_bin + n)
                if z > a:
                    row[a - lo_bin: z - lo_bin] = arr[a - lo: z - lo]
                packed = ((lo_bin, n), struct.pack(f"<{n}e", *(min(65_000.0, v / k) for v in row)))
                if t + res < done:                                # a finished bucket: its answer will not change
                    acc[4] = packed
            times.append(t)
            strength.append(round(st / k, 4))
            out += packed[1]
        return times, bytes(out), strength


def half(vals) -> bytes:
    """Band dollars -> $K as little-endian half floats."""
    return struct.pack(f"<{len(vals)}e", *(min(65_000.0, v / K) for v in vals))


def snap_res(want: float) -> int:
    """The finest resolution no finer than `want` seconds a column."""
    return next((r for r in RESOLUTIONS if r >= want), RESOLUTIONS[-1])


class Heatmap:
    """The recorders, and the page's answers."""

    def __init__(self, market, session, whales=None, clock=time.time):
        self.market, self.session, self.whales, self.clock = market, session, whales, clock
        self.rec: dict[str, Recorder] = {}
        self.wanted: dict[str, float] = {}

    def _keep(self) -> None:
        now = self.clock()
        keep = sorted((s for s, t in self.wanted.items() if now - t <= WATCH_S), key=lambda s: -self.wanted[s])[:MAX_COINS]
        for s in list(self.rec):
            if s not in keep:
                r = self.rec.pop(s)
                if r.task:
                    r.task.cancel()
                log.info("heatmap %s: no longer recorded", s)

    def want(self, sym: str) -> Recorder:
        self.wanted[sym] = self.clock()
        self._keep()
        r = self.rec.get(sym)
        if r is None:
            r = self.rec[sym] = Recorder(sym, self.market, self.session, self.clock)
            log.info("heatmap %s: recording the mainnet book", sym)
        if r.task is None or r.task.done():
            r.task = asyncio.create_task(r.run())
        return r

    @staticmethod
    def rows_for(lo_p: float, hi_p: float, mid: float, step: float) -> tuple[int, int]:
        """The bands from lo_p to hi_p, snapped to 64-band blocks (the page keeps its columns while the rows stay the
        same), at most MAX_ROWS around `mid`."""
        lo_bin, hi_bin = int(lo_p // step) // 64 * 64, (int(hi_p // step) // 64 + 1) * 64
        if hi_bin - lo_bin > MAX_ROWS:
            mid_bin = int(mid // step) // 64 * 64
            lo_bin, hi_bin = mid_bin - MAX_ROWS // 2, mid_bin + MAX_ROWS // 2
        return lo_bin, hi_bin - lo_bin

    async def view(self, sym: str, interval: str, t0: float | None = None, t1: float | None = None,
                   res: float | None = None, plo: float | None = None, phi: float | None = None,
                   after: float = 0.0) -> dict:
        """The chart's answer: the candles, and the heat for the window it shows (t0..t1 seconds, prices plo..phi) at
        `res` seconds a column (snapped to RESOLUTIONS, coarser when the window would need more than MAX_COLS).
        `after`: only the columns from this time on (the page has the older ones)."""
        sym = self.market.check_symbol(sym)
        if interval not in STEPS:
            raise ValueError(f"interval must be one of {', '.join(STEPS)}")
        r = self.want(sym)
        kl = await self.market.klines(sym, interval)
        iv = STEPS[interval]
        now = self.clock()
        t0 = kl[0][0] / 1000 if t0 is None else max(float(t0), now - KEEP_S)
        t1 = now + iv if t1 is None else float(t1)
        if t1 <= t0:
            raise ValueError("the window must end after it starts")
        res = snap_res(max(float(res or SAMPLE_S), (t1 - t0) / MAX_COLS))
        out = {"symbol": sym, "interval": interval, "klines": kl, "status": r.status, "since": r.since,
               "columns": len(r.cols), "sample_s": SAMPLE_S, "res": res, "source": "Binance mainnet futures book",
               "step": r.step, "grid": None, "book": None, "liqs": [], "trades": [], "t0": t0, "t1": t1, "now": now}
        if self.whales is not None:
            mk = self.whales.markers(sym)
            first = kl[0][0]
            out["liqs"] = [q for q in mk.get("liqs", []) if q["ts"] >= first]
            out["trades"] = [x for x in mk.get("trades", []) if x["ts"] >= first]   # the volume bubbles
        if r.step is None:
            return out
        if plo is None or phi is None:
            seen = [k for k in kl if t0 * 1000 <= k[0] < t1 * 1000] or kl[-60:]
            lo_p, hi_p = min(k[3] for k in seen), max(k[2] for k in seen)
            pad = (hi_p - lo_p) * 0.5 or hi_p * 0.002
            plo, phi = lo_p - pad, hi_p + pad
        lo_bin, n = self.rows_for(float(plo), float(phi), kl[-1][4], r.step)
        times, data, strength = await r.grid(max(t0, float(after or 0)), t1, res, lo_bin, n)
        out["grid"] = {"lo_bin": lo_bin, "rows": n, "times": times, "partial": bool(after), "strength": strength,
                       "unit": K, "data": base64.b64encode(data).decode()}
        if r.book.synced:
            out["book"] = {"mid": r.book.mid(), "strength": round(r.book.strength(r.book.mid()), 4),
                           "bids": [round(v) for v in r.book.bands(r.step, lo_bin, n, "bid")],
                           "asks": [round(v) for v in r.book.bands(r.step, lo_bin, n, "ask")],
                           "best_bid": max(r.book.bids), "best_ask": min(r.book.asks)}
            out["book_age"] = round(now - r.cols[-1][0], 2) if r.cols else None
        return out
