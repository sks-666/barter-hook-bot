# Research plan: top-down trade plan (HTF trend → MTF zones → LTF entry → exit rules)

Goal: turn four chart-reading steps into **rules a program can run**, test them on history with fees included,
and only then write the result into a `trade-plan` file the terminal, swing kit and paper bot can read.

Nothing here is a claim that it makes money. Each step ends in a test that can fail. A step that fails is
dropped or changed, not argued with.

## 0. Ground rules (apply to every step)

- **Data:** Binance USD-M perpetuals, the same market the bot trades. Klines already exist in
  `data/klines/` and via `/api/market/history` (up to 5 years, 6 coins, 1h/4h/1d/1w).
- **Closed candles only.** A signal is decided at a candle's close and filled at the next open (as `swingkit` does).
- **Costs in every test:** taker fee 5 bps per fill, slippage 1–3 bps, funding while held. A rule that only
  works with zero fees is not a rule.
- **No look-ahead.** A higher-timeframe candle counts only after it closes. (The classic bug: reading today's
  daily candle at 10:00 as if it were finished.)
- **Split the data:** design on the oldest 60%, check on the next 20%, and look at the last 20% once, at the end.
- **Judge by many trades,** not a few: at least 100 trades per rule per coin group before any conclusion.
- **Paper first, then testnet, then tiny size.** The mainnet bot is never started by a plan file.

## 1. What the repo already gives us

| Need | Already in the repo |
|---|---|
| EMA / RSI / ATR, pivot highs and lows | `swingkit/indicators.py` |
| Trend, pullback and sweep→BOS→order-block entries | `swingkit/strategies.py` (`trend`, `swing`, `smc`) |
| Backtest with fees, slippage, funding | `swingkit/backtest.py`, `swingkit/cli.py backtest` |
| Position size from a stop distance, 1–2% risk cap | `swingkit/risk.py`, terminal RISK panel |
| Paper and demo accounts, journal | `swingkit/paper.py`, `swingkit/journal.py` |
| EMA50 + RSI swing signals on the chart | `web/terminal.html` (`swingSignals`) |
| Macro and news context | `arbbot/macro.py`, terminal ECO / NEWS |

What is **missing** and is the actual build: a multi-timeframe layer (steps 1–3 below talking to each other),
zone detection that is more than "a swing low", and a plan file format.

## 2. Step 1: Macro trend identification (higher time frame: 1d, 1w)

**Question:** is the market trending up, down, or neither, in a way we can state as a number?

**Candidate definitions to test (each is a different "trend" label):**

| ID | Rule | Notes |
|---|---|---|
| T1 | close > EMA200 (daily) | simplest; lags |
| T2 | EMA50 > EMA200 and EMA200 rising | what `trend` uses |
| T3 | market structure: last two swing highs and lows both higher (HH/HL) or both lower (LH/LL), using confirmed pivots (k=3–5) | needs `pivot_highs/lows` |
| T4 | weekly close vs weekly EMA20, agreeing with T2 | two-timeframe agreement |
| T5 | ADX(14) > 20–25 as a "is it trending at all" filter on top of any of the above | to find chop |

**Output:** one label per coin per day: `up`, `down` or `range`, plus how many days it has held.

**Research questions:**
1. How often does each rule flip? (More than ~6 flips a year on daily means it will whipsaw.)
2. After an `up` label, what are the forward 5/10/20-day returns versus after `range` and `down`? Compare with the unconditional average. If the labels do not separate the returns, the rule has no value.
3. Do BTC's labels lead the altcoins' (use BTC and ETH as the market gate for the rest)?
4. Does adding the macro layer (CPI, fed funds, curve, VIX from the ECO panel) change anything? Treat as a late, optional test; the chance it helps is small and it is easy to overfit.

**Pass bar:** a label set where forward returns differ in the expected direction on the held-out 20% too, after fees if traded.

## 3. Step 2: Mapping key structural zones (medium time frame: 4h, 1d)

**Question:** where, in price, does the market have a reason to react?

**Zone types to define precisely (a zone is a price range with a start time, not a line):**

| Zone | Definition to code | Invalidated when |
|---|---|---|
| Swing support / resistance | cluster of ≥2 confirmed pivots within 0.5–1 ATR of each other | a close beyond it by > 0.5 ATR |
| Prior-range high/low | highest high / lowest low of the last N days (N = 20, 55) | broken, then it flips role |
| Order block | last opposite candle before a displacement move (as `smc` uses) | a close through the far side |
| Fair value gap | 3-candle gap where candle 1's high < candle 3's low (or the mirror) | fully filled |
| Moving-average zone | EMA50 / EMA200 ± 0.5 ATR | n/a (dynamic) |
| Anchored VWAP | VWAP from the last major swing low / high | n/a (dynamic) |
| Liquidity pools | equal highs / equal lows (stop clusters) | swept |

**Output:** per coin, a list of live zones: `{type, low, high, created_at, touches, strength}`, refreshed on each closed 4h candle.

**Research questions:**
1. For each zone type, when price first returns to it, how often does it hold (reverse at least 1 ATR) versus break? Count it against a **control: random price levels** chosen the same way. Zones only matter if they beat random levels.
2. Does the first touch hold better than the 2nd or 3rd? (Common belief; test it.)
3. Which zone types work only in the direction of the step-1 trend? (Pullback to support in an `up` trend vs. against it.)
4. How wide is "a zone" before it stops being useful? Tight zones give small stops but get missed.

**Pass bar:** at least one zone type whose hold rate beats random levels by a clear margin on both the design and check data, in the trend direction.

## 4. Step 3: Timing the entry (lower time frame: 15m, 1h)

**Question:** inside a zone, what simple event says "now" rather than "falling knife"?

**Triggers to test (all on closed candles, only while price is inside a step-2 zone and step 1 allows the side):**

| ID | Trigger |
|---|---|
| E1 | RSI(14) crosses back through 40 (long) / 60 (short), the rule already in the terminal |
| E2 | Break of structure on the LTF: close above the last lower-high (long) after the zone touch |
| E3 | Liquidity sweep: wick below the zone low, close back inside it |
| E4 | Engulfing / pin-bar close at the zone edge |
| E5 | Volume or taker-buy imbalance spike on the touch candle (the history export has `taker_buy_usdt`) |
| E6 | Combination: E3 then E2 (what `smc` does) |

**Research questions:**
1. Which trigger gives the best result *after fees*, and is that stable across coins and across years?
2. How many signals per week? A rule that fires twice a year cannot be judged or traded.
3. What is the cost of waiting for the trigger (how much of the move is already gone)? Compare entering at the zone touch vs. at the trigger.
4. Does the HTF + zone filter actually improve the trigger, versus the trigger alone? This is the key test: **run each trigger with and without steps 1 and 2.** If the filters add nothing, the plan is just the trigger.

**Pass bar:** expectancy per trade positive after costs on design and check data, with enough trades, and clearly better with the filters than without.

## 5. Step 4: Exit points (stop loss and take profit)

**Question:** given an entry, where is the trade proven wrong, and where do we take profit?

**Stop candidates:**

| ID | Stop |
|---|---|
| S1 | beyond the zone edge + 0.25–0.5 ATR |
| S2 | beyond the sweep wick / last LTF swing + buffer |
| S3 | fixed 1.5 / 2 / 3 × ATR |
| S4 | structure stop that trails: behind each new confirmed swing in the trade's favour |

**Target candidates:**

| ID | Target |
|---|---|
| X1 | fixed 1.5R / 2R / 3R |
| X2 | next opposite zone from step 2 (the distance decides if the trade is worth taking at all) |
| X3 | partial: take half at 1R, move the stop to break-even, trail the rest |
| X4 | time stop: leave after N candles with no progress |
| X5 | trend exit: HTF label flips or price closes through EMA50 |

**Research questions:**
1. For each (entry, stop, target) combination: win rate, average win/loss in R, expectancy per trade, max drawdown, fees paid per trade.
2. **Minimum reward-to-risk filter:** skip trades whose nearest opposite zone is closer than 1.5R. Does that remove losers or just trades?
3. Do breakeven moves and partials help or just cut the winners? (They often lower expectancy while making it feel better.)
4. How often does a stop sit inside normal noise? Compare stop distance with the ATR of the entry timeframe. Stops tighter than ~1 ATR get hit by noise.
5. Fee drag: at 5 bps per side and ~10 bps round trip including slippage, what is the smallest stop distance where fees stay under, say, 10% of the risk? This sets a floor on trade size and stop width.

**Pass bar:** a combination that stays positive when slippage is doubled and when the entry is delayed by one candle. Fragile results get thrown out.

## 6. The trade-plan file (what the research feeds)

One file per coin/plan, JSON or TOML, read by the terminal and the swing paper accounts. Fields are filled by the
research results, not guessed. A sketch:

```toml
[plan]
name = "btc-pullback-v1"
symbol = "BTCUSDT"
status = "paper"            # draft | paper | testnet   (never "mainnet": that stays a manual decision)

[trend]                      # step 1
timeframe = "1d"
rule = "T2"                  # which tested definition
allow = ["up"]               # sides it may trade: up -> long only

[zones]                      # step 2
timeframe = "4h"
types = ["swing_sr", "order_block"]
max_age_days = 30
min_touches = 2

[entry]                      # step 3
timeframe = "1h"
trigger = "E6"
valid_for_candles = 6        # the trigger expires if not filled

[exit]                       # step 4
stop = "S2"
stop_buffer_atr = 0.25
target = "X2"
min_rr = 1.5
time_stop_candles = 48

[risk]
risk_per_trade_pct = 1.0     # capped by swingkit/risk.py
max_open = 2
fees_bps = 5
slippage_bps = 2

[evidence]                   # filled by the research, so the plan carries its own proof
tested = "2021-01..2026-10"
trades = 0
expectancy_r = 0.0
max_drawdown_r = 0.0
```

The `[evidence]` block matters: a plan with no numbers there is a draft and the dashboard should say so.

## 7. Build order

| # | Work | Output | Needs |
|---|---|---|---|
| 1 | Data loader: multi-timeframe aligned candles (HTF value as of each LTF candle, no look-ahead) | `mtf.py` + test that proves no look-ahead | klines |
| 2 | Step 1 labels + the forward-return study | table of T1–T5, one page of results | 1 |
| 3 | Step 2 zone finder + hold-vs-random study | zone list per coin, results table | 1 |
| 4 | Step 3 triggers, with and without filters | results table | 1–3 |
| 5 | Step 4 stop/target grid, with the robustness checks | results table | 1–4 |
| 6 | Combine the winners into a `swingkit` strategy (`mtf`) and backtest on the held-out data once | one backtest report | 1–5 |
| 7 | Plan file loader + validator, `[evidence]` filled from step 6 | `plans/*.toml` | 6 |
| 8 | Terminal overlay: show zones, the HTF label, and the plan's stop/target on the chart | PLAN panel | 7 |
| 9 | Paper-trade the plan on the swing paper accounts for 4+ weeks, compare with the backtest | live-vs-backtest gap | 7 |

Steps 1–6 are research, 7–9 are product. Decide after step 6 whether 7–9 are worth building; if no combination
survives the pass bars, the honest result is "no edge found" and that is a useful outcome too.

## 8. Risks to keep in view

- **Overfitting:** many rules × many parameters × few coins will always find something. Fix the parameter grid before looking, keep the final 20% untouched, and prefer rules that work on most coins with the same settings.
- **Regime dependence:** a rule tuned on a bull run can fail in a range. Report results per year, not only the total.
- **Survivorship:** only coins that still trade today are in the data; delisted ones are missing and would have hurt.
- **Subjective patterns:** "order block" and "fair value gap" have many definitions. The study tests *one written definition*; a different one is a different rule.
- **Execution gap:** backtests fill at the next open; real fills slip, especially on small coins (see CYBERUSDT's −88 bps in the paper bot).
- **Not advice:** this is a testing framework. Nothing in it tells you what to trade.

## 9. Decisions needed before building

1. **Coins:** BTC + ETH only, or the top 6–10 by volume?
2. **Timeframes:** the plan uses 1d/1w → 4h/1d → 15m/1h. Swing style (days) or faster?
3. **Sides:** long and short, or long only to start?
4. **Where it runs first:** swing paper accounts (recommended) or the testnet copy?

## 10. Progress log

### 2026-11-10: build steps 1–2 done, step 1 study run

**Built (all tested, 573 tests pass):**
- `swingkit/mtf.py`: weekly resampling (complete weeks only) and `align`, which gives each lower-timeframe candle the newest higher-timeframe candle that had already closed. Tests prove a 4h candle never sees an unfinished daily candle.
- `swingkit/trend.py`: trend rules T1–T5, plus `adx` in `swingkit/indicators.py`. A test proves no label changes when later candles are changed.
- `research/step1_trend.py`: the study. The last 20% of the data is hidden unless `--final` is passed. Output: `research/results/step1-trend.md`.
- Data: 5 years of daily and 4h USD-M candles (2021-11 to 2026-11) for BTC, ETH, SOL, BNB, XRP, DOGE in `data/klines/`.

**Result: no trend rule passes the step 1 bar.**
- T1, T2, T4, T5: forward returns after `up` and after `down` days are nearly the same on the design data (difference under 0.5%, |t| under 1.5). On the check data the `down` label is followed by *higher* returns: the market rebounded after downtrend labels.
- T3 (swing structure) looked best on the design data (up minus down +1.9% over 10 days) but the difference did not hold on the check data (−0.25%).
- So a daily trend label does not, by itself, predict the next 5–20 days in this sample.

**Why this is weaker evidence than it looks, in both directions:**
- One market cycle only (a 2021 top, a bear market, a rebound, a new drop). Trend rules are normally judged over several.
- The check period is dominated by XRP's +329% move. A few coins and a few episodes decide the averages.
- The six coins are today's survivors, and arithmetic mean returns of volatile assets look higher than typical outcomes.
- The final 20% (the latest drop) has not been looked at, on purpose.

**What this changes in the plan:**
1. The trend label is **not** used as a stand-alone direction call. It stays in the plan only as a *filter*, and it earns its place only if step 3 shows a trigger doing better with the filter than without it.
2. Add to step 1 a second, fairer question: does the label change **risk** (volatility, drawdown of a trade taken in its direction) even if it does not change the average return? Not run yet.
3. Next in the build order: step 2 (zone finder and the hold-versus-random study).

### 2026-11-10 (later): step 2 built and run

**Built (tests pass):** `swingkit/zones.py` (7 zone kinds, touch detection, the ±1 ATR outcome), `tests/test_zones.py` (zones and touches are identical whether or not later candles exist; a swing zone is known only after its pivot is confirmed), `research/step2_zones.py`. Results: `research/results/step2-zones.md`. Resistance zones reuse the same code on mirrored prices.

**Question tested:** when price first returns to a zone from above (support) or below (resistance), does it go +1 ATR in the zone's favour before −1 ATR against it within 5 days, more often than at a random level of the same size and distance from price?

**Result: no zone type beats random levels.**
- Two baselines were used: random levels at random times, and a stricter one, random levels born on the *same candle* as each real zone so the market context is the same.
- Fair value gaps looked best against plain random levels (+3.6 and +5.3 points, z 2.7 and 2.4). Against the same-time baseline the gap is +0.6 and +2.2 points (z 0.6 and 1.2): the "edge" came from FVGs forming right after a rally, not from the gap itself.
- Swing support/resistance and order blocks bounce *no more* than random (swing: −0.8 and −2.1 points). The one big number, order blocks −9.2 points on the check data, goes against the idea and is probably chance among ~30 comparisons.
- Moving-average zones (EMA50 +2.6 and +2.5 points, EMA200 +1.6 and +8.5) have the right sign in both periods, but the sizes are small and inconsistent, and their baseline is not time-matched.
- Later touches do not hold better than first touches. The daily trend does not change the result (FVG with trend 50.6%, against 52.5%).
- Raw rates are 44–54% on a symmetric ±1 ATR race. A 51% coin-flip win rate with equal win and loss, minus ~10 bps round-trip fees on a 4h ATR of roughly 0.8–1.5%, is not tradable by itself. Fees matter in step 3.

**Plan bar for step 2** ("at least one zone type that beats random levels by a clear margin on both design and check data") **is not met.**

**Caveats:** one written definition per zone kind; one reaction measure (±1 ATR, 5 days); one market cycle; the final 20% is unread; with ~30 comparisons a few |z| above 2 are expected by chance.

**Where this leaves the plan:** neither a daily trend label (step 1) nor a zone (step 2) shows a standalone edge. Step 3 is now the deciding test: run each entry trigger alone, with the trend filter, with a zone filter, and with both, **after fees**, and keep a filter only if it improves the result.

### 2026-11-10 (later still): step 3 built and run

**Built (tests pass):** `swingkit/triggers.py` (six triggers, long and short; a simulator that charges taker fee 5 bps + slippage 3 bps per fill and flat funding), `zones.active_bands` (which zones are alive and known at each candle), `tests/test_triggers.py` (signals and zone bands identical with or without later candles; stop wins when a candle reaches both; a gap through the stop fills at the open; fees, slippage and funding checked to the digit), `research/step3_triggers.py`. Results: `research/results/step3-triggers.md`, plus two other exit shapes as `step3-triggers-1atr-3r-48h.md` and `step3-triggers-2.5atr-1.5r-24h.md`. 1h candles for 6 coins, 5 years, are in `data/klines/`.

**Test:** E1–E6 plus a random-entry baseline, each alone and with the daily-trend filter, the zone filter and both; the same exit for everything (stop 1.5 ATR, target 2R, 48 h time exit); long and short; ~12,000 trades per row.

**Result: no trigger makes money after costs, with or without filters.**
- Random entries lose **−0.13 R per trade** (win rate 34%, 65% stopped out, 30% reach the target). That is the cost of trading: ~16 bps of fees and slippage plus funding on a stop only ~70 bps away.
- Every trigger is between −0.06 and −0.14 R. The best, **volume thrust** (−0.08 R) and **break of structure** (−0.10 R), beat random by about 0.05 R in both design and check data, which is consistent but recovers only about a third of the costs. Sweep, engulfing and RSI-cross are no better than random; sweep-then-BOS is about the same as BOS.
- **The filters make it worse, not better.** The trend filter lowers every trigger's result (volume thrust: −0.08 → −0.13 R). The zone filter changes nothing, because "price near any zone" is true on ~80% of candles. Both together: worse again.
- The same ranking holds with a 1 ATR stop / 3R target (random −0.19 R, best −0.12 R) and with a 2.5 ATR stop / 1.5R target / 24 h (random −0.09 R, best −0.06 R). The conclusion does not depend on the exit I picked.

**Plan bar for step 3** ("expectancy per trade positive after costs on design and check data, clearly better with the filters than without") **is not met.** To break even a trigger would have to lift the share of trades reaching a 2R target from ~30% to about 37–38%; the best one reaches ~32%.

**Caveats:** one exit family and one definition per trigger; taker fills assumed (limit entries would cut costs but add missed fills); coins are today's survivors; one market cycle; the final 20% is unread; funding is a flat 1 bp / 8 h, not the real rate.

**Where this leaves the plan:** steps 1–3 find no edge in this top-down recipe as written. Step 4 (exits) can still change the result because the exit decides the cost per trade, but it cannot create an entry edge; it is worth running mainly to test whether a wider stop or limit-order exits could lower cost below the ~0.05 R the best triggers add. It is also reasonable to stop here: the honest finding so far is "no edge found".

### 2026-11-10 (end of day): step 4 built and run

**Built (tests pass):** `swingkit/exits.py` (stops: N×ATR or under the last confirmed swing low; targets: fixed R, half at 1R then break-even, structure trail, exit on a close under the 1h EMA50; conservative intrabar rules), `tests/test_exits.py` (the new simulator reproduces the step 3 simulator to the last digit on 400 random trades, and every new exit is checked by hand), `research/step4_exits.py`. Results: `research/results/step4-exits.md`.

**Test:** 5 stops × 6 target styles × 2 time limits = 60 exits, for volume thrust, break of structure, RSI cross and random entries (~12,000 trades per cell, all costs in). The winner of each entry set was chosen on the design period only, then judged on the check period and under three stresses.

**Result: no exit makes any entry profitable after costs.**
- **Exits change the cost, not the edge.** Widening the stop from 1 ATR to 3 ATR cuts fees and slippage from ~20% of the amount risked to ~6% (BTC: 24% → 8%), and random entries improve from −0.19 R to −0.06 R per trade. Entries that were no better than random stay no better.
- **Best combinations (chosen on design data):** break of structure with a swing-low stop, structure trail and 96 h limit: design −0.03 R, check **+0.00 R** (t = 0.1; break-even before any real-world friction). Volume thrust with 3 ATR / EMA50 exit: −0.03 / −0.02. RSI cross: −0.05 / −0.05 (t −2.7). Random entries with the same kind of exit: −0.06 / −0.01.
- **Stresses on the winners:** doubling slippage, entering one candle late and using maker-fee limit targets all leave them between −0.07 and 0.00 R. None is positive.
- **Half-off-at-1R-then-break-even is the worst management style everywhere** (−0.09 to −0.36 R): it sells winners early and gets stopped at break-even by normal noise.

**Plan bar for step 4** ("a combination that stays positive when slippage is doubled and the entry delayed one candle") **is not met.**

## 11. Outcome of the research (steps 1–4)

**No edge found.** On six major USD-M perpetuals, five years of data, long and short, with realistic costs:
1. A daily trend label does not predict the next 5–20 days (step 1).
2. Support/resistance zones, order blocks, fair value gaps and moving-average zones bounce no more often than random price levels once the comparison is matched in time (step 2).
3. Six entry triggers lose money after fees, random entries lose about as much, and the trend and zone filters make things worse (step 3).
4. No stop/target combination turns any of them positive; the best are break-even at best (step 4).

**Decision on the build order:** steps 7–9 (trade-plan file loader, terminal overlay, paper-trading the plan) are **not worth building** for this recipe: a plan file's `[evidence]` block would read "expectancy ≈ 0 R or worse". The tooling stays in the repo (`swingkit/mtf.py`, `trend.py`, `zones.py`, `triggers.py`, `exits.py`, `research/`) so a different idea can be tested the same way in an afternoon.

**What was not tested, honestly:** other coins and the long tail of small caps; timeframes slower than 1d entries or faster than 1h; discretionary reading of charts (not codeable); funding/basis information as a filter; other written definitions of order blocks and fair value gaps. The final 20% of the data (the latest down move) was deliberately never opened, because nothing passed the design and check data.

**The contrast with the arbitrage bot:** it earns from a structural, measurable gap (basis and funding) and its paper results show costs of ~30 bps per round trip eating almost all of the gross profit; that is a cost problem with a real edge underneath, which is a better place to work than a direction-prediction problem with no edge.
