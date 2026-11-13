#!/bin/bash
# Double-click to run backtests from a menu. Research only: nothing here places an order or touches your account
# (the swing backtests download public candles and funding history from Binance; the replays read the chosen bot's
# recordings, data/recordings-paper or data/recordings-mainnet).
# Every run is also saved to logs/backtests/<swing|paper|mainnet>/ (the report as .txt, swing trades as .csv).
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
OUT=logs/backtests
mkdir -p "$OUT"
export PYTHONUNBUFFERED=1            # so the report shows up as it runs, not all at the end

# Each bot keeps its own data, and so do the backtests:
#   replays     read the chosen bot's recordings with that bot's config; saved to logs/backtests/<bot>/
#   swing tests use public candles (no bot's data); saved to logs/backtests/swing/, journal data/swing-journal-backtest.db
BOT=paper                            # whose recordings the replays use: paper | mainnet (testnet records nothing)
BACKTEST_JOURNAL=data/swing-journal-backtest.db
set_bot() {   # $1 = paper | mainnet; sets REC (recordings folder) and CFG (that bot's config)
    case "$1" in
        paper)   BOT=paper;   REC=data/recordings-paper;   CFG=config.paper.toml ;;
        mainnet) BOT=mainnet; REC=data/recordings-mainnet; CFG=config.toml ;;
        *) return 1 ;;
    esac
}
set_bot "$BOT"

choose_bot() {
    ask "Replay whose recordings: paper or mainnet (testnet does not record)" "$BOT"
    if [[ "$REPLY" == "testnet" ]]; then
        echo "  The testnet bot does not record the market (recorder off in config.testnet.toml)."
    elif ! set_bot "$REPLY"; then
        echo "  Type paper or mainnet."
    fi
    echo "  Replays now use: $BOT ($REC, $CFG)"
}

choose_journal() {   # sets JOURNAL to the chosen swing journal
    ask "Which journal: paper, testnet, mainnet or backtest" paper
    case "$REPLY" in
        paper)    JOURNAL=data/swing-journal.db ;;
        testnet)  JOURNAL=data/swing-journal-testnet.db ;;
        mainnet)  JOURNAL=data/swing-journal-live-mainnet.db ;;
        backtest) JOURNAL=$BACKTEST_JOURNAL ;;
        *) echo "  Type paper, testnet, mainnet or backtest."; return 1 ;;
    esac
    if [[ ! -f "$JOURNAL" ]]; then echo "  No journal yet: $JOURNAL"; return 1; fi
}

ask() {   # $1 = question, $2 = default; the answer goes to $REPLY
    read -r -p "  $1 [$2]: " REPLY
    REPLY=${REPLY:-$2}
}

run() {   # $1 = subfolder of logs/backtests (swing | paper | mainnet), $2 = short name, then the command
    local dir="$OUT/$1" name=$2; shift 2
    mkdir -p "$dir"
    local file="$dir/$(date +%Y-%m-%d_%H%M%S)-$name.txt"
    echo
    echo "  > $*"
    echo
    { echo "# $(date)"; echo "# $*"; echo; } > "$file"
    "$@" 2>&1 | tee -a "$file"
    if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
        echo
        echo "  Saved: $file"
    else
        echo
        echo "  The run stopped with an error (see above). Output saved: $file"
    fi
}

recordings() {   # the replay tools need recorded days; say so instead of failing
    if ! ls "$REC"/quotes-*.jsonl.gz >/dev/null 2>&1; then
        echo
        echo "  No recordings yet in $REC. The $BOT bot records the market while it runs on a real feed;"
        echo "  let it run for a few days, or switch bots with b)."
        return 1
    fi
    echo
    echo "  $BOT bot, recorded days: $(ls "$REC"/quotes-*.jsonl.gz | sed 's/.*quotes-//; s/.jsonl.gz//' | tr '\n' ' ')"
    echo "  (each day is up to ~100 MB, so this can take a few minutes)"
}

swing_custom() {
    echo
    echo "  Press Enter to keep the value in [brackets]."
    ask "Strategy: trend, swing, smc or all" all; local strategy=$REPLY
    ask "Coins, comma separated" BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT; local symbols=$REPLY
    ask "Candle size: 1h 2h 4h 6h 8h 12h 1d" 4h; local interval=$REPLY
    ask "Days of history" 730; local days=$REPLY
    ask "Risk per trade in % (2 at most)" 1; local risk=$REPLY
    ask "Starting capital in USDT" 10000; local capital=$REPLY
    ask "Also short? y/n" y; local short=$REPLY
    ask "Save the trades to the journal too? y/n" n; local journal=$REPLY
    if ! [[ "$days" =~ ^[0-9]+$ ]]; then echo "  Days must be a whole number."; return; fi
    local stamp; stamp=$(date +%Y-%m-%d_%H%M%S)
    mkdir -p "$OUT/swing"
    local cmd=("$PY" swing.py backtest --strategy "$strategy" --symbols "$symbols" --interval "$interval"
               --days "$days" --risk-pct "$risk" --capital "$capital" --csv "$OUT/swing/$stamp-trades.csv")
    [[ "$short" == "n" || "$short" == "N" ]] && cmd+=(--no-short)
    [[ "$journal" == "y" || "$journal" == "Y" ]] && cmd+=(--journal --journal-db "$BACKTEST_JOURNAL")
    run swing "swing-$strategy-$interval" "${cmd[@]}"
}

while true; do
    echo
    echo "=== Backtesting (research only: no orders, no account access) ==="
    echo
    echo "  Swing toolkit: trend, swing and SMC on past Binance candles"
    echo "    1) All three strategies, 2 years of 4h candles, BTC ETH SOL BNB XRP"
    echo "    2) All three strategies, 4 years of daily candles"
    echo "    3) Choose: strategy, coins, candle size, days, risk, shorts"
    echo "    4) Journal summary (choose: paper, testnet, mainnet or saved backtests)"
    echo
    echo "  Arbitrage bot: replay the days the $BOT bot recorded ($REC, $CFG)"
    echo "    b) Switch bot: paper or mainnet"
    echo "    5) What has been recorded"
    echo "    6) Replay the strategy over the recorded days"
    echo "    7) Compare settings (sweep), e.g. strategy.z_entry=2.5,3,4,5"
    echo "    8) Slippage: price impact by order size"
    echo
    echo "    q) Quit"
    echo
    read -r -p "Choose: " choice
    case "$choice" in
        1) mkdir -p "$OUT/swing"
           run swing swing-all-4h "$PY" swing.py backtest --csv "$OUT/swing/$(date +%Y-%m-%d_%H%M%S)-trades.csv" ;;
        2) mkdir -p "$OUT/swing"
           run swing swing-all-1d "$PY" swing.py backtest --interval 1d --days 1460 \
               --csv "$OUT/swing/$(date +%Y-%m-%d_%H%M%S)-trades.csv" ;;
        3) swing_custom ;;
        4) choose_journal && run swing "journal-$(basename "$JOURNAL" .db)" "$PY" swing.py journal --db "$JOURNAL" --summary ;;
        b|B) choose_bot ;;
        5) recordings && run "$BOT" replay-info "$PY" backtest.py info "$REC" --config "$CFG" ;;
        6) recordings && run "$BOT" replay "$PY" backtest.py replay "$REC" --config "$CFG" ;;
        7) if recordings; then
               ask "Setting and values to compare" strategy.z_entry=2.5,3,4,5
               run "$BOT" replay-sweep "$PY" backtest.py replay "$REC" --config "$CFG" --sweep "$REPLY"
           fi ;;
        8) if recordings; then
               ask "Order sizes in USDT" 200,1000,5000,25000
               run "$BOT" slippage "$PY" backtest.py slippage "$REC" --config "$CFG" --sizes "$REPLY"
           fi ;;
        q|Q) break ;;
        *) echo "  Type a number from the menu, or q." ;;
    esac
done
echo "Results are in $OUT/. You can close this window."
