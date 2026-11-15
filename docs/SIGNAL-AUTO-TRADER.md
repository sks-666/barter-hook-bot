# Signal auto-trader (testnet)

The terminal's **Signals panel** turns five market readings into one trade idea per coin: LONG, SHORT or WAIT, with an
entry, a stop and a target. The **signal auto-trader** takes the best of those ideas and trades them on the
**Binance futures TESTNET**, with test money only. It cannot run on mainnet: it refuses any account that isn't the
testnet.

```
mainnet market data ──▶ arbbot/signals.py  (5 readings → LONG / SHORT / WAIT)
 (whales, crowd,              │
  curve, macro, news,         ▼
  candles, funding,  arbbot/quality.py  (regime, 4 pillars, guardrails, stop / TP1 / TP2, pre-mortem → TRADE / NO TRADE)
  calendar)                   │
                              ▼
                     arbbot/signal_trader.py  (filters, sizing, limits; every 30 s)
                              │
                              ▼
                     testnet order ticket  ──▶  Binance futures TESTNET  (stop and target sit on the exchange)
```

Nothing here has been backtested. It's an experiment with test money, not advice.

---

## 1. How the signal works

### Five readings, each from −1 (down) to +1 (up)

For each coin, over the timeframe you pick (1m to 1w), the bot reads:

| Reading | What it looks at | Weight |
|---|---|---|
| **Whales** | Big buy orders vs big sell orders, and liquidations | 35% |
| **Crowd** | Top traders' long share vs all accounts', its change, taker buy/sell, open interest | 25% |
| **Curve** | The perpetual's premium over spot (basis) and its trend, and funding (a crowded side) | 15% |
| **News** | Headlines about the coin or crypto, scored by bullish and bearish words | 5% under 15m, 15% up to 1d, 10% above |
| **Macro** | S&P 500, VIX, US dollar index, 10-year yield | 0% under 1h, 5% under 4h, 10% under 1d, 20% from 1d |

A reading with no data (e.g. fewer than 3 whale orders) is left out, and the others are re-weighted.

### From readings to a side

1. **Score** is the weighted average of the readings, from −1 to +1. The panel shows it ×100 (e.g. −33).
2. **Side:**
   - **LONG** when the score is +0.15 or higher.
   - **SHORT** when it's −0.15 or lower.
   - **WAIT** ("no clear side") in between.
3. **Confidence** (0–100) is higher when:
   - the score is strong,
   - the readings agree with each other (the **AGREE** column), and
   - most of the weight had data to read (the **DATA** column).
4. **Entry** is the price now, or a pullback to the whales' price at most one ATR away ("at the price" in the
   panel means now).

The side only says which way the flow leans. Whether to trade it is the quality check's job.

### The quality check: TRADE or NO TRADE

`arbbot/quality.py` checks every LONG or SHORT against the chart, the derivatives market and the clock. A marginal
or choppy setup is **NO TRADE**. The auto-trader only takes **TRADE** rows.

It reads three candle sets picked from the timeframe:

| Timeframe | LTF (trigger) | MID | HTF (context) |
|---|---|---|---|
| 5m | 1m | 15m | 1h |
| 15m | 1m | 15m | 1h |
| 1h | 15m | 1h | 4h |
| 4h | 1h | 4h | 1d |
| 1d | 4h | 1d | 1w |

**Step 1: the regime (HTF candles)**

| Regime | When |
|---|---|
| `TRENDING_BULL` / `TRENDING_BEAR` | ADX ≥ 25, EMA 21 above / below EMA 55, price on that side |
| `VOLATILE_CHOP` | Candle ranges ≥ 1.4× their 100-candle average with no trend, or a strong ADX with the EMAs mixed. **Never traded.** |
| `RANGING_MEAN_REVERSION` | ADX < 25 |

The **setup** is a *breakout* (the last closed LTF candle closed past the 20-candle high/low), a *trend* trade (with
the HTF trend), *mean reversion* (against it) or a *range* trade.

**Step 2: four pillars, 0–25 points each (the quality score, 0–100)**

| Pillar | Points for |
|---|---|
| Structure & liquidity | a break of structure (BOS) its way (10), a liquidity sweep of a swing (8), a fair value gap retest (7) |
| Momentum & divergence | RSI in the right zone (8), MACD histogram its way / building (4 / 7), EMA 8/21/55 ribbon stacked (6), HTF trend agrees (4), RSI divergence its way (4) |
| Volume & volatility | trigger candle RVOL ≥ 1.5× (9, or 4 at ≥ 1×), ATR contracted before the trigger (6), right side of the 24h VWAP (6), taker buys its way (4) |
| Derivatives & cross-market | open interest rising with the move (7), funding not against it (6), premium trend agrees (5), BTC agrees (7; for BTC itself: S&P / dollar) |

Missing data scores half, so one dead source doesn't veto everything. **A TRADE needs 55 or more.**

**Step 3: the guardrails** (learned from losing trades)

| Guardrail | Rule |
|---|---|
| **[REGIME]** | No mean-reversion or range trade when ADX > 30 on the MID or HTF candles, unless there's a break of structure its way. |
| **[FAKEOUT]** | No breakout when the trigger candle's volume is under 1.5× its 20-candle average. |
| **[FUNDING]** | No long when funding is above 0.03% per 8h **and** spot volume is falling (last 6 MID candles under 85% of the 6 before; futures volume when the coin has no spot market). The mirror for shorts. |
| **[SESSION]** | Confidence × 0.8 at weekends, 21:00–24:00 UTC, or within 15 minutes of a high-impact US release (this week's calendar, from Forex Factory's public feed, read once an hour). |

**Step 4: stop and targets from structure**

- **Stop:** past the nearest swing behind the entry (at least 1 LTF ATR away), plus **0.25 ATR**, and pushed off
  round numbers (e.g. not 97,000 on BTC). A structural stop further than 5% is **NO TRADE**: it is never pulled inside
  the structure.
- **TP1:** the first unbroken swing level at least **1.8R** away (otherwise exactly 1.8R). **Half the position closes
  there**, then the stop moves to breakeven.
- **TP2:** the runner, the first unbroken level from **3R** to 5R (otherwise 3R).
- An unbroken HTF swing level in the way closer than **1.5R** (or under 1.8R, so TP1 can't fit) is **NO TRADE**.

**Step 5: the pre-mortem.** It names the most likely reason this trade would hit its stop, checked in this order:

| Risk | If active |
|---|---|
| It fights the HTF trend with no break of structure | **NO TRADE** |
| BTC trends the other way on the MID candles (alts only) | **NO TRADE** |
| Momentum stretched (RSI > 75 / < 25) or diverging against it | confidence −15 |
| Its side is crowded (funding past 0.03%) | confidence −15 |
| The stop is inside one normal MID candle's range | confidence −15 |

If none is active it names the default risk (a sudden BTC-led move) and how many ATRs away the stop is.

**Confidence** is then 40% the score's own confidence + 60% the quality score, × 0.8 for the session penalty, −15
for an active pre-mortem risk.

On the panel: the **Verdict** column (hover for why), **TP1** / **TP2**, and under *Why* the quality score, regime,
each pillar's points and reasons, every failed guardrail, the pre-mortem and the session.

### The panel's timeframe vs the auto-trader's

The **1m … 1w buttons at the top of the Signals panel only change what the table shows.** The auto-trader trades
**one** timeframe, its own:

- **"Its timeframe = panel"**: the table is exactly what it trades.
- **"Its timeframe 1h [Trade 30m]"**: you're looking at 30m and it trades 1h. Press the orange button to switch it.

---

## 2. What the auto-trader does

Every 30 seconds it reads the signal on its timeframe, then works through these steps.

**Enters** a coin when all of these hold, most confident coin first:
- The quality check says **TRADE**, with confidence **≥ 60**.
- At least **60%** of the weight had data, and the whale reading is present.
- It holds fewer trades than its **most open** limit, which counts positions plus waiting limit entries.
- Today's realised loss is under the **daily loss limit** (UTC day).
- The coin isn't in a cooldown after an exit, and no other part of the bot holds it (arbitrage, swing mirror, the
  order ticket).
- The testnet price is within 2% of mainnet. Otherwise the testnet book isn't the real market, so it skips the coin.
- The testnet order book can take its stop: the side the stop would close into must hold 2× the position within
  half a stop distance past the stop. Otherwise it skips the coin and tries again after the cooldown.

**Sizes** the trade so hitting the stop loses about **$20** (`risk_usd`), and at most **$2,000** of position.

**Places the order:**
- A **market** order when the entry is the price now. It's refused if it would slip more than 0.3%.
- Otherwise a **limit** order at the entry, cancelled if it hasn't filled within half the timeframe (5 min to
  4 h).
- The **stop and TP2 ride on Binance** as stop-market and take-profit-market orders, so they work even if
  the bot stops. They trigger on the **mark price**, so one stray trade on a thin testnet book can't set them off.
- Once the position is open it adds a **reduce-only take-profit for half** of it at **TP1**. When TP1 fills, it
  moves the stop to the entry (**breakeven**) as soon as the mark is past the entry.
- It first sets the coin's leverage to **5x** (the order ticket's own default is 15x). Binance auto-deleverages the
  most profitable × most leveraged positions first, so low leverage keeps its winners at the back of that queue.

**Exits** when any of these happens:
- The stop or TP2 fills on Binance (after TP1, the stop sits at breakeven).
- The signal flips to the other side with confidence ≥ 60.
- It has held the trade for **2× the timeframe it was opened on** (30 min to 7 days).

Then it waits a **cooldown** of half the timeframe (at least 5 min) before trading that coin again.

Mainnet prices are moved onto testnet prices: entry = testnet price × (signal entry ÷ mainnet price), and likewise for
the stop and target.

### Limits that always apply

| Limit | Default | Where it's set |
|---|---|---|
| Timeframe | 1h | **Page** (terminal or dashboard), or `--signal-trader-window` |
| Most open trades | 3 | **Page** (1–20), or `--signal-trader-max-open` |
| Loss at the stop per trade | $20 | **Page** or `/auto risk 30` (1–500), or `--signal-trader-risk-usd` |
| Largest position | $2,000 | `--signal-trader-max-usd` |
| Leverage it sets on a coin before entering | 5x | **Page** or `/auto lev 3` (1–20), or `--signal-trader-leverage` |
| Daily realised loss, then no new entries until the next UTC day | $100 | **Page** or `/auto daily 150` (5–5000), or `--signal-trader-daily-loss-usd` |
| Minimum confidence | 60 | `--signal-trader-min-conf` |
| Kill switch | — | `touch KILL` in the bot's folder: the ticket refuses every new order |

**A value picked on the page is saved** in `data/signal-trader-testnet.json`. It survives restarts and **wins over**
the command-line flag. To go back to the flag's value, pick that value on the page again.

---

## 3. Start, stop, restart

On this machine the testnet bot runs as the systemd service **`arbbot-testnet`**, from `/srv/apps/binance-arb-bot`.
The auto-trader is switched on by a drop-in file,
`/etc/systemd/system/arbbot-testnet.service.d/override.conf`, which adds `--signal-trader` and its settings to the
service's command.

```bash
sudo systemctl start arbbot-testnet      # start
```

```bash
sudo systemctl stop arbbot-testnet       # stop (open testnet positions stay open, with their stop and target on Binance)
```

```bash
sudo systemctl restart arbbot-testnet    # restart (needed after a code deploy)
```

```bash
systemctl status arbbot-testnet          # is it running?
```

```bash
sudo systemctl enable arbbot-testnet     # also start it at every boot (it's disabled now)
```

**Check the auto-trader started.** Look for `signal auto-trader: ON on the futures TESTNET (...)`:

```bash
journalctl -u arbbot-testnet --since "-5min" | grep -iE "signal auto-trader|reconcil|418"
```

Without `systemd` (e.g. on another machine), `./start-bots.sh` in the project folder starts the testnet bot with the
auto-trader on.

---

## 4. Change settings

### On the page (no restart)

**Terminal** (`http://127.0.0.1:8767/terminal`), Signals panel, auto-trader strip:
- **Its timeframe**: pick a timeframe at the top of the panel, then press the orange **Trade …** button and confirm.
- **Its open trades / most**: pick a number from the list (1–20) and confirm.
- **Risk per trade · day limit**: type the $ lost at the stop per new trade (1–500) and the day's loss limit
  (5–5000), press Enter and confirm.
- **Leverage · new trades**: pick 1x–20x from the list and confirm.

The **Lev** column in **Account positions** and **Auto-trader closed trades** shows each trade's leverage ("—" for
trades from before it was recorded).

**Dashboard** (`http://127.0.0.1:8767/`), **Signal auto-trader · testnet** card:
- **Timeframe buttons** (1m … 1w).
- **Most open** list.
- **Risk $** and **Day limit $** boxes.
- **Leverage** list.
- **Entries ON/OFF** switch.

Each change applies from the next pass (≤ 30 s), is saved, and is announced on Telegram.

- **Lowering "most open" closes nothing.** It just takes no new trade until fewer are open.
- **Changing the timeframe** affects new trades only. Open trades keep the maximum hold of the timeframe they were
  opened on.
- **Entries OFF** stops new trades. Open ones are still managed: stop, target, signal flip, maximum hold.

### From Telegram (no restart)

The testnet bot answers these in your Telegram chat (only your configured chat):

| Send | What it does |
|---|---|
| `/auto` | Status: entries on/off, timeframe, open / most open, why it isn't entering (if it isn't), today and total result, each open trade |
| `/auto on` · `/auto off` | New entries on or off |
| `/auto 30m` | Trade the 30m signal (1m 5m 15m 30m 1h 4h 1d 1w) |
| `/auto max 5` | Hold up to 5 trades at once (1-20) |
| `/auto risk 30` | New trades lose about $30 if they hit their stop (1-500) |
| `/auto daily 150` | No new trade after $150 of realised loss in a UTC day (5-5000) |
| `/auto lev 3` · `/auto 3x` | New trades at 3x leverage (1-20); open trades keep theirs |
| `/auto trades` | Open trades, waiting entries, the last 10 closed with result and reason |
| `/ideas` · `/ideas 4h` | The combined signal's coins: side, score, confidence, R:R, entry / stop / target; ✅ marks the ones the auto-trader may take |
| `/account` | The testnet futures account: balance, equity, every position and who opened it |
| `/status` | The bot's health, now with a one-line auto-trader summary |

These are the same settings as on the page, saved in the same place.

### On the command line (restart needed)

For the settings without a page control (largest position, minimum confidence), edit the
drop-in file and restart:

```bash
sudo nano /etc/systemd/system/arbbot-testnet.service.d/override.conf
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart arbbot-testnet
```

The file holds an empty `ExecStart=` line, which clears the service's command, then the full command with these flags:

```ini
[Service]
ExecStart=
ExecStart=/srv/apps/binance-arb-bot/.venv/bin/python run.py --live --testnet --config config.testnet.toml --min-samples 20 --swing --swing-mirror trend,swing,smc --swing-mirror-copy-open --lan --signal-trader --signal-trader-window 1h --signal-trader-risk-usd 20 --signal-trader-max-usd 2000 --signal-trader-max-open 3
```

Flags you can add or change: `--signal-trader-risk-usd`, `--signal-trader-max-usd`, `--signal-trader-daily-loss-usd`,
`--signal-trader-min-conf`. Remember that the timeframe, most-open, risk and daily-loss flags are overridden by values picked on the page or in Telegram.

**To turn the auto-trader off for good**, delete the drop-in file, then reload and restart:

```bash
sudo rm /etc/systemd/system/arbbot-testnet.service.d/override.conf
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart arbbot-testnet
```

---

## 5. Open the pages

| Page | On this machine | From a phone or laptop on the same Wi-Fi |
|---|---|---|
| Dashboard | `http://127.0.0.1:8767/` | `http://<this machine's IP>:8767/?key=<key>` |
| Terminal | `http://127.0.0.1:8767/terminal` | same link, then open Terminal |

The machine's address can change (it was 192.168.100.139 on 2026-11-14). Print the current link with its key:

```bash
echo "http://$(hostname -I | cut -d' ' -f1):8767/?key=$(cat /srv/apps/binance-arb-bot/data/lan-key-8767.txt)"
```

Keep that link private: anyone with it can use the dashboard. After a deploy, **reload the page**, because an open tab
keeps the old version.

---

## 6. Deploy a code change

Code is edited in `~/Desktop/binance-arb-bot-main/binance-arb-bot`. To ship it:

```bash
cd /srv/apps/binance-arb-bot && git pull --ff-only ~/Desktop/binance-arb-bot-main/binance-arb-bot main
```

```bash
sudo systemctl restart arbbot-testnet
```

`/srv/apps` keeps its own `config.toml` edit. Page-only changes (`web/*.html`) show after a page reload, without a
restart.

---

## 7. When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `HTTP 418 … banned until …` | Binance banned the IP for too many requests. The bot then holds back every request by itself until the time shown. | Wait until that time. **Don't restart repeatedly**: each restart sends a request during the ban. Restart once after it ends. |
| `ENTRIES HALTED: FAULT: startup reconciliation failed` | At startup the bot couldn't read its positions (e.g. during a ban), so it refuses new trades. | Once Binance answers again, restart once. Look for `reconciled: saved positions match the exchange`. |
| `ClientConnectorDNSError … Temporary failure in name resolution` | This machine lost its network or DNS for a moment (Telegram fails at the same time). | Nothing: the streams reconnect by themselves. If it keeps happening, check the Wi-Fi or use the Ethernet port. |
| Status: `3 positions / entries open` | It's at its most-open limit. | Wait for one to close, or raise **most open** on the page. |
| A trade closed far past its stop, `why`: "closed by Binance: auto-deleveraging" | Binance's **auto-deleveraging (ADL)**: when another trader is liquidated and can't be covered, Binance closes the most profitable, most leveraged positions on the other side at that trader's price. On the testnet that price can be absurd (on 2026-11-14 a winning HYPE short was closed at 100.00 while HYPE traded near 67: −$944). | Nothing to do: it is the exchange, not the strategy. Such closes count in the totals but **not** in the daily loss limit, and the 5x leverage makes them rarer. A trade **liquidated** by Binance still counts. |
| Status: `the daily loss limit (100 USDT) is reached` | Realised loss today reached the limit. | New entries come back at the next UTC day (05:45 local, UTC+5:45). |
| Status: `entries are off` | Entries were switched off. | Switch them on, on the dashboard card. |
| No **Trade …** button or **most open** list | The page is an old version, or the page has no control token. | Reload the page. |
| `swing refresh failed … SSLCertVerificationError … fapi.binance.com` | The swing kit can't reach mainnet Binance from this network. | Separate from the auto-trader; it needs a network or VPN that reaches `fapi.binance.com`. |

Logs:

```bash
journalctl -u arbbot-testnet -f                                         # everything, live
```

```bash
journalctl -u arbbot-testnet --since today | grep -i "signal"           # the auto-trader's lines today
```

Its trades and results are in the terminal's Signals panel (**Account positions**, **Auto-trader closed trades**) and
on Telegram (🤖 messages).

---

## Files

| File | What it is |
|---|---|
| `arbbot/signals.py` | The combined signal: readings, weights, side, confidence, entry / stop / target |
| `arbbot/signal_trader.py` | The auto-trader: filters, sizing, orders, exits, limits, saved settings |
| `arbbot/server.py` | `GET/POST /api/signal-trader` (`{"enabled"}`, `{"window_s"}`, `{"max_open"}`; POST needs the control token) |
| `web/terminal.html` | Signals panel, auto-trader strip, **Trade …** button, **most open** list |
| `web/index.html` | Dashboard card: timeframe buttons, **Most open**, Entries ON/OFF |
| `data/signal-trader-testnet.json` | Its state: open trades, waiting entries, cooldowns, closed trades, saved settings |
| `tests/test_signal_trader.py` | Its tests |
