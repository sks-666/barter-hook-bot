# Binance bots: opening the dashboards on your phone

Last updated: 2026-11-07

## The bots and their ports

All bots live in `/srv/apps/binance-arb-bot` and run on this laptop.

| Bot | Port | What it is | Real money? |
|---|---|---|---|
| Paper + swingkit | 8765 | Live mainnet prices, simulated fills. Runs the `trend`, `swing` and `smc` swing paper accounts ($10,000 each). | No |
| Mainnet (live) | 8770 | Live mainnet, `--no-trade`: never opens arbitrage trades by itself. Its order ticket sends real orders (max $100 each, you confirm each one). | Only the order ticket |
| Testnet | 8767 | Binance testnet (fake money). **Currently not wanted; keep it stopped.** | No |

## Phone links

Format: `http://<laptop IP>:<port>/?key=<key>`

- Use `http://`, not `https://`.
- The `?key=...` part is required. Without it the dashboard only shows a "locked" page.
- Each port has its own key, stored in `/srv/apps/binance-arb-bot/data/lan-key-<port>.txt`
  (for example `data/lan-key-8765.txt`). The bot also prints the full phone link in its log as `phone: http://...`.
- The phone must be on the **same Wi-Fi** as the laptop (its IP should start with `192.168.100.`).

Laptop IP on 2026-11-07: `192.168.100.133`. It has changed several times (.77, .130, .133); see "IP keeps changing" below.

## The fix that made phone access work

The laptop's **UFW firewall** was active with a default incoming policy of `DROP`, so it silently blocked every
other device on the Wi-Fi. The fix was this rule (run once, needs your password):

```bash
sudo ufw allow from 192.168.100.0/24 to any port 8765:8770 proto tcp
```

It opens ports 8765-8770 to devices on the home Wi-Fi only, not to the internet. Check it with `sudo ufw status`.

### NordVPN settings (also changed)

With NordVPN connected, its own firewall can also block local devices. These were set:

```bash
nordvpn set lan-discovery off
nordvpn allowlist add subnet 192.168.100.0/24
```

(LAN Discovery and a subnet allowlist can't both be on; the allowlist was chosen.)

## Troubleshooting checklist

If the link times out on the phone:

1. **Is the bot running?** `ss -ltnp | grep -E "8765|8767|8770"` should show the port.
2. **Did the laptop's IP change?** `ip -4 addr show wlp2s0` and use the `inet` address in the link.
3. **Is the phone on the same Wi-Fi?** Its IP must start with `192.168.100.`, not mobile data or another router.
4. **Did the Wi-Fi network change?** If the laptop's IP no longer starts with `192.168.100.`, the UFW rule and the
   NordVPN allowlist above must be redone for the new subnet.
5. **Is NordVPN blocking it?** Test with `nordvpn disconnect`, retry, then `nordvpn connect`.
6. **Router isolation:** on guest Wi-Fi or with "AP/client isolation" turned on, devices can't reach each other.

## IP keeps changing

The router hands out the laptop's IP dynamically, so it changes when the Wi-Fi reconnects. For a permanent fix,
add a **DHCP reservation** for this laptop in the router's admin page (router at `192.168.100.1`).

## Starting and stopping

- `./stop-bots.sh` stops the mainnet and testnet bots (not the paper bot).
- `./start-bots.sh` asks before starting the mainnet bot (you must type `TRADE REAL MONEY`) and **always starts the
  testnet bot too**. Stop testnet afterwards if you don't want it:
  `kill $(pgrep -f "[.]venv/bin/python run.py --live --testnet")`
- The paper bot with the swingkit was started by hand (it does not restart after a reboot):

  ```bash
  cd /srv/apps/binance-arb-bot
  nohup .venv/bin/python run.py --config config.paper.toml --swing --lan --no-commands > logs/paper-bot.out 2>&1 &
  ```

- Emergency stop for every bot: `touch /srv/apps/binance-arb-bot/KILL`

## Where each bot keeps its data (all separate)

All in `/srv/apps/binance-arb-bot/data/`:

| Data | Paper (8765) | Mainnet (8770) | Testnet (8767) |
|---|---|---|---|
| Trades, positions, P&L | `paper-mainnet-real.db` | `live-mainnet-real.db` | `live-testnet-real.db` |
| Swing journal | `swing-journal.db` | `swing-journal-live-mainnet.db` (only if started with `--swing`) | `swing-journal-testnet.db` |
| Order ticket | in its own `.db` | `ticket-live-mainnet-real.json` | `ticket-live-testnet-real.json` |
| Swing mirror | none | none | `swing-mirror-testnet-<account>.json` |
| Market recordings | `recordings-paper/` | `recordings-mainnet/` | off |

The trades database name is built from mode + network (`paper`/`live` + `mainnet`/`testnet`), so the three can
never share a file. On 2026-11-07 `arbbot/main.py` was changed so a live mainnet bot with `--swing` gets its own
journal instead of sharing the paper bot's (backup: `backups/main.py.bak-20261107`).

Shared on purpose (not trade data): `klines/` (candle cache), `desk/` (research notes), the `KILL` file (one
emergency stop for every bot), and the Telegram chat. Only the paper bot publishes to the hosted Vercel dashboard;
`start-bots.sh` blanks the push settings for the mainnet and testnet bots.

## Backtests (also separate per bot)

Run `./run-backtests.sh` in the bot folder (changed 2026-11-07; backup `backups/run-backtests.sh.bak-20261107`).

| Backtest | Reads | Saves to |
|---|---|---|
| Arbitrage replay, info, sweep, slippage (menu 5-8) | the chosen bot's recordings and config: paper = `data/recordings-paper` + `config.paper.toml`, mainnet = `data/recordings-mainnet` + `config.toml` | `logs/backtests/paper/` or `logs/backtests/mainnet/` |
| Swing backtests (menu 1-3) | public Binance candles (no bot's data) | `logs/backtests/swing/`; "save to journal" goes to `data/swing-journal-backtest.db` |
| Journal summary (menu 4) | the journal you pick: paper, testnet, mainnet or backtest | `logs/backtests/swing/` |

- Press `b` in the menu to switch the replays between paper and mainnet (paper is the default).
- The testnet bot has no replays: its recorder is off in `config.testnet.toml`.
- Before this change the replays read `data/recordings`, which no longer exists, so options 5-8 did not work.

## Account balance (mainnet, 2026-11-07)

From the bot's read-only check (`.venv/bin/python run.py --check --config config.toml`):
futures 0.00 USDT available, spot 20.00 USDT free. Arbitrage needs money in **both** spot (to buy the coin) and
USDⓈ-M futures (margin for the short leg).
