"""The liquidity heatmap (web/heatmap.html): where resting orders sat over time, price band by price band.

For each coin the page opens, a local copy of Binance's MAINNET futures order book is kept live from the diff-depth
WebSocket (<sym>@depth@500ms) on top of one 1000-level REST snapshot (weight 20, only when (re)connecting), following
Binance's sync rules (drop events before the snapshot, then every event's `pu` must be the last event's `u`, else
resync). Every SAMPLE_S the book is summed into price bands (BIN_FRAC of the price wide, +-RANGE around it) and kept
as one column, for KEEP_S. The diff stream reports changes across the whole book, so bands far from the price fill in
as their orders change: the first minutes after a coin is opened show mostly the snapshot's +-0.2% (on BTC).

A coin is recorded while the page asked about it in the last WATCH_S, at most MAX_COINS at a time, in memory only (a
restart starts over). Read-only: nothing here trades.
"""
import array
import asyncio
import base64
import json
import logging
import math
import time
from collections import deque

log = logging.getLogger("heatmap")

WS_URL = "wss://fstream.binance.com/ws/{sym}@depth@500ms"     # book streams stay on the root path (not /market)
SAMPLE_S = 5.0               # one column this often
KEEP_S = 86_400.0            # columns kept this long
WATCH_S = 6 * 3600.0         # a coin not asked for in this long stops being recorded
MAX_COINS = 3                # coins recorded at once (the most recently asked)
BIN_FRAC = 0.0004            # a band is about this share of the price (rounded to a 1/2/2.5/5 x 10^n step)
RANGE = 0.04                 # columns cover +-this share of the price
BOOK_SPAN = 0.10             # levels further than this from the price are dropped from the local book
SNAPSHOT = 1000              # levels a side in the REST snapshot
MAX_ROWS = 384               # rows in one answer
MAX_COLS = 600               # columns in one answer
STEPS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400}


def nice_step(price: float) -> float:
    """The round band height nearest price x BIN_FRAC: 1, 2, 2.5 or 5 x 10^n (BTC at 63,000 -> 25)."""
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
    """One coin: the WebSocket, the local book and its columns (t, lo_bin, band dollars)."""

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
        col = self.book.bands(self.step, lo, n)
        self.cols.append((now, lo, col))
        self.since = self.since or now
        for res, roll in self.rolls.items():
            self._add(roll, res, now, lo, col)
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

    # ---- buckets: per answer resolution, running sums of the columns (built once, then one column at a time) --------
    def _add(self, roll: dict, res: float, t: float, lo: int, vals: array.array) -> None:
        b = int(t // res)
        acc = roll.get(b)
        if acc is None:
            roll[b] = [lo, array.array("f", vals), 1]
            return
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

    def rollup(self, res: float) -> dict:
        """{bucket index: [lo_bin, band dollar sums, columns]} for this resolution, kept up to date from now on."""
        roll = self.rolls.get(res)
        if roll is None:
            roll = self.rolls[res] = {}
            for t, lo, vals in list(self.cols):
                self._add(roll, res, t, lo, vals)
            while len(self.rolls) > 4:                            # an old resolution nobody asks for any more
                del self.rolls[next(iter(self.rolls))]
        return roll

    def grid(self, t0: float, t1: float, res: float, lo_bin: int, n: int) -> tuple[list[float], list[int], bytes]:
        """The buckets between t0 and t1 over bands [lo_bin, lo_bin + n): their start times, column counts, and the band
        dollar SUMS as float32 rows (the page divides by the count)."""
        roll = self.rollup(res)
        times, counts, out = [], [], bytearray()
        for b in sorted(roll):
            t = b * res
            if t < t0 - res or t >= t1:
                continue
            lo, arr, k = roll[b]
            row = array.array("f", bytes(4 * n))
            a, z = max(lo, lo_bin), min(lo + len(arr), lo_bin + n)
            if z > a:
                row[a - lo_bin: z - lo_bin] = arr[a - lo: z - lo]
            times.append(t)
            counts.append(k)
            out += row.tobytes()
        return times, counts, bytes(out)


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
    def rows_for(kl: list, step: float) -> tuple[int, int]:
        """The bands shown: the candles' range with a margin, snapped to 32-band blocks so it stays put while the price
        moves inside it (the page keeps its buckets as long as the rows are the same), at most MAX_ROWS."""
        lo_p, hi_p = min(k[3] for k in kl), max(k[2] for k in kl)
        pad = (hi_p - lo_p) * 0.1 or hi_p * 0.002
        lo_bin, hi_bin = int((lo_p - pad) // step) // 32 * 32, (int((hi_p + pad) // step) // 32 + 1) * 32
        if hi_bin - lo_bin > MAX_ROWS:
            mid_bin = int(kl[-1][4] // step) // 32 * 32
            lo_bin, hi_bin = mid_bin - MAX_ROWS // 2, mid_bin + MAX_ROWS // 2
        return lo_bin, hi_bin - lo_bin

    async def view(self, sym: str, interval: str, candles: int = 200, per_candle: int = 4, after: float = 0.0) -> dict:
        """The chart's answer. `after`: only the buckets from this time on (the page has the older ones) when the rows
        are still the ones it has."""
        sym = self.market.check_symbol(sym)
        if interval not in STEPS:
            raise ValueError(f"interval must be one of {', '.join(STEPS)}")
        r = self.want(sym)
        kl = (await self.market.klines(sym, interval))[-max(20, min(int(candles), 300)):]
        iv = STEPS[interval]
        res = max(SAMPLE_S, iv / max(1, min(int(per_candle), 12)))
        t0, t1 = kl[0][0] / 1000, kl[-1][0] / 1000 + iv
        if (t1 - t0) / res > MAX_COLS:
            res = math.ceil((t1 - t0) / MAX_COLS / SAMPLE_S) * SAMPLE_S
        out = {"symbol": sym, "interval": interval, "klines": kl, "status": r.status, "since": r.since,
               "columns": len(r.cols), "sample_s": SAMPLE_S, "res": res, "source": "Binance mainnet futures book",
               "step": r.step, "grid": None, "book": None, "liqs": []}
        if self.whales is not None:
            out["liqs"] = [q for q in self.whales.markers(sym).get("liqs", []) if q["ts"] >= t0 * 1000]
        if r.step is None:
            return out
        lo_bin, n = self.rows_for(kl, r.step)
        times, counts, data = r.grid(max(t0, after), t1, res, lo_bin, n)
        out["grid"] = {"lo_bin": lo_bin, "rows": n, "times": times, "counts": counts, "partial": after > 0,
                       "data": base64.b64encode(data).decode()}
        if r.book.synced:
            out["book"] = {"mid": r.book.mid(),
                           "bids": list(r.book.bands(r.step, lo_bin, n, "bid")),
                           "asks": list(r.book.bands(r.step, lo_bin, n, "ask"))}
        return out
