# Freqtrade trading bot (the `/trading` page)

[Freqtrade](https://github.com/freqtrade/freqtrade) is integrated as a **separate service**: it runs as its own
process (the binary of a dedicated venv, or the official Docker image) and the dashboard drives it only through
Freqtrade's documented REST API. The arbitrage bot, the swing kit and the order ticket are untouched; the subsystem
lives in `freqkit/` and `web/freqtrade.html`.

```
web/freqtrade.html  (/trading)  ──X-Token──▶  arbbot Dashboard  /api/freqtrade/*   (arbbot/server.py)
                                                   │
                                                   ▼  freqkit/ (routes.py → manager.py → client.py, store.py)
                                   ┌───────────────┴────────────────┐
                                   ▼                                ▼
                     freqtrade trade  (127.0.0.1:8080)    freqtrade webserver (127.0.0.1:8081)
                     the bot: dry-run or live             strategies · data download · backtests · candles
                                   └────────── supported exchanges (ccxt) ──────────┘
```

Freqtrade version used while building this: **2026.10** (pip and `freqtradeorg/freqtrade:stable`).

## 1. Install

**Option A, local venv (what the shipped config uses).** Freqtrade needs its own venv: it pulls pandas, ccxt,
TA-Lib and more, none of which the bot itself needs.

```bash
python3 -m venv .venv-freqtrade
.venv-freqtrade/bin/pip install --upgrade pip wheel
.venv-freqtrade/bin/pip install freqtrade
.venv-freqtrade/bin/freqtrade --version
```

(Python 3.11 to 3.13. On Linux x86_64 and macOS the TA-Lib wheels are prebuilt; if `pip` tries to compile TA-Lib,
follow https://www.freqtrade.io/en/stable/installation/.)

**Option B, Docker (official image).** `docker pull freqtradeorg/freqtrade:stable`, then set
`runner = "docker"` in the `[freqtrade]` section. The manager runs `deploy/docker-compose.freqtrade.yml`
(services `freqtrade` and `freqtrade-webserver`, APIs published on 127.0.0.1 only, `ft_user_data/` mounted as
`/freqtrade/user_data`). The docker user must be in the `docker` group.

`ft_user_data/` is Freqtrade's `user_data` directory (created with `freqtrade create-userdir`). Put your own
strategies in `ft_user_data/strategies/` (the shipped `SampleStrategy` is Freqtrade's example). The folder's
generated files (`config*.json`, trade databases, logs, candle data, backtest results, pid files, API credentials) are
git-ignored.

## 2. Turn it on

`config.paper.toml` ships with it on; the real-money bot (`config.toml`) and the testnet bot keep it off, because
**only one dashboard may manage the Freqtrade processes** (they share one API port and one `user_data`).

```toml
[freqtrade]
enabled = true
runner = "local"                  # or "docker"
executable = ".venv-freqtrade/bin/freqtrade"
compose_file = "deploy/docker-compose.freqtrade.yml"
user_data = "ft_user_data"
api_host = "127.0.0.1"
api_port = 8080                   # freqtrade trade
webserver_port = 8081             # freqtrade webserver (backtesting)
poll_s = 5.0
autostart_webserver = true
start_timeout_s = 90.0
```

Start the dashboard as usual (`.venv/bin/python run.py --config config.paper.toml ...`) and open
<http://127.0.0.1:8765/trading>. The backtesting webserver starts with the dashboard; the trading bot starts only when
you press **Launch bot**.

Environment (`.env`, all optional):

| Variable | Meaning |
|---|---|
| `FREQTRADE_LIVE_ACK=I_UNDERSTAND_THIS_TRADES_REAL_MONEY` | Required for live trading, together with the typed confirmation on the page. Without it every launch is a dry run. |
| `FREQTRADE_API_USERNAME` / `FREQTRADE_API_PASSWORD` | Login of Freqtrade's REST API. Generated into `ft_user_data/api_credentials.json` (mode 0600) when unset. |

## 3. Using the page

* **Mode & engine**: dry-run (default) or live. *Launch bot* starts the Freqtrade process with the saved settings;
  *Shut down bot* stops the process. *Pause entries* / *Start trading* / *Stop trading* are Freqtrade's own
  `/pause`, `/start`, `/stop` (the process keeps running; a stopped loop manages nothing, a paused one still exits).
  *Reload config* is Freqtrade's `/reload_config`. The processes survive a dashboard restart: the manager adopts them
  from their pid files. `python -m freqkit status|stop` controls them from a shell.
* **Settings** (applied at the next launch): exchange (any ccxt id Freqtrade lists; *Show exchange markets* lists the
  pairs quoted in your stake currency), spot or futures (isolated margin, pairs become `BTC/USDT:USDT`), stake currency
  and amount (`unlimited` spreads the wallet), max open trades, dry-run wallet, strategy (every class in
  `ft_user_data/strategies/`, *View strategy code* shows it), timeframe, static pair list or top-N by volume, blacklist.
* **Exchange API keys**: stored in `data/freqtrade.db` (mode 0600), shown never again, written only into
  Freqtrade's `config.json` when a LIVE bot is launched. Dry-run needs no keys.
* **Open trades** (with *Exit* = Freqtrade's `/forceexit`), **Trade history** (paged from `/trades`), **Profit & loss**
  (daily / weekly / monthly), **Performance by pair**, **Wallet** (`/balance`), **Equity & profit history** (the
  dashboard's own minute snapshots, kept in `data/freqtrade.db`), **Recent activity** (manager events), **Logs**
  (the process output; Freqtrade's in-memory log when the API answers).
* **Strategies**: every `.py` file in `ft_user_data/strategies/` with a class extending `IStrategy`. *New strategy…*
  creates one from a working RSI + EMA template (name, timeframe, stop loss, ROI table); *Edit* opens the file in
  the page; *Validate & save* writes it, asks the Freqtrade webserver to load the class, and restores the previous
  version if Freqtrade refuses (syntax errors are caught before that). *Duplicate* copies a file under a new class
  name; *Delete* is refused for the configured strategy and for the one a running bot uses. Files you edit by hand
  are picked up the same way. A running bot keeps the strategy it loaded until you relaunch it.
* **Backtesting** runs on the webserver instance and never touches the trading bot: pick strategy, timeframe, date
  range, pairs, sizes; *Download the candles first* fetches the data from the exchange (into `ft_user_data/data/`).
  Results (summary, per pair, exit reasons, trades, daily profit) are shown and kept in Freqtrade's
  `backtest_results/` (reopen them under *Previous backtests*). Exchanges limit how far back small candles go (Gate:
  ~35 days of 5m), so a refused download says so instead of backtesting on nothing.

Every trade table carries a **SIM** (dry-run) or **REAL** (live) chip; the header badge says DRY-RUN or LIVE and the
page title too. Dry-run and live trades are kept in different databases (`tradesv3.dryrun.sqlite`,
`tradesv3.sqlite`).

## 4. Going live (read first)

Live trading needs all of these, and the manager enforces them (not just the page):

1. `FREQTRADE_LIVE_ACK=I_UNDERSTAND_THIS_TRADES_REAL_MONEY` in `.env` (restart the dashboard after adding it).
2. API keys for the configured exchange stored on the page (trading permission only, never withdrawals, IP-restricted).
3. The bot stopped, then **Arm LIVE trading…** with the exact phrase `TRADE REAL MONEY`.
4. **Launch bot**, which asks for the phrase again.

If any condition is missing at launch time the bot starts dry-run and the activity log says why. Live is disarmed
again when the keys are deleted, and on every dashboard start that finds no running live bot. `force_entry_enable`
stays off, so nothing can open a trade by hand through the API.

Freqtrade's own documentation on risk applies in full: https://www.freqtrade.io/en/stable/ (start with
*Strategy Customization*, *Backtesting* and *Dry-run / Live*).

## 5. Where things are

| What | Where |
|---|---|
| REST client | `freqkit/client.py` (HTTP Basic auth, every endpoint used) |
| Process manager, config generation, safety rules, backtest runner | `freqkit/manager.py` |
| Settings, keys, events, snapshots, backtest summaries | `freqkit/store.py` → `data/freqtrade.db` |
| Strategy files: list, template, validated save, duplicate, delete | `freqkit/strategies.py` + `FreqtradeManager` |
| Dashboard routes | `freqkit/routes.py`, mounted by `arbbot/server.py` under `/api/freqtrade/` |
| Page | `web/freqtrade.html` (served at `/trading`), link + summary card in `web/index.html` |
| Config section | `arbbot/config.py` (`FreqtradeCfg`), `config*.toml` `[freqtrade]` |
| Freqtrade's files | `ft_user_data/` (config.json, config.webserver.json, logs/, data/, backtest_results/, strategies/) |
| Docker | `deploy/docker-compose.freqtrade.yml` |
| Tests | `tests/test_freqkit.py` (`.venv/bin/python -m unittest tests.test_freqkit`) |

### API endpoints (dashboard side)

Reads (GET, no token): `/api/freqtrade/overview`, `trades?limit&offset`, `performance`, `pnl?period=day|week|month`,
`balance`, `logs?kind=trade|webserver`, `events`, `strategy?name=`, `exchanges`, `pairs[?markets=1]`, `whitelist`,
`locks`, `candles?pair&timeframe&limit`, `history?hours=`, `backtest`, `backtest/history`,
`backtest/result?filename&strategy`, `strategies`, `strategy/file?file=`.

Writes (POST, JSON, `X-Token` header): `engine {action: start|stop|restart}`, `webserver {action}`,
`bot {action: start|stop|pause|reload}`, `config {...settings}`, `exchange {exchange,key,secret,password} | {exchange, delete:true}`,
`mode {mode: dry_run|live, confirm}`, `forceexit {tradeid|"all", ordertype?}`, `backtest {strategy,timeframe,timerange,pairs,...} | {action: abort}`,
`blacklist {action: add|remove, pair}`, `lock {action: delete, id}`,
`strategy {action: create, name, timeframe, stoploss, roi, description, from_file?} | {action: save, file, code, name} | {action: delete, file}`.

Errors come back as `{"ok": false, "error": "..."}`: 400 for a bad request, 503 when Freqtrade is not reachable,
Freqtrade's own status when it refused something. The hosted (Render) mode never mounts these routes.

## 6. Troubleshooting

* **"Freqtrade is not installed"**: the executable in `[freqtrade] executable` is missing; install (section 1) or use
  the docker runner.
* **Engine error "Could not load markets"**: the exchange is unreachable from this machine. On this laptop the ISP's
  resolver blocks `api.binance.com` unless the VPN is connected (`getent hosts api.binance.com`); Gate, for one,
  resolves without it.
* **Backtest "no candles after the download"**: the exchange refused the range (too far back for that timeframe) or the
  pair does not exist there; the webserver log (Logs → Webserver) has the exchange's message.
* **Port in use**: another Freqtrade on 8080/8081, or a second dashboard with `[freqtrade] enabled = true`. Only one
  dashboard may manage it; `python -m freqkit status` shows what runs.
