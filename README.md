# binance-arb-bot

Scans 50+ Binance **USD-M perpetuals** against their **spot** counterparts every second, finds
perp/spot basis dislocations, and trades them as hedged cash-and-carry positions (long spot + short
perp). Includes a live multi-market dashboard, risk controls, a paper-trading mode (default) and an
opt-in live executor.

## Quick start

```bash
cd ~/Documents/binance-arb-bot
.venv/bin/python run.py --feed sim          # offline demo with synthetic dislocations
.venv/bin/python run.py                     # REAL Binance WebSocket data (REST fallback), PAPER trading, no keys
```

Open <http://127.0.0.1:8765>. Run the tests with `.venv/bin/python -m unittest tests.test_core`.

| Command | What it does |
|---|---|
| `run.py --feed sim` | Offline simulator. Dislocations are synthetic. State kept separately. |
| `run.py` | Live public Binance data over WebSockets (REST fallback), **paper** fills (no orders ever sent). |
| `run.py --feed rest` | REST polling only (1 Hz). |
| `run.py --no-trade` | Monitor only. |
| `run.py --live --testnet` | Real order flow against Binance **testnet**. Needs testnet API keys. |
| `run.py --live` | Real money. Also needs `ARBBOT_LIVE_ACK` (see below). |
| `Start Binance Bots.command` | Double-click (macOS): starts the testnet bot (port 8767, mirroring the trend, swing and smc accounts), and the real-money mainnet bot (port 8770) only if you say yes, its read-only `--check` passes and you type `TRADE REAL MONEY`. The testnet bot starts paused; the mainnet bot runs `--no-trade` (monitor only: it never opens arbitrage entries, and Resume cannot turn them on), so its only real orders are the ones you place in its dashboard's order ticket (`--real-ticket`). Both keep running after the window closes. `Stop Binance Bots.command` stops them. The testnet bot also runs the [signal auto-trader](#signal-auto-trader-testnet-only) (`--signal-trader`): it trades the terminal's combined signal with test money. |
| `Run Backtests.command` | Double-click (macOS): a menu for the backtests below, with no orders and no account access. It covers the swing strategies on 4h or daily candles (or your own choice of strategy, coins, candle size, days and risk), the journal summary, and the arbitrage replay, sweep and slippage over `data/recordings`. Each run is also saved to `logs/backtests/`: the report as `.txt`, swing trades as `.csv`. |

Useful flags: `--max-markets N`, `--interval S`, `--min-samples N`, `--port P`, `--no-commands` (Telegram alerts
without commands, for a second bot: only one bot per Telegram token may poll for commands).

## Dashboards on your phone (same Wi-Fi)

Both bots start with `--lan` from `Start Binance Bots.command`, so their dashboards also open on a phone that is on the
**same Wi-Fi / router** as this Mac.

| Bot | On this Mac | On your phone |
|---|---|---|
| MAINNET (real money) | http://127.0.0.1:8770 | `http://<computer's Wi-Fi address>:8770/?key=<mainnet key>` |
| TESTNET (demo, swing kit, signal auto-trader) | http://127.0.0.1:8767 | `http://<computer's Wi-Fi address>:8767/?key=<testnet key>` |
| PAPER (live prices, never an order) | http://127.0.0.1:8765 | `http://<computer's Wi-Fi address>:8765/?key=<paper key>` |

The Mac's Wi-Fi address was `192.168.100.9` when this was written; it can change after a router restart.

**Pages** (the same on the Mac and the phone; the menu under the header switches between them):

| Address | What it shows |
|---|---|
| `/` | Only the P&L reports, daily / weekly / monthly: arbitrage, swing kit (demo account or paper), futures account |
| `/arbitrage` | Summary cards, markets, open positions, recent trades, event log |
| `/swing` | Swing kit accounts, funds, open positions (and whether each is on the demo account), setups, trades |
| `/market` | Chart, order book, order ticket, positions and orders of the futures account |
| `/terminal` | The dense trading terminal: live chart, signals, order book, whales, macro (see [Terminal and Heatmap](#terminal-terminal-and-heatmap-heatmap)) |
| `/heatmap` | The liquidity heatmap: the Binance order book recorded over time under the candles |
| `/trading` | The Freqtrade trading bot |

The header (LIVE / TESTNET badge, Pause or Resume entries, Flatten all, theme) is on every page.

**Get your phone links** (they are printed with the key filled in, every time the bots start):

```bash
grep -h phone: ~/Documents/binance-arb-bot/logs/mainnet-bot.out ~/Documents/binance-arb-bot/logs/testnet-bot.out
```

The `Start Binance Bots.command` window shows the same links ("on your phone (same Wi-Fi): …"). The Mac's current
Wi-Fi address on its own: `ipconfig getifaddr en0`.

**First time on the phone:** send the link to the phone (AirDrop, Notes), open it in Safari or Chrome, then bookmark the
page. The phone remembers the key (a cookie for 180 days), so afterwards `http://192.168.100.9:8767` alone works.

**The key, and why it is not written here.** The dashboard can pause, flatten and, on mainnet, place REAL-MONEY orders,
so every device other than this Mac needs the key; without it, it only gets a "This dashboard is locked" page. The
keys live only on this computer, in `data/lan-key-<port>.txt` (`8770` mainnet, `8767` testnet, `8765` paper); `data/` is
never committed, so they are not in this README or on GitHub. Do not share a phone link: anyone with it can use that
dashboard while on your Wi-Fi.

* **Plain HTTP, so trust the network.** The page, the key and the key cookie travel unencrypted. On a home Wi-Fi with a
  WPA2/WPA3 password that only you use, that is fine; on a shared, office or public Wi-Fi, another device that can watch
  the traffic could copy the key and use the dashboard, including the mainnet order ticket. Do not use `--lan` there;
  the mainnet bot also prints this warning at start.
* **Revoke access** (lost phone, shared link): delete the bot's `data/lan-key-<port>.txt`, restart
  the bots (Stop, then Start); new keys are made and the old links and cookies stop working.
* **The phone cannot connect:** check it is on the same Wi-Fi (not mobile data); NordVPN can block local network
  traffic, so allow LAN / local network access in its settings or turn it off; after a router restart the address may
  have changed, so get the links again with the command above.
* **Without the launcher:** add `--lan` to any `run.py` dashboard command; without `--lan` the dashboard stays on this
  Mac only (127.0.0.1).

## Running the bots on this computer (Linux, `/srv/apps`)

How the bots actually run here (checked 2026-11-19). The bots do **not** run from this git folder: they run from a
second checkout, **`/srv/apps/binance-arb-bot`**, with its own `.venv`, `.env` (API keys) and `data/`. Code is changed
and committed here, then copied over (step 2 below).

| Bot | Port | Start | Stop | Status / log |
|---|---|---|---|---|
| **Testnet** (test money, swing mirror, signal auto-trader) | 8767 | `sudo systemctl start arbbot-testnet` | `sudo systemctl stop arbbot-testnet` | `systemctl status arbbot-testnet` · `journalctl -u arbbot-testnet -f` |
| **Mainnet** (REAL money: monitor only + your order ticket, max 100 USDT an order) | 8770 | `~/vps-kit/apps/binance-arb-bot/mainnet.sh start --signal-button` (`--signal-button`: the terminal's ▶ Trade it button on signals, you confirm each order, never automatic; leave it out for the ticket only; `--no-ticket`: no order button at all) | `~/vps-kit/apps/binance-arb-bot/mainnet.sh stop` | `mainnet.sh status` · `/srv/apps/binance-arb-bot/logs/mainnet-bot.out` |
| **Paper** (live prices, simulated fills) | 8765 | `sudo systemctl start arbbot-paper` | `sudo systemctl stop arbbot-paper` | `systemctl status arbbot-paper` |

* **Run only one of them:** stop the others first, e.g. testnet only = `mainnet.sh stop`, then
  `sudo systemctl start arbbot-testnet`; mainnet only = `sudo systemctl stop arbbot-testnet`, then `mainnet.sh start`.
  Stopping a bot leaves its open positions open on Binance.
* **After a reboot:** `arbbot-testnet` stays off (it is disabled at boot; `sudo systemctl enable arbbot-testnet` makes it
  start by itself), `arbbot-paper` tries to start (it is enabled; `sudo systemctl disable arbbot-paper` stops that) and
  the mainnet bot only ever starts by hand.
* **The testnet unit's flags** (signal auto-trader, swing mirror, `--lan`) are in the drop-in
  `/etc/systemd/system/arbbot-testnet.service.d/override.conf`.
* **Read-only key check before mainnet** (sends no order):
  `cd /srv/apps/binance-arb-bot && set -a && . ./.env && set +a && .venv/bin/python run.py --check --config config.toml`.
  Your home IP changes from time to time; when Binance answers `-2015 Invalid API-key, IP, or permissions`, add the
  new IP to the API key's allow-list on Binance.
* **Which address for the phone:** `hostname -I` (the first address); it changes between networks. The phone link is
  `http://<that address>:<port>/?key=$(cat /srv/apps/binance-arb-bot/data/lan-key-<port>.txt)`.

**Deploying a change** (code is edited and committed in this folder):

```bash
# 1. test and commit here
.venv/bin/python -m unittest discover -s tests -t .
git commit -am "…"
# 2. copy the commits into the running checkout (keeps its own config.toml edit)
cd /srv/apps/binance-arb-bot && git pull --ff-only ~/Desktop/binance-arb-bot-main/binance-arb-bot main
# 3. restart the bot that should run it (page-only changes in web/*.html are live without a restart: just reload)
sudo systemctl restart arbbot-testnet          # testnet
~/vps-kit/apps/binance-arb-bot/mainnet.sh restart --signal-button   # mainnet (~30 s to 3 min to stop; the flags
                                                                    # are not remembered: repeat --signal-button)
# 4. publish
git push origin main
```

On macOS the launcher scripts below (`Start Binance Bots.command`, `start-bots.sh`) still do the same from the project
folder.

## The strategy, and what it can honestly deliver

### Core entry formula (all in basis points)

```
B_mid  = (perp_mid − spot_mid) / spot_mid            B_entry = (perp_bid − spot_ask) / spot_ask   (executable)
z      = (B_mid − μ) / σ                              μ, σ = median and 1.4826·MAD of the last 5 min of B_mid
target = μ + 0.5σ + ExitCost                          ExitCost = p75 of recent exit spreads (never below today's)
Net    = (B_entry − target) + ExpectedFunding − Fees − Slippage − RiskBuffer
```

Enter only if `z ≥ 3` **and** `Net ≥ 1 bp` **and** the perp is at/above spot. What makes it conservative:

* **Robust baseline.** Median/MAD, so the dislocation itself can't inflate its own yardstick.
* **Frozen at entry.** For an open trade μ and σ are frozen; convergence means `(B_mid − μ_entry)/σ_entry ≤ 0.5`.
  A rolling baseline that the spike has dragged upward could declare "converged" while the basis never came back.
* **Discrete funding.** Funding counts only for funding events actually crossed during the expected hold, using each
  coin's real interval (in live data 32 of 55 coins settle every 8 h, 22 every 4 h, 1 every hour) and exchange caps.
* **Risk buffer.** A floor (`risk_buffer_bps`: **8 bps** in the shipped `config.toml`, because the first real paper
  trades missed their predictions by 30-180 bps; the code default is 2), rising to the 95th percentile of
  `predicted − realised` P&L over the last 100 trades (capped at 30 bps). If the model keeps over-promising,
  entries dry up until reality matches.
* **Thin-touch guard.** A market whose best bid or ask holds less than `min_touch_depth_usd` (**$300**) is skipped.
  Thin books produce fake signals: AVAUSDT showed a +69 bps "edge" on ~$50-110 of touch liquidity while the spot
  price was simply dumping, and lost 111 bps in 3 seconds.
* **Decide on fresh quotes.** The order-book request takes time and the market keeps moving. After it returns, the
  signal is **re-evaluated on the freshest quotes** and the trade is skipped if it has gone (`SKIP … signal gone`).
  The book request has a 1.5 s timeout, the entry basis recorded is the one **actually obtained** from the fill
  prices, and the position's open time is stamped at the real fill so `min_hold_s` counts real seconds.
* **Size-dependent edge.** Right before entering, both order books are walked (VWAP for the real size) and the
  bot takes the largest of `[100%, 75%, 50%, 35%, 20%]` of the target size whose Net still clears the bar.
* **P&L stop.** Exit when closing now would lose more than the round-trip cost plus 25 bps (real leg prices, funding,
  fees); plus a mid-basis stop, so a momentarily wide spread alone can't trigger it.

With default VIP-0 fees the round trip costs **34 bps** (live mode replaces this with your account's real fee tier),
so only dislocations well above that qualify. The dashboard and `/market BTC` show each market's net edge so you
can see exactly why nothing is firing.

**Where the 34 bps comes from:** `2 × (5 futures + 10 spot) + 4 × 1 slippage`, that is 30 bps of taker fees (entry and
exit on both legs) plus 4 bps of assumed slippage on the four fills. `ExpectedFunding` counts only funding
settlements crossed in the assumed 300 s hold (`expected_hold_s`), so most trades earn none. The baseline needs
`min_samples` = 60 seconds of data before any signal, and uses the last `lookback` = 300 seconds.

**Worked example** (basis points; μ = 3, σ = 2, exit cost 3, funding +1, buffer 8):

```
target = 3 + 0.5·2 + 3 = 7
Executable entry basis 45:  gross = 45 − 7 = 38   Net = 38 + 1 − 34 − 8 = −3   → no trade (a real gap, but too small)
Executable entry basis 60:  gross = 60 − 7 = 53   Net = 53 + 1 − 34 − 8 = 12   → trade (z = (62 − 3)/2 ≈ 30 ≥ 3,  Net ≥ 1)
```

Be realistic about the edge: on liquid USDT perps the basis normally sits within a few bps of spot, and
the rare large gaps are contested by co-located market makers with maker-fee rebates. A bot polling REST
once per second will usually be late to them. Paper mode exists so you can measure the *actual* net P&L
before risking money. Lowering fees (BNB discount, VIP tier, maker orders) is what widens the opportunity
set; edit `[fees]` in `config.toml` to match your account.

Only the **positive-basis** side is traded (buy spot, short perp), because the reverse needs borrowed spot.

## Backtesting: recorder and replay

On a real feed the bot records what it sees to `data/recordings/quotes-YYYY-MM-DD.jsonl.gz` (one gzip file per UTC
day, safe to read while the bot runs, ~100-130 MB/day; the oldest files are deleted beyond `[recorder] max_mb`):
top-of-book quotes for every fresh market each second, mark/index/funding, one **order-book snapshot per second**
(round-robin over all markets), the books fetched at each signal, and the bot's own entries and exits.
The simulator is never recorded. A recording failure disables recording only; it can never affect trading.

```bash
.venv/bin/python backtest.py info                    # what has been recorded
.venv/bin/python backtest.py slippage --sizes 200,1000,5000,25000
.venv/bin/python backtest.py replay                  # the real strategy over the recorded days
.venv/bin/python backtest.py replay --set strategy.z_entry=4 --set fees.futures_taker_bps=4
.venv/bin/python backtest.py replay --sweep strategy.z_entry=2.5,3,4,5     # one row per value
.venv/bin/python backtest.py replay --no-depth       # top-of-book only, to see what depth-aware sizing is worth
```

* **`slippage`** measures book impact per fill (bps vs the touch) by size from the recorded snapshots, the share of
  snapshots too thin to fill at all, the hardest markets, and compares the p75 with your `[fees] slippage_bps`.
* **`replay`** drives the same Engine, Scanner, Risk and PaperExecutor as the live bot on a simulated clock, so a
  parameter change shows exactly what it would have done on those days: trades, win rate, P&L per day, max drawdown,
  exit reasons, and the model error (predicted − realised bps) that feeds the adaptive risk buffer.
* **Honest limits.** Replay sizes and fills entries from the most recent recorded snapshot of that market (≤120 s old,
  `--depth-age`), otherwise top-of-book. Exits fill at top-of-book plus the configured slippage. Latency slippage
  (price moving between decision and fill) can't be measured from recordings, only from live fills, so keep
  `slippage_bps` as a cushion for it. A few days of paper data is anecdote, not proof: sweep widely, then check that
  the parameters you like still work on days they weren't tuned on.

## Swing trading, trend following and Smart Money Concepts (`swingkit/`)

A separate toolkit for the timescale where retail traders can compete: candles of hours to days, positions held days to
weeks, so fees and latency matter far less than they do for arbitrage. It follows the three rules that matter most:
**backtest first, risk only 1-2% per trade, keep a journal.** It is research and paper only: nothing here places an order,
and it shares no state with the arbitrage bot. (The one exception is opt-in and testnet-only: `--swing-mirror` copies the
chosen accounts' new trades to the futures testnet; see the order ticket section.)

```bash
.venv/bin/python swing.py download --days 730            # cache 4h candles + real funding history (BTC ETH SOL BNB XRP)
.venv/bin/python swing.py backtest                       # all three strategies, long and short
.venv/bin/python swing.py backtest --strategy trend --symbols BTCUSDT,ETHUSDT --set trend.slow=100 --no-short
.venv/bin/python swing.py backtest --interval 1d --days 1460 --journal --csv data/trades.csv
.venv/bin/python swing.py scan                           # what the strategies say about the latest closed candle
.venv/bin/python swing.py journal --summary              # also: --last 20, --note 12 "text", --export trades.csv
```

| Strategy | The rule, written out |
|---|---|
| `trend` | Trend following. Close above the 20-candle high while EMA50 > EMA200 (mirror for shorts). Stop 3 ATR away, trailed up as a chandelier; exit on a close beyond the 10-candle low. No profit target: winners run. |
| `swing` | Swing trading. In an EMA50 > EMA200 uptrend, RSI dips to 40 or lower near the EMA50, then turns up with an up candle. Stop below the last 10 candles, target 2R, time stop after 30 candles. |
| `smc` | Smart Money Concepts as mechanical rules: a candle sweeps below the latest confirmed swing low and closes back above it (liquidity sweep); within 10 candles a close breaks the range high (break of structure); the order block is the last down candle before that break; enter when a later up candle dips into the block, stop beyond the block and the sweep, target 2.5R. SMC is a discretionary craft: this is one explicit, testable reading of it, not *the* method. |

Each strategy is written once for longs and run on the sign-flipped price series for shorts, so the two sides cannot drift apart.

**Risk.** Every trade is sized so being stopped out costs `risk_pct` of current equity: 1% by default, and **2% is a hard
ceiling** that no setting can lift (`--risk-pct 3` is refused). Position size is capped by leverage (3x), total open risk
("heat", 5%) and the number of open positions (5). After a 25% drawdown new entries pause for 14 days and then start over
from the current equity, because a halt that never lifts would freeze a strategy for good.

**How the backtester stays honest** (each rule has a test): a strategy decides at a candle's close from candles up to it and
the order fills at the *next* open, so there is no look-ahead (also proven by running every strategy on a prefix and on the
full series and requiring identical decisions); stops and targets are checked inside the candle, the stop wins a tie, and a gap
through a stop fills at the open; every fill pays the taker fee and slippage; funding is the **real Binance funding history**
(longs pay a positive rate, shorts receive it) and a coin without history falls back to a flat charge; positions still open at
the end are closed and marked "end of data". The report always shows what simply holding the coins would have returned.

**Journal.** `data/swing-journal.db` records every trade with the reason it was taken, stop, target, result in R, how it
ended and your own notes; backtest runs (`--journal`) and paper trades live in the same table, tagged by mode.

### Paper trading live (forward testing)

A backtest only tells you about the past. To see whether the same rules hold up on candles that have not happened yet, run them
live on paper. It starts **three paper accounts** (trend, swing, smc), $10,000 each, and risks 1% per trade:

```bash
.venv/bin/python swing.py run                       # alerts to Telegram + the journal; Ctrl-C stops it
.venv/bin/python swing.py run --once                # one refresh, print the status and any setups, and exit
.venv/bin/python run.py --swing                     # the arbitrage bot AND the swing paper accounts in one process
                                                    #   (adds the /setups /swing /journal Telegram commands)
```

* **Which markets.** `run.py --swing` paper-trades the **same markets the arbitrage bot scans** (`--swing-symbols arb`, the
  default; 55 with the standard universe). `swing.py run` defaults to BTC ETH SOL BNB XRP; give it `--symbols top55` (the 55
  most traded USDT perpetuals) or your own comma separated list. The list is **frozen when a paper run starts** and stored in
  the journal, so the forward test cannot change retroactively when the volume ranking moves; `--swing-restart` (or
  `--restart`) begins a new run, with a new start time, on a fresh list (old trades stay in the journal). A market with too
  little history or a failing download is skipped and named (`/swing`, the dashboard), never fatal for the others. Each
  account still risks 1% per trade and holds at most 5 positions / 5% total risk, so 55 markets give more *chances*, not
  more risk.
* **Light on Binance.** It only asks for data when a 4h candle boundary has passed (one small top-up per market, six at a
  time) and only replays when something changed, so it adds almost nothing next to the arbitrage feed.
* **No second engine.** Every poll (2 minutes) the runner replays the real candles from the day paper trading started
  through the *same backtester*, in its live mode, so forward results follow exactly the rules that were backtested. A
  decision is only ever taken on a **closed** candle; the still-forming candle may fill a waiting order at its open (and
  catch a stop that gapped), nothing more.
* **Reported once.** New entries and exits are written to the journal (`mode = paper`) and sent to Telegram, and a setup is
  announced the moment it is decided ("paper entry at the next 4h open"). The journal remembers what was reported, so
  restarting the bot never duplicates a trade or an alert.
* **Telegram (with `run.py --swing`, because only one program may read a bot's messages):** `/setups` setups waiting for
  the next open, `/swing` the three accounts with equity and open trades, `/journal [n]` the paper stats and the last *n*
  trades with the reason for each. `swing.py run` on its own sends alerts but cannot answer commands.
* **On the dashboard.** With `run.py --swing` the web dashboard (local, and the hosted read-only copies once they are
  redeployed) gets a full-width **"Swing paper accounts"** panel: the three accounts (equity, return, open and closed
  trades, win count, average R), the open swing positions with entry, current price, stop, target and P&L in dollars and R,
  the setups waiting for the next candle open with the reason, and the last ten closed trades. Without `--swing` the panel
  says so instead of showing zeros. The data is the same sanitised snapshot (no keys, no controls); after changing
  `web/index.html` rebuild the hosted page with `scripts/build_hosted_dashboard.py` (a test fails if it is out of date).
* **On / off.** The swing panel's *Toolkit ON/OFF* switch (local dashboard) or `/swing off` and `/swing on` in Telegram.
  OFF means the trend, swing and SMC accounts take **no new entries** and announce no setups; positions already open keep
  their stops, targets and exits (the candles keep being read for that, and an exit is still reported). ON again: they
  trade from that moment; the off time is skipped, never back-filled. The switch is saved in the journal (`paper_off`,
  every off window), so it survives a restart, and the replay honours the windows (`run_backtest(..., paused=...)`).
  It only exists when the bot runs with `--swing`.
* **Changing a fund from the web.** On the local dashboard (not the hosted read-only copies) the swing panel has a *Fund
  per account* row: type an amount from $100 to $10,000,000 for trend, swing or smc and press Set (it asks first). Position
  sizes follow the starting fund, so that **one account restarts from now**: its open paper trades are closed at their last
  price (reason "account reset: fund changed"), its stats start again from zero, the other two accounts are untouched, and
  the old trades stay in the journal. The change is saved, so it survives a restart, and is announced on Telegram. It uses
  `POST /api/swing-capital`, guarded by the same per-run token as the pause and flatten buttons.
* **Market panel (local dashboard).** Below the swing accounts, a Binance-style market view of USD-M futures: a candlestick
  **chart** (1m to 1W, MA 7/25/99, volume, crosshair, mouse-wheel zoom) with the swing paper positions on that market drawn
  as entry / stop / target lines; the **order book** (8 levels each side, size and running sum in USDT, spread, last and
  mark price); recent **trades**; **top movers** (24h gainers, losers, volume among liquid USDT perpetuals; click one to
  chart it); **Info** (24h stats, mark/index price, funding rate and countdown, open interest) and **Trading Data**
  (hourly accounts long %, taker buy/sell ratio, open interest). The page asks the bot (`GET /api/market/...`), which
  relays a fixed set of public, unsigned Binance requests with symbol/interval whitelists and 1 to 30 s caches
  (`arbbot/market.py`), so an open page costs roughly 250 request-weight a minute and nothing in it can place an order. It
  only polls while the tab is visible. Binance has no 1-second candles for futures, so the shortest interval is 1m. The
  hosted read-only copies leave the panel out.
* **Paper order ticket (local dashboard).** Beside the order book, a Binance-style ticket trades a separate **paper USD-M
  futures account** (starts with 1,000 paper USDT; the ⇆ button adds or takes out paper money) at live prices. **Nothing
  is ever sent to Binance**: it lives in `arbbot/paper_futures.py`, never touches the arbitrage executor or `live.py`, and
  every Telegram note it sends says so. It offers Limit (GTC / IOC / FOK, BBO = best bid for a buy, best ask for a sell,
  or click a book level to use its price), Market (walks the real order book, optional slippage tolerance) and
  Conditional (stop price on last or mark, releasing a limit or market order); attached TP/SL (a one-cancels-other pair of
  reduce-only stop-market orders); Reduce-Only; isolated margin with leverage 1x to 125x per market (change it while the
  market has no position or order); estimated liquidation price, cost and max size for both sides; a size slider. Below,
  **Positions / Open Orders / Order History / Trade History / Assets** with market close and cancel buttons; the chart
  draws the paper entry, liquidation price and working orders. Fills pay the maker 0.02% / taker 0.05% fees, positions
  pay or receive the real funding at each settlement, and a position is liquidated (losing its isolated margin) when the
  mark price reaches its liquidation price. Simplified on purpose: whole fills only (no partials), a resting limit fills
  when the price reaches it (no queue position), FOK behaves like IOC, one maintenance margin rate (0.5%) for every
  market, a 5 USDT minimum order. State: `data/paper-futures.json`.
* **Order ticket on the Binance futures TESTNET.** Run `run.py --live --testnet --config config.testnet.toml` (your
  normal strategy, recorder off, dashboard on http://127.0.0.1:8767) and the same ticket sends **real orders to the
  futures testnet** (test money): the badge says TESTNET, and balance, positions (with Binance's own liquidation
  price), open orders and fills are read back from the testnet account (`arbbot/testnet_ticket.py`). Limit and market
  orders use the normal order endpoint; conditional orders and TP/SL use Binance's **Algo Order API**
  (`/fapi/v1/algoOrder`, which the normal endpoint now requires: code -4120), and TP/SL are placed as closePosition
  orders once the entry fills, then removed automatically when the position closes. TP/SL cannot be attached to a
  conditional order that has not triggered yet. The ticket refuses a mainnet connection unless you opt in with
  `--real-ticket` (below); otherwise, on paper and mainnet bots, it stays the paper account. It shares the account with the arbitrage bot,
  so the two never touch the same coin (the ticket refuses the bot's coins, the bot skips the ticket's, and the startup
  check ignores the ticket's coins, saved in `data/ticket-live-testnet-real.json`). The bot logs the testnet request
  weight every 30 s with the busiest endpoints: the testnet bans an IP that keeps exceeding 2,400 a minute, and the IP
  Binance sees may be shared with other traffic.
* **REAL-MONEY order ticket on MAINNET (opt-in).** `run.py --live --config config.toml --real-ticket` (the
  launcher adds `--no-trade`, so on mainnet only your own ticket orders are real) makes the mainnet dashboard's ticket send **real orders to your Binance
  USD-M futures account**, with the same behaviour as the testnet ticket. Guards: the badge turns red (REAL MONEY);
  every order and every Market close asks you to confirm; one order is worth at most `--real-ticket-max-usd` USDT
  (default 100; the launcher's `REAL_TICKET_MAX_USD`), closing is never capped; new orders are refused while the
  `KILL` file exists (closing still works); new coins get `execution.leverage` (2x) isolated margin until you change
  it; transfers are refused (move USDT in the Binance app). Pausing the arbitrage bot does **not** pause the ticket.
  `--real-ticket` is refused on paper and testnet bots. State: `data/ticket-live-mainnet-real.json`.
* **Signal Trade button on MAINNET (opt-in, real money, never automatic).** Add `--signal-button` (with
  `--real-ticket`; `mainnet.sh start --signal-button`) and the terminal's **▶ Trade it** button on a signal sends a
  real order to your futures account after a REAL MONEY confirm box (entry, stop, TP1, TP2, position size and the loss
  at the stop). Automatic entries are locked off: they cannot be switched on from the page or Telegram. A trade then
  manages itself like a testnet auto-trade (stop and TP2 on Binance, half at TP1 then stop to breakeven, closes if the
  signal flips or after the maximum hold). Starts at $5 risk per trade, $20 daily loss, 2 open trades, 3x leverage
  (change them in the Signals panel); each position stays under 95% of `--real-ticket-max-usd`. **Position size** (Signals
  panel) sets a fixed USDT value per new trade instead (blank = sized by the risk), and **Leverage** picks 1x to 20x; a
  trade whose margin (size ÷ leverage, +2%) is more than the account's available USDT is refused before it is sent.
  The Signals table's **Liq** column shows each signal's liquidation price with those settings (isolated margin,
  Binance's maintenance tiers per coin from `/fapi/v1/leverageBracket`, read hourly; 1% when unknown), red when it
  comes before the stop; hover for margin, fees and the loss at the stop including fees. The confirm box repeats it,
  and a trade that would be liquidated before its stop is refused. **Auto close · flip / max hold** (Signals panel,
  saved) switches off the bot's own market closes: then only the stop, TP1, TP2 on Binance (and the breakeven stop
  after TP1), or you, close a trade. State:
  `data/signal-trader-mainnet.json`.
* **Swing accounts mirrored to the TESTNET.** Add `--swing --swing-mirror trend` to the testnet command (any of `trend`,
  `swing`, `smc`, comma separated, or `all`; the launcher uses `trend,swing,smc`; `--swing-mirror-fund 10000` sets the
  size of each, default $10,000) and every **new** entry of those paper accounts is
  copied to the futures testnet through the ticket (`arbbot/swing_mirror.py`): a market order sized as if the account
  held that fund, with its stop as a real exchange-side stop-market; when the paper stop trails, the exchange stop is
  moved (only for moves of 0.1% or more); when the paper position exits, the testnet one is closed. If the exchange stop
  fires first, that is recorded and the trade is not re-entered. The paper account stays the source of truth: its
  signals still come from mainnet candles, and the testnet bot keeps its own journal (`data/swing-journal-testnet.db`).
  Skipped, and noted once: positions the account held before mirroring started, coins the arbitrage bot holds or that
  already have a testnet position, and orders the testnet refuses (below its minimum size, the stop already through the
  price). The testnet account holds one position per coin, so when two mirrored accounts trade the same coin, the
  first to open it owns it and the other skips that trade; the mirrors take turns, so they never race for a coin.
  `--swing-mirror-copy-open` (used by the launcher) also copies the positions the accounts already hold, entered at
  today's price with the paper stop. The dashboard then shows the **demo account's real results** in the swing cards
  (open positions, realised P&L after fees from Binance's own fills, today, win rate; the paper figures are in the
  tooltips), a "Demo account" column saying whether each paper position is really open on the testnet (or why not),
  and a table of the demo account's closed trades.
  It only runs with `--live --testnet`. State: `data/swing-mirror-testnet-<account>.json` (one file per account).
* **Run only one of the two at a time.** Both write the same journal; they would agree, but you would get every alert twice.
* **It is paper only and separate from the arbitrage book.** If the candle download fails the runner logs it and retries
  (30 s, doubling to 15 min); it can never stop the arbitrage bot that shares the process.

### What the first real backtest said (2 years of 4h candles, BTC ETH SOL BNB XRP, run on 2026-10-24)

| Strategy | Trades | Win % | Expectancy | Profit factor | Return | Max drawdown |
|---|---|---|---|---|---|---|
| trend | 368 | 33 | +0.09R | 1.17 | +27.7% | 27.9% |
| swing | 276 | 42 | -0.02R | 0.93 | -9.0% | 25.3% |
| smc | 113 | 30 | -0.04R | 0.93 | -6.0% | 22.0% |
| *holding the coins* | | | | | *+27.6% on average* | |

Read this carefully. Trend following matched buy-and-hold with a drawdown of the same size, which is not an edge; swing and
SMC lost money after costs. One two-year window is one market regime, five coins is a small sample, and nothing was tuned
(deliberately: tuning on the same data you judge on only finds the past). Treat it as the starting line, not a verdict. The
next honest steps are other timeframes and years (`--interval 1d --days 1460`), other coins, and parameter sweeps that
are then checked on data they were not tuned on. A backtest also cannot see how you will behave in a 28% drawdown.

## Risk controls

| Control | Behaviour |
|---|---|
| Paper by default | Real orders need `--live` **and** `execution.mode="live"` in config; mainnet also needs `ARBBOT_LIVE_ACK`. |
| Size limits | Per-position size, max open positions, total exposure cap, ≤50% of top-of-book depth, the largest size whose edge survives the real order book, and a $300 minimum touch depth. |
| Daily loss limit | UTC-day realised loss limit halts entries **and flattens**. Rebuilt from the trade log, so a restart cannot reset it. |
| Kill switch | `touch KILL` in the project folder halts and flattens within a second. Delete the file to re-arm. |
| Fault breaker | 3 consecutive execution errors, or an unhedged leg, latches a halt that needs a restart. |
| Stale data | No signals on quotes older than 3 s; positions are force-closed after 30 s of staleness. |
| Data integrity | Markets with mark ≠ index by >50 bps are skipped (halted spot, thin books); entries need the perp at/above spot. |
| Exits | Convergence against the baseline **frozen at entry** (`(B_mid − μ₀)/σ₀ ≤ 0.5`, after ≥5 s), P&L stop (close-now loss > round-trip cost + 25 bps), mid-basis stop (+25 bps), 15-minute timeout. |
| Dashboard | Binds to localhost; Host/Origin checks; control endpoints need a per-run token. |

## Architecture

```
feeds.py      WsFeed (streams) + RestFeed fallback via HybridFeed | SimFeed (offline)  ->  Board
scanner.py    robust baseline, discrete funding, exit cost, risk buffer, fully-costed net edge  ->  Signal
sizing.py     order-book walk (VWAP), size-dependent edge, REST depth fetch
risk.py       sizing, caps, thin-touch guard, cooldowns, daily loss, kill switch, fault breaker
engine.py     each tick: scan -> exits (frozen baseline, P&L stop) -> entries (fresh-quote re-check) -> snapshot
executor.py   PaperExecutor (book-VWAP fills + fees + slippage)
live.py       LiveExecutor (both legs concurrently, partial-fill rebalancing, real fee tiers, reconcile)
recorder.py   gzip-JSONL market recorder + depth sampler (data/recordings)
backtest.py   (arbbot/backtest.py) info | slippage | replay with --set / --sweep
commands.py   Telegram questions, /pause /resume, and custom paper trades /open /confirm /close (your chat only)
portfolio.py  positions + SQLite persistence (data/<mode>-<env>-<feed>.db)
server.py     aiohttp dashboard + WebSocket snapshots + guarded controls
alerts.py     Telegram notifier (queued, rate-limited, token-safe)
swingkit/     (separate toolkit, see its section) data.py candles + funding | strategies.py trend/swing/smc |
              risk.py 1-2% sizing + drawdown pause | backtest.py | journal.py | paper.py live paper runner | cli.py  <- swing.py
```

`1000SHIB`-style perps are mapped onto the unscaled spot symbol with a price multiplier.

### Data feed

* **WebSocket (default).** Futures: `!markPrice@arr@1s` (mark, index, funding) plus a `@bookTicker` per
  market; spot: a `@bookTicker` per market. Two connections, auto-reconnect with backoff.
* **REST fallback.** If either stream goes quiet for `ws_stale_s` (5 s) the bot polls REST at 1 Hz until the
  streams recover, then hands back. The dashboard shows `WebSocket`, or `REST (fallback)`.
* **Quiet books.** A book stream only emits on change, so while a connection is alive quotes that changed
  within `ws_max_silent_s` are re-confirmed each scan. A symbol silent longer than that goes stale.
* **Speed.** Data is always the latest tick; scan cadence is `feed.interval_s` (try `0.25`). REST weight at 1 Hz
  is ~900/2400 per minute, so keep REST-only scans at 1 s or slower.
* Stream hosts are overridable with `ws_futures_url` / `ws_spot_url` in `config.toml` in case Binance changes
  its endpoints; the testnet defaults are best-known values and worth confirming.

## Telegram alerts

The bot messages you about **signals** (always `LONG spot / SHORT perp`; that is the only side it trades),
**entries and exits** with P&L, **halts / resumes** (manual pause, kill file, daily loss limit), **faults**
(unhedged leg, repeated execution errors, startup mismatch), **feed health** (REST fallback, outage, recovery)
and **start / stop / crash**. You can also **ask it questions** (next section).

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `.env` as `TELEGRAM_BOT_TOKEN=`.
2. Open your new bot and press **Start**, then run `.venv/bin/python run.py --find-chat-id` and put the id in
   `.env` as `TELEGRAM_CHAT_ID=`.
3. Verify: `.venv/bin/python run.py --test-alert`. Alerts then switch on automatically at startup.

Tuning is in `[alerts]` in `config.toml` (per-category switches, one signal alert per market per 5 minutes,
10 s debounce on feed problems). Messages are queued and rate-limited, failures never affect trading, and the
token is never logged.

**Signal alerts only when the arbitrage bot would trade.** While its entries are paused (Pause, `/pause`, or the
default paused start), halted (kill file, daily loss limit, fault) or switched off (`--no-trade`, e.g. the mainnet
monitor bot), it sends **no** signal messages. You still get the pause / halt / resume messages, so you know why it
is quiet, and `/signals` still answers. To get them anyway, marked "→ NOT trading (reason)", set
`notify_signals_when_not_trading = true` under `[alerts]`.

### Ask the bot from Telegram

Type in plain words or use the `/` menu (registered automatically):

| You send | You get |
|---|---|
| "any trades long or short in the market?" or `/signals` | Live signals (LONG spot / SHORT perp) or, if none, the closest candidates and why they don't qualify, plus markets where the perp is cheaper than spot (information only) |
| "how is BTC?" or `/market BTC` | Perp/spot prices, basis vs its normal range, z-score, net edge after costs, funding, depth, and which side the trade would be |
| `/status` | Feed health, entries active/halted, open positions, P&L, uptime |
| `/positions` · `/pnl` · `/top 10` | Open positions (one summary each) · profit/loss and last trades · best markets by edge |
| `/positions BTC` (or "show me the BTC position") | **Full detail of one open trade:** AUTO or MANUAL, size, the spot and perp quantities and prices paid, time held, entry basis, fees paid, funding earned, the model's z and expected net at entry, the unwind basis now, P&L if closed now, and the exit rules with how far each is (convergence z, timeout countdown, P&L stop, basis stop) |
| `/pause` · `/resume` | Stop / allow new entries. The pause (also the dashboard's Pause and Flatten buttons) is saved in the state database, so a restart keeps entries paused until you resume; the bot says so in its log, its event list and on Telegram. **By default every start begins paused** (`[risk] start_paused = true` in `config.toml`; set it to `false` to start trading straight away) |
| `/ideas` · `/ideas 30m` | The **combined signal** (whales, crowd, curve, macro, news) with the quality check, as on the terminal's Signals panel: each busy coin's side, TRADE / NO TRADE, score, confidence, quality, regime, R:R, and for a TRADE the entry / stop / TP1 / TP2 and its pre-mortem (for a NO TRADE, why not), marked ✅ when the auto-trader may take it. Without a timeframe: the auto-trader's (1h when it doesn't run) |
| `/types` · `/types off breakout` | The **signal types**: on/off, rules, and each type's closed trades, win rate, average R and P&L; switch one off or on |
| `/auto` (or "how is the auto trader?") | The **signal auto-trader** (testnet bot only): entries on/off, its timeframe, open / most open, status (looking for entries, or why not), today's and total result, win rate, the per-trade rules and each open trade with its entry, stop, target and P&L |
| `/auto on` · `/auto off` | New entries on or off. Open trades stay managed: stop, target, signal flip, maximum hold |
| `/auto 30m` (or `/auto tf 30m`) | Trade the 30m signal from the next pass (1m 5m 15m 30m 1h 4h 1d 1w). Open trades keep the maximum hold of their own timeframe |
| `/auto max 5` | Hold up to 5 trades at once (1-20). Lowering it closes nothing |
| `/auto risk 30` · `/auto daily 150` | New trades lose about $30 at their stop (1-500; open trades keep their size) · no new trade after $150 of realised loss in a UTC day (5-5000) |
| `/auto trades` | Its open trades, waiting limit entries and the last 10 closed trades with their result in $ and R and why each closed |
| `/account` (or "my futures account") | The futures account the order ticket trades (testnet on the testnet bot): balance, equity, unrealised P&L, every position with who opened it (signal auto-trader, swing mirror, order ticket, arbitrage, or outside the bot) |
| `/setups` · `/swing` · `/journal [n]` | The swing / trend / SMC paper accounts (only with `run.py --swing`; see the swingkit section) |
| `/open BTC 200` → `/confirm` · `/cancel` | A custom **paper** trade (see below) |
| `/close BTC` | Close an open paper position now |

Safety: only your configured chat is answered (everyone else is ignored); commands sent while the bot was
offline are skipped, never replayed; plain words like "stop trading" or "open btc" never change anything (only the
explicit slash commands do); `/resume` cannot override a fault, the kill file or the daily loss limit; and no
command can place a **real** order (the trade commands refuse when the bot runs `--live`). `/auto` settings only steer
the testnet auto-trader (test money), which places orders itself, inside its own limits. Only **one** running bot
may poll a given Telegram bot token (a second one is detected and backs off). Turn it off with `commands = false`
under `[alerts]`.

### Custom paper trades from Telegram

`/open BTC 200` (market, then size in USD; the size defaults to `trade_notional_usd`) replies with a **preview**
and opens nothing. Send `/confirm` within 60 s to open it, or `/cancel`. Then `/close BTC` closes it whenever you like.

* **Long or short? Both, as one hedged pair.** `/open BTC 200` BUYS $200 of BTC spot (LONG) and SELLS $200 of BTC
  perp (SHORT) at the same time, at the current executable prices with the usual fees and slippage. It is not a bet
  on BTC going up or down: the two legs cancel the price move, and it earns only if the perp's premium over spot
  narrows. A pure long, a pure short, or the reverse pair (short spot / long perp, which needs borrowed spot) are not
  available.
* **Same limits as automatic entries:** it refuses while entries are halted (pause, kill file, daily loss limit,
  fault), when the market has no fresh quotes, is already open, would exceed `max_open_positions` or
  `max_total_notional_usd`, has a thin book (`min_touch_depth_usd`), or the size is more than half the touch depth
  (`depth_fraction`). The refusal says which. It skips only the *signal*, which is the point of choosing by hand.
  The preview is honest: with a flat basis a trade starts about the 34 bps round-trip cost under water.
* **Held until you close it.** The strategy's convergence and 15-minute timeout exits don't apply to a manual trade;
  the protective ones still do (P&L stop, +25 bps mid-basis stop, stale feed, kill file, daily loss limit, flatten).
* **Bookkeeping:** it counts in realised P&L and the daily loss limit like any paper trade, is recorded as exit
  reason `manual-close`, is flagged `manual` on the dashboard snapshot, and never feeds the adaptive risk buffer
  (no model forecast exists for it).
* **Paper only.** In `--live` mode `/open` and `/close` refuse; real orders are never placed from a chat message.

## Signal auto-trader (testnet only)

The terminal's Signals panel (`/terminal`) scores each coin from five readings (whales, crowd, futures curve, macro,
news) into LONG / SHORT / WAIT, then a **quality check** (`arbbot/quality.py`) says TRADE or NO TRADE: market regime
(trend / range / chop), four scored pillars (structure & liquidity, momentum, volume, derivatives & BTC), the
guardrails learned from losing trades ([REGIME] ADX > 30 against mean reversion, [FAKEOUT] breakouts need 1.5× volume,
[FUNDING] no crowded side while spot volume falls, [SESSION] −20% in thin hours or near a high-impact US release), a
structural stop with TP1 ≥ 1.8R and TP2 ≥ 3R, and a pre-mortem. Each idea also gets a **signal type** (funding
squeeze, sweep reversal, breakout, trend pullback, **candle reversal**, mean reversion, or a **custom type you build** from a menu of
conditions on the terminal) that sets its own entry, stop, targets, extra conditions and risk; each type can be
switched off, and its results are tracked (`/types` on Telegram). With `--live --testnet --signal-trader` the bot trades
the TRADE rows on the Binance futures **testnet** (test money; it refuses any other account). **How the signal
works, and how to start, change and troubleshoot it: [docs/SIGNAL-AUTO-TRADER.md](docs/SIGNAL-AUTO-TRADER.md).**

You can also open a signal yourself with the **▶ Trade it** button in the terminal's Why header (same entry, stop,
targets, size, exits and limits; see [Terminal and Heatmap](#terminal-terminal-and-heatmap-heatmap)).

**On MAINNET it is the Trade button only** (`mainnet.sh start --signal-button`, real money): the same class runs with
automatic entries locked off (the page, Telegram and a saved state cannot turn them on), so a trade opens only when
you press ▶ Trade it and confirm the REAL MONEY box. It starts at $5 risk per trade, $20 daily loss, 2 open trades and
3x leverage, keeps each position under the ticket's per-order cap, refuses a trade whose margin is more than the
account's available USDT or that Binance would liquidate before its stop, and saves to
`data/signal-trader-mainnet.json`. Details: [the REAL-MONEY ticket section](#paper-trading-live-forward-testing).

**What it does**

* **Enters** on a **TRADE** verdict with confidence ≥ 60 and the whale reading present, most confident coin first. The entry is a market order when the signal's entry is the price now, otherwise a limit order that is
  cancelled after half the timeframe.
* **Sizes** each trade so the stop loses about **$20**, at most **$2,000** of position, at **5x** leverage
  (`--signal-trader-leverage`). The **stop and TP2 sit on Binance**, triggered by the mark price, so they work even
  if the bot stops. **Half closes at TP1**, then the stop moves to breakeven. It skips a coin whose testnet order book is too thin for its stop.
* **Binance's own closes:** a trade closed by the exchange's auto-deleveraging (`adl_autoclose`: a profitable
  position taken away at a liquidated trader's price, absurd on the testnet) is marked as such and left out of the
  daily loss limit. A liquidation still counts.
* **Exits** on its stop or TP2, when the signal flips with confidence ≥ 60, or after holding **2× the timeframe it
  was opened on**.
* **Limits:**
  * the most open trades (positions plus waiting entries),
  * $100 of realised loss per UTC day, then no new entries,
  * a cooldown per coin after an exit,
  * the `KILL` file.

  It leaves alone coins held by the arbitrage bot, the swing mirror or the order ticket.

**What you set from the page (no restart; saved, so it survives restarts and wins over the command-line flag)**

| Setting | Terminal (`/terminal`, Signals panel) | Dashboard (`/`, "Signal auto-trader · testnet" card) |
|---|---|---|
| **Timeframe** it trades (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w) | Pick a timeframe at the top of the panel, then press the orange **Trade …** button in "Its timeframe". It reads **"= panel"** once they match. | The timeframe buttons |
| **Most open trades** (1–20, default 3) | The list in "Its open trades / most" | The **Most open** list |
| **Risk per trade**: $ lost if a new trade hits its stop (1–500, default 20) | The first box in "Risk per trade · day limit" | The **Risk $** box |
| **Daily loss limit**: no new trade after this much realised loss in a UTC day (5–5000, default 100) | The second box in "Risk per trade · day limit" | The **Day limit $** box |
| **Leverage** for new trades (1–20x, default 5x; mainnet 3x) | The list in "Leverage · new trades" | The **Leverage** list |
| **Position size**: a fixed $ position per new trade instead of sizing by risk (5+, blank = by risk; capped by the largest position) | The box in "Position size · new trades", with the margin it needs (red "too little!" when the account can't cover it) | — |
| **Auto close**: close at market on a signal flip or after the maximum hold (default ON) | The **ON · turn off / OFF · turn on** button in "Auto close · flip / max hold". OFF: only the stop, TP1, TP2 on Binance (and the breakeven stop), or you, close a trade; open trades too | — |
| **New entries** on / off (testnet; locked off on mainnet) | — | The Entries ON/OFF switch |

Leverage in Telegram: `/auto lev 3` or `/auto 3x`. Lower leverage puts its winning trades later in Binance's
auto-deleveraging queue and liquidation further away, but needs more margin per trade: with too little margin
Binance refuses the entry and it skips the coin. Open trades keep the leverage they were opened at. The **Lev**
column in the terminal's "Account positions" and "Auto-trader closed trades" tables shows each one's leverage
("—" for trades from before it was recorded); `/auto trades` and `/account` show it too.

The panel's timeframe buttons on their own only change what the table shows. The auto-trader trades one timeframe,
its own. Each change asks first, applies within 30 s and is announced on Telegram:
- **Changing the timeframe** affects new trades only: open ones keep the maximum hold of the timeframe they were opened
  on.
- **Lowering "most open"** closes nothing: it just takes no new trade until fewer are open.
- **Entries OFF** still manages the open trades.

**Set with flags (restart needed):**
- `--signal-trader-max-usd` (2000)
- `--signal-trader-min-conf` (60)
- `--signal-trader-window` (1h), `--signal-trader-max-open` (3), `--signal-trader-risk-usd` (20),
  `--signal-trader-daily-loss-usd` (100) and `--signal-trader-leverage` (5), which only apply until you pick a value
  on the page or in Telegram

State: `data/signal-trader-testnet.json` (mainnet: `data/signal-trader-mainnet.json`). API: `GET /api/signal-trader`;
`POST /api/signal-trader` with `{"enabled"}`, `{"window_s"}`, `{"max_open"}`, `{"risk_usd"}`, `{"daily_loss_usd"}`,
`{"leverage"}`, `{"size_usd"}` (0 or null = by risk), `{"auto_close"}` or `{"take": "SOLUSDT", "tf": 3600}` (the Trade
button), which needs the control token. `GET /api/brackets`: every coin's maintenance-margin tiers, for liquidation
prices.

The terminal is otherwise a view: it can't pause, flatten or place orders. It receives the dashboard's control token
only for these auto-trader settings, and a test checks the token is sent nowhere else.

Nothing here has been backtested: it is an experiment with test money.

## Terminal (`/terminal`) and Heatmap (`/heatmap`)

Both pages are read-only views of the market (the terminal's only controls are the signal trader's settings and its
**▶ Trade it** button: test money on the testnet bot, real money on the mainnet bot with `--signal-button`).
Everything they show comes from public Binance data, relayed by the bot, plus your own futures account.

**Terminal: live data**
* **Live price, trades and forming candle** (`arbbot/livefeed.py`): the bot keeps one Binance stream connection per coin
  the terminal shows (trades, candles of every interval, the 24h ticker, the mark price) and pushes the updates to the
  page over `/ws/live` up to 10 times a second. The chart shows **● LIVE · lag 0.25 s** (how late Binance's newest
  trade is when it leaves the bot; measured about 0.2-0.3 s); while it is live, price, candles and trades are polled
  only every 30 s as a backstop, and without it the page polls every 5 s (**○ polling**). The stream is on the bot's own
  exchange: mainnet on the mainnet bot, testnet on the testnet bot. No REST weight.
* **The browser tab** shows the coin's live price: `64,529.90 ▲ | BTCUSDT -0.58% · Arb Terminal`.
* The rest refreshes on timers: order book and tape 2 s (live trades go to the tape too), signals 15 s (each coin
  re-read every 60 s), the BOOK overlay 5 s.

**Terminal: chart overlays** (buttons above the chart: VWAP, EMA, CVD, LVL, WHL, EMA50, RSI, **BOOK**, SIG)
* **BOOK** (`arbbot/orderbook.py`): which way the order book pushes, per timeframe. A strip at the bottom of the price
  pane reads `5m ▲ UP +42 · 15m ● even −8 · 1h ▼ DOWN −31 · 4h …` (−100 sellers in control … +100 buyers; ±25 or more is a
  call); hover a timeframe for its reasons. The score is 55% the book's **imbalance** (bids vs asks near the price,
  averaged over the timeframe), 45% the **taker delta** of the timeframe's last 2 candles, plus **absorption** (heavy
  selling while the price holds, or the reverse) and **walls** within 0.5%. A **BOOK** pane under CVD shows the
  imbalance per candle, and the biggest walls are drawn as dashed lines on the price. The book is Binance **mainnet**
  (500 levels, every 10 s, only for the coin shown, kept 24 h; on BTC 500 levels reach only about ±0.1%). Pressure, not
  a forecast: walls can be pulled; not backtested.

**Terminal: Signals panel**
* **Signal types** now include **Candle reversal** (see below) and every pattern is shown on a **Candles** line in the
  Why section (hover a pattern for what it means; rows with a pattern get a 🕯).
* **▶ Trade it LONG / SHORT** (in the Why header of a coin with a LONG or SHORT signal; testnet bot, or mainnet bot
  with `--signal-button`): opens that signal yourself with the auto-trader's entry, stop, TP1 (half closes), TP2 and
  exits. The confirm box (headed **⚠ REAL MONEY ⚠** on mainnet) shows the position size, leverage, margin against the
  USDT available, the loss at the stop including fees, and the **liquidation price** and how far the stop is before
  it. It warns when the quality check says NO TRADE or the confidence is under the minimum. It keeps every limit (kill
  switch, daily loss, most open, coins held elsewhere) and is refused, before anything is sent, when the margin is
  short, the leverage is over the coin's limit or Binance would liquidate it before its stop. Your trades are marked
  "· you".
* **Position size, leverage and Auto close** (the strip at the top of the panel): your own $ size per trade (blank =
  sized by the risk), 1-20x, and whether the bot may close a trade at market on a signal flip or after the maximum
  hold (OFF: only the stop, TP1, TP2 or you). See the settings table in
  [Signal auto-trader](#signal-auto-trader-testnet-only).
* **Liq column** (after R:R, when the bot runs the signal trader): each signal's liquidation price with your size and
  leverage, isolated margin, from Binance's maintenance-margin tiers per coin (`/fapi/v1/leverageBracket`, read hourly;
  a cautious 1% until read), e.g. `90.9091 ⚠ −9.1% · 10x`. **Red ⚠** = liquidated before the stop. Hover: margin,
  maintenance rate, room between stop and liquidation, fees and the loss at the stop with fees. An estimate at the
  entry: funding paid over time moves the real one a little.
* **Account balances** (Positions & P&L, and the Account positions block under the signals): futures wallet,
  available, open P&L and equity, plus the **spot** wallet (every coin valued in USDT, read every minute, read-only)
  and the total of both. The coins line lists each spot coin with its $ value and any non-USDT coin in the futures
  wallet (`F·BNB`). The testnet bot shows futures only (testnet keys do not open the spot testnet); the paper bot has
  no spot wallet.
* **TP1 reachability:** a signal is NO TRADE when TP1 is further than the price can usually travel before the time exit
  (1.25 × LTF ATR × √(LTF candles in the hold)).

**Candlestick patterns** (`arbbot/quality.candle_pattern`, on the signal's trigger candles, in their gap-free crypto
forms, sized in ATRs, on the last 2 closed candles and dropped once the price closes through them):

| Pattern (long / short) | Kind | What it shows |
|---|---|---|
| Bullish / bearish **engulfing** | reversal | a candle against, then a strictly bigger one its way that covers it |
| **Hammer / shooting star** (pin bar) | reversal | a wick ≥ 2× the body toward the low / high, rejected |
| **Morning / evening star** | reversal | a big candle against, a small one, then one its way past the first one's middle |
| **Three white soldiers / black crows** | continuation | three solid candles its way, each closing further |
| **Inside-bar breakout / breakdown** | continuation | a candle inside the one before, then a close past that mother candle |

A reversal pattern only counts **at a support / resistance** (its extreme within 0.5 ATR of the 20-candle low / high or
a recent swing, or up to 1.5 ATR through it). The built-in **Candle reversal** type (after Trend pullback, before Mean
reversion) takes a reversal candle at a level, needs at least average volume and no pattern the other way, and puts the
stop past the pattern. Custom types get four conditions: *candle*, *candle at level*, *which pattern*, *candle against*.
Patterns add no quality points. Not backtested.

**Heatmap (`/heatmap`, `arbbot/heatmap.py`)**

The order book over time, like heatcharting.com: every price band coloured by the dollars resting there (dark → blue →
cyan → green → yellow → orange → white), with the candles, the EMA 50, volume, liquidations and the book now on top.

* **Data:** the bot keeps a live local copy of the Binance **mainnet** futures book from its diff-depth stream (one
  1000-level snapshot when it connects, Binance's sync rules) and every **5 s** records the dollars per band (about
  0.015% of the price: **10** on BTC, **0.5** on gold, ±2.5% around it), kept **24 h** in memory (about 12 MB a coin a
  day), for up to **3 coins** each opened in the last 6 h. **The heat starts when a coin is first opened and a restart
  clears it**: exchanges do not provide order-book history.
* **Detail follows the screen:** the page asks for exactly the window it shows at about one column a pixel (5 s on 1m,
  30 s on 5m, 2 min on 15m, 5 min on 1h …), so every timeframe looks equally fine; then only the newest columns every 3 s.
* **Live:** the candle and price come over `/ws/live?src=book`; the status line shows **● LIVE · lag 0.27 s · book 5 s
  old**.
* **Mouse:** wheel zooms time at the cursor, drag pans time and price, drag or wheel the price scale stretches it, drag
  the time scale zooms, double-click resets (on the price scale: price auto-fit back). The crosshair labels both scales;
  the legend shows the candle, the EMA 50, the BID / ASK and spread, and the dollars resting under the cursor.
* **⚙ Liquidity heatmap options:** show heatmap background, price step (a multiple of the recorded band), contrast,
  volume bubbles (big trades; 0 = off), liquidity profile (the book now as thin lines at the right edge, red asks above,
  teal bids below), liquidations (diamonds, the big ones named), strength level (a −100…+100 pane: bids vs asks within
  ±0.5% over time) and walls ≥ × median (bands that big glow white-hot).
* **Search:** the box suggests every mainnet USDT perpetual (busiest first; stocks and metals too) as you type.
* Read-only; Binance only (heatcharting.com merges three venues).

**Account reads (testnet and mainnet order tickets):** the bot follows its futures account over Binance's user-data
WebSocket (a listen key): every fill, order and position change is pushed and triggers one read; otherwise it reads
every 15 s instead of every 4 s (about 80% fewer requests; it falls back to 4 s while the stream is down). The log's
request-weight line says `stream up (N pushes)`. Your home IP is shared with other customers of your provider, so the
"weight used" Binance reports can be above what the bot itself sends.

**API** (all read-only): `GET /api/market/book` (BOOK overlay), `GET /api/market/heatmap` (`symbol`, `interval`, `t0`,
`t1`, `res`, `plo`, `phi`, `after`), `GET /api/market/heatmap-symbols`, WebSocket `/ws/live` (`?src=book` for the
heatmap's exchange; send `{"sym": "BTCUSDT", "iv": "5m"}`).

## Trading bot: Freqtrade (`/trading`, `freqkit/`)

[Freqtrade](https://github.com/freqtrade/freqtrade) runs as a **separate process** (a dedicated venv or the official
Docker image) and is managed from the dashboard's `/trading` page through Freqtrade's REST API: exchange and pair
setup, strategy choice and an in-page strategy editor (create, edit, duplicate, delete, validated by Freqtrade),
**dry-run by default**, live only after an explicit typed confirmation plus
`FREQTRADE_LIVE_ACK` in `.env`, start / pause / stop, open trades and history, profit & loss, wallet, performance,
backtesting with data download, logs and events. Install it once:

```bash
python3 -m venv .venv-freqtrade && .venv-freqtrade/bin/pip install freqtrade
```

The paper bot (`config.paper.toml`) has `[freqtrade] enabled = true`; the other configs keep it off because only one
dashboard may manage the processes. Everything else, including the safety rules and the API list, is in
[docs/FREQTRADE.md](docs/FREQTRADE.md). Tests: `.venv/bin/python -m unittest tests.test_freqkit`.

## Hosted read-only dashboard (Vercel)

The bot itself can't run on Vercel (it needs an always-on process, local storage, and Binance blocks many cloud
regions), so Vercel only hosts a **read-only view**: the bot pushes a sanitised snapshot every 10 s to Upstash Redis
and a password-protected Vercel app reads it. No endpoint on your computer is exposed, the page has no controls, and
the app serves nothing until a password is configured. Setup and architecture: `vercel-dashboard/README.md`.
Deployed at <https://arbbot-dashboard.vercel.app> (needs the setup steps before it shows data).

## Running 24/7 (macOS launchd)

`deploy/com.arbbot.plist` starts the bot at login and restarts it if it crashes (non-zero exit); a clean stop
is not restarted. Open positions and the daily-loss total survive restarts (SQLite).

```bash
mkdir -p ~/Library/LaunchAgents && cp deploy/com.arbbot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.arbbot.plist   # start + enable
launchctl bootout gui/$(id -u)/com.arbbot                                   # stop + disable
tail -f logs/bot.log
```

Caveats: macOS may block a launch agent from reading `~/Documents` until the Python binary is granted
access under *System Settings → Privacy & Security → Files and Folders* (or move the project outside
`~/Documents`); the Mac must be awake and online (use `caffeinate` or Energy settings); and if your DNS
blocks Binance the bot stays safe but blind (feed down, no entries) until it recovers.

## Linux

The bot is plain Python (3.10 or newer; this project uses 3.14) with one dependency (`aiohttp`), so it runs the same
on Linux. Setup:

```bash
git clone https://github.com/sks-666/binance-arb-bot.git ~/binance-arb-bot && cd ~/binance-arb-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then put your own keys in .env (never share it)
.venv/bin/python run.py --check --testnet --config config.testnet.toml   # read-only key check
```

| macOS | Linux |
|---|---|
| double-click `Start Binance Bots.command` | `./start-bots.sh` (the same script: same questions, same `TRADE REAL MONEY` confirmation for mainnet) |
| double-click `Stop Binance Bots.command` | `./stop-bots.sh` |
| double-click `Run Backtests.command` | `./run-backtests.sh` |
| the dashboard opens in your browser | it opens with `xdg-open` on a desktop; on a server with no screen the address is printed instead |
| `deploy/com.arbbot.plist` (launchd) | `deploy/arbbot.service` / `deploy/arbbot-testnet.service` (systemd, below) |

The phone link finds the computer's home-network address with `ip` (or `ifconfig` where `ip` is missing), leaving
out VPN tunnels (tun, wg, NordLynx), Docker and virtual bridges. If the computer runs a firewall (`ufw`), allow the
dashboard ports from your home network only, for example `sudo ufw allow from 192.168.0.0/16 to any port 8765:8770 proto tcp`.

### Running 24/7 (Linux systemd)

Two user services, both restarted if they crash. There is deliberately **no service for the mainnet bot**: real
money only starts by hand with `./start-bots.sh`. Edit the paths inside if the project is not in `~/binance-arb-bot`.

| Service | What it runs |
|---|---|
| `deploy/arbbot.service` | the PAPER bot (live prices, simulated fills, never an order), like the macOS launchd agent |
| `deploy/arbbot-testnet.service` | the TESTNET bot as `start-bots.sh` starts it (swing kit on the demo account, port 8767, phone link) |

```bash
mkdir -p ~/.config/systemd/user && cp deploy/arbbot-testnet.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now arbbot-testnet      # start now and at every login
loginctl enable-linger "$USER"                    # keep it running when you are logged out / after a reboot
systemctl --user status arbbot-testnet            # is it running?
tail -f logs/testnet-bot.out
systemctl --user disable --now arbbot-testnet     # stop + disable
```

Use either the service or `start-bots.sh` / `stop-bots.sh` for the testnet bot, not both at once: the stop script
stops it, but a service restarts only after a crash, so a stopped service stays stopped until
`systemctl --user start arbbot-testnet`. If the mainnet bot runs too, it answers the Telegram commands, so add
`--no-commands` to the testnet service's ExecStart (one bot per Telegram token).

`deploy/arbbot-testnet.service` does **not** start the [signal auto-trader](#signal-auto-trader-testnet-only), unlike
`start-bots.sh`. To add it, append these flags to the service's `ExecStart`, then `systemctl --user daemon-reload` and
restart:

```
--signal-trader --signal-trader-window 1h --signal-trader-risk-usd 20 --signal-trader-max-usd 2000 --signal-trader-max-open 3
```

For a system-wide service, a drop-in `/etc/systemd/system/arbbot-testnet.service.d/override.conf` does the same
without touching the unit. It needs an empty `ExecStart=` line, then the full command
(see [docs/SIGNAL-AUTO-TRADER.md](docs/SIGNAL-AUTO-TRADER.md#3-start-stop-restart)).

## Running without your computer (Render)

`python run.py --hosted` is the container-host mode: **paper only** (`--hosted --live` refuses to start), and the
recorder stays off unless `ARBBOT_RECORD=1`. Instead of the local dashboard (which has pause/flatten controls and is
never exposed) it serves, on `0.0.0.0:$PORT`, the same **read-only** page as the Vercel copy straight from the running
bot at `/` and `/api/snapshot`, behind HTTP Basic auth (user `viewer` or `DASHBOARD_USER`, password
`DASHBOARD_PASSWORD`, at least 12 characters). It fails closed: without a password only `GET /healthz` answers. Failed
logins are delayed and capped at 30 a minute. Entries can still be paused from Telegram (`/pause`, `/resume`). The
`Dockerfile` and `render.yaml` package it.

1. Put the project in a **private** GitHub repository. `.env`, `data/` and `logs/` are in `.gitignore`; check
   `git status` shows no `.env` before pushing. Never make this repo public.
2. In Render: New -> Blueprint -> pick the repo. `render.yaml` sets the **Frankfurt** region (Binance refuses US
   addresses; the region can't be changed later) and asks you for five secrets: `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `DASHBOARD_PUSH_URL`, `DASHBOARD_PUSH_TOKEN` (the Upstash **read-write** token; only needed for
   the Vercel copy) and `DASHBOARD_PASSWORD` (the login for the dashboard on Render itself).
3. **Stop the bot on your computer first.** Two instances would both publish to the same page and both poll Telegram.
4. Watch the Render logs for `hosted mode: health check on 0.0.0.0:...` and `hosted dashboard: publishing ...`.

Free vs paid: a free web service sleeps after 15 minutes without inbound requests (point an uptime pinger such as
UptimeRobot at `/healthz` every 5 minutes) and its disk is wiped on every restart, so the paper book and daily-loss
total reset. For history that survives, switch to a paid instance and uncomment the `disk` block in `render.yaml`
(mount `/app/data`).

## Verification status

| Area | Status |
|---|---|
| Scanner, risk, paper executor, persistence, engine cycle, kill switch, new core formula, recorder, replay | Unit-tested (186 tests, including regressions for the real losing trades) |
| Funding intervals/caps, order-book depth parsing, VWAP slippage | **Checked against real Binance** (public endpoints) |
| Partial-fill rebalancing, exit top-ups, fee-tier lookup | Tested against a fake exchange with programmable partial fills; **not run on Binance** |
| WebSocket feed, REST fallback, chunked connections, Telegram, reconciliation | Tested against local mocks that mimic Binance/Telegram message formats |
| REST universe load, WebSocket streams, REST→WS handoff, dashboard, pause/resume | **Run against real Binance** (2h+ paper session, 0 stream errors until DNS dropped; clean degradation afterwards) |
| Strategy on real data | Found and fixed: broken-price markets (halted spot / mark≠index) and entries below spot (see tests) |
| `live.py` orders, preflight balance endpoints, reconciliation endpoints | **Not run against Binance.** Run `--live --testnet` first |
| Telegram delivery to your phone | **Needs your bot token**; use `--test-alert` |

## What real paper trading has taught us

The first real-data paper session (8 trades, −$4.16, one win) exposed bugs that no unit test had caught; each now
has a regression test. Reviewing a trade means reading the recorded quotes around it (`data/recordings`), not just
the summary line.

| Symptom | Cause | Fix |
|---|---|---|
| ERA/SAND "signals" at −138/−75 bps basis | Halted/broken spot made mark ≠ index; rolling mean was itself broken | mark/index gate, entries need perp ≥ spot |
| ZEC: signal +32 bps, filled at −11 bps, lost 33.6 bps | Order-book fetch timed out (4 s); the bot then filled on later quotes but recorded the old signal | re-check on fresh quotes, 1.5 s timeout, real entry basis |
| "held 5 s" after 1 s | open time stamped at tick start, not at fill | stamped at fill |
| AVA: +69 bps predicted, −111 realised | ~$50-110 touch liquidity; spot dumping, perp lagging | $300 minimum touch depth, larger risk buffer |

The saved trade history persists across restarts on purpose (so a restart can't reset the daily loss limit). To start
a fresh paper book, stop the bot and move `data/paper-mainnet-real.db` aside (don't delete it); the next start creates
an empty one.

## Going live (read before using real money)

**`live.py` has not been run against the exchange.** The unit tests cover quantity alignment and the engine
logic, but not Binance's responses. Do these in order:

0. On startup the bot **reconciles** saved positions with Binance (short perp present, spot inventory present,
   no unknown perp positions in tracked markets) and trips a fault if anything disagrees. Use a dedicated
   account or sub-account so unrelated positions are not mistaken for orphans.
1. Create **testnet** keys: the spot and futures testnets are separate exchanges, so make a futures key on
   testnet.binancefuture.com and a spot key on testnet.binance.vision, and put them in `.env` as
   `BINANCE_TESTNET_FUTURES_API_KEY/_SECRET` and `BINANCE_TESTNET_SPOT_API_KEY/_SECRET` (see `.env.example`). Then run
   **`run.py --check --testnet`**: a read-only check of the clock, account mode (One-way), balances and fee rates on
   both exchanges that names the exact venue and key variable that fails. It sends no orders and never starts the bot
   (`run.py --check` does the same on mainnet with `BINANCE_API_KEY`). Only when it passes, run `run.py --live --testnet`;
   if the live preflight is refused it now stops with the same plain explanation instead of a traceback.
2. Fund both wallets: the **spot** wallet needs USDT for the long leg and the **USDⓈ-M futures** wallet
   needs USDT for margin (isolated, `leverage = 2`). The futures account must be in **One-way mode**.
3. Watch several full entry/exit cycles; compare fills and fees with the dashboard.
4. For mainnet use a dedicated API key with **withdrawals disabled** and IP restriction, set
   `ARBBOT_LIVE_ACK=I_UNDERSTAND_THIS_TRADES_REAL_MONEY`, and start with `trade_notional_usd` at the
   exchange minimum.

**Live execution details.** Partial fills are accepted, not treated as errors: after an entry the legs are compared
in delta terms (`spot_qty` vs `perp_qty × multiplier`) and any excess beyond 1% is unwound; exits top up partial
fills (up to 3 orders per leg) and fault if a non-dust remainder is left. Contract multipliers (`1000SHIB` etc.)
are handled on both price and quantity. The account's real taker fees are read at startup. None of this has run
against the exchange yet (see the verification table), so testnet first.

**Not possible yet: backtested slippage.** That needs historical order-book data the bot doesn't have. Instead the
bot measures its own model error live (predicted vs realised P&L) and widens the risk buffer accordingly, and
paper trades fill at the real book VWAP. A recorder plus replay tool would be the next step for true backtests.

Known limits: the WebSocket feed is tested against a local mock of Binance's combined-stream format, not
Binance itself; futures fees and funding are estimated (not queried); spot fees in BNB are estimated; orders
are still REST (no WebSocket order API).

*Not financial advice. Trading carries risk of loss.*
