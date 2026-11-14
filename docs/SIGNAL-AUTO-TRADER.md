# Signal auto-trader (testnet)

The terminal's **Signals panel** turns five market readings into one trade idea per coin: LONG, SHORT or WAIT, with an
entry, a stop and a target. The **signal auto-trader** takes the best of those ideas and trades them on the
**Binance futures TESTNET**, with test money only. It cannot run on mainnet: it refuses any account that isn't the
testnet.

```
mainnet market data ──▶ arbbot/signals.py  (5 readings → LONG / SHORT / WAIT + entry, stop, target)
 (whales, crowd,              │
  curve, macro, news)         ▼
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
4. **The trade plan** (LONG or SHORT only):
   - **Entry** is the price now, or a pullback to the whales' price at most one risk away ("at the price" in the
     panel means now).
   - **Risk** is the larger of the whales' level, the coin's ATR on candles that fit the timeframe, and 0.3% of the
     price, capped at 5%.
   - **Stop** is one risk from the entry.
   - **Target** is the whales' other level if that's at least 1.5 risks away, otherwise 2 risks. **R:R** is
     target distance ÷ stop distance.

### The panel's timeframe vs the auto-trader's

The **1m … 1w buttons at the top of the Signals panel only change what the table shows.** The auto-trader trades
**one** timeframe, its own:

- **"Its timeframe = panel"**: the table is exactly what it trades.
- **"Its timeframe 1h [Trade 30m]"**: you're looking at 30m and it trades 1h. Press the orange button to switch it.

---

## 2. What the auto-trader does

Every 30 seconds it reads the signal on its timeframe, then works through these steps.

**Enters** a coin when all of these hold, most confident coin first:
- The side is LONG or SHORT, with confidence **≥ 60**.
- R:R is **≥ 1.5**.
- At least **60%** of the weight had data, and the whale reading is present.
- It holds fewer trades than its **most open** limit, which counts positions plus waiting limit entries.
- Today's realised loss is under the **daily loss limit** (UTC day).
- The coin isn't in a cooldown after an exit, and no other part of the bot holds it (arbitrage, swing mirror, the
  order ticket).
- The testnet price is within 2% of mainnet. Otherwise the testnet book isn't the real market, so it skips the coin.

**Sizes** the trade so hitting the stop loses about **$20** (`risk_usd`), and at most **$2,000** of position.

**Places the order:**
- A **market** order when the entry is the price now. It's refused if it would slip more than 0.3%.
- Otherwise a **limit** order at the entry, cancelled if it hasn't filled within half the timeframe (5 min to
  4 h).
- The **stop and the target ride on Binance** as stop-market and take-profit-market orders, so they work even if
  the bot stops.

**Exits** when any of these happens:
- The stop or the target fills on Binance.
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

**Dashboard** (`http://127.0.0.1:8767/`), **Signal auto-trader · testnet** card:
- **Timeframe buttons** (1m … 1w).
- **Most open** list.
- **Risk $** and **Day limit $** boxes.
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
