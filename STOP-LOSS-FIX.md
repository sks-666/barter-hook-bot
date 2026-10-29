# Arbitrage stop-loss fix

One formula changed: the **stop-loss trigger** in `_exit_reason` in [arbbot/engine.py](arbbot/engine.py).

## The problem

On a coin with a wide bid-ask spread (common on the testnet's thin order books), closing right after the fill
already costs the whole spread plus fees. The entry rule had priced that spread in, but the stop-loss compared it
against a fixed 37 bps, so such trades were stopped out on the very next price check, before the price could move.

## Before

```
stop if:  loss_now  ≥  cost + stop_loss_bps
          loss_now  ≥  12   + 25            = 37 bps
```

## After

```
stop if:  loss_now  ≥  max(cost, entry_loss) + stop_loss_bps
```

## The terms

| Term | Meaning |
|---|---|
| `loss_now` | What closing **right now** would lose, in bps of the trade size. It covers real fill prices, the current bid-ask spread, all fees and funding. This calculation is unchanged; it moved into a helper, `_loss_bps`. |
| `cost` | The fixed round-trip cost from config: `2 × (futures fee + spot fee) + 4 × slippage` = **12 bps** on testnet. |
| `entry_loss` | **New.** `loss_now` measured once, right after the trade fills, and saved on the position as `entry_loss_bps`. |
| `stop_loss_bps` | **25 bps** from config. How far the trade may move against you. |

`loss_now` itself is calculated as before:

```
loss_now = −unrealized_usd / notional × 10,000

unrealized_usd = spot_qty × (spot_bid − spot_entry)
               + perp_qty × (perp_entry − perp_ask)
               + funding − entry fees − exit fees
```

## Example: the SHELL trade

Right after the fill, closing would already have lost about **49 bps**, mostly the wide testnet spread.

| | Stop level | Result |
|---|---|---|
| Before | 12 + 25 = **37 bps** | 49 ≥ 37 → stopped out after 5 ms |
| After | max(12, 49) + 25 = **74 bps** | stays open; stops only if the loss reaches 74 bps, i.e. 25 bps of real movement |

## What does not change

- **Narrow-spread coins** (typical on mainnet): `entry_loss` is about 12 bps or less, so `max(12, entry_loss)` = 12
  and the stop level stays 37 bps, as before.
- **Positions saved before the fix** have no `entry_loss_bps` (it reads as 0), so they keep the old rule.
- The second stop, the **basis-stop** on the mid-price, is unchanged.

## Files and tests

- [arbbot/models.py](arbbot/models.py): new `entry_loss_bps` field on `Position`.
- [arbbot/engine.py](arbbot/engine.py): `_loss_bps` helper, `entry_loss_bps` recorded at entry (automatic and
  manual `/open` trades), and the new stop formula.
- [tests/test_core.py](tests/test_core.py): `test_a_wide_spread_alone_does_not_stop_out_a_new_position` opens a trade
  with a 30 bps spread, checks it stays open while nothing moves, and is stopped out once the price moves 30 bps
  against it. It fails on the old formula and passes on the new one.
