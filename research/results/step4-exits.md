# Step 4: exits after costs

Data: BTC, ETH, SOL, BNB, XRP, DOGE, USD-M perpetual 1h candles 2021-11-11 to 2026-11-10, long and short. Entry at the next open; taker fee 5 bps + slippage 3 bps per fill, no slippage on a limit target, funding 1 bp per 8 h. R = net result in units of the initial risk. Stops: atrN = N x ATR(1h) below the entry; swing = under the last confirmed swing low (1-4 ATR). Targets: rrN = N x risk; half+rr3 = half at 1R, stop to break-even, rest at 3R; trail = stop follows each new confirmed swing low; ema50 = leave after a close under the 1h EMA50. random = entries at random candles (the baseline).

## random · time exit after 48 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.20/-0.18   -0.19/-0.16   -0.19/-0.15   -0.30/-0.28   -0.21/-0.08   -0.19/-0.07
atr1.5     -0.13/-0.11   -0.13/-0.11   -0.14/-0.11   -0.19/-0.16   -0.14/-0.05   -0.13/-0.04
atr2.0     -0.10/-0.09   -0.10/-0.09   -0.08/-0.07   -0.14/-0.11   -0.11/-0.04   -0.09/-0.03
atr3.0     -0.07/-0.07   -0.06/-0.06   -0.06/-0.06   -0.08/-0.08   -0.07/-0.03   -0.06/-0.02
swing      -0.12/-0.11   -0.13/-0.10   -0.11/-0.10   -0.18/-0.15   -0.14/-0.06   -0.13/-0.06
```

## random · time exit after 96 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.20/-0.18   -0.19/-0.16   -0.19/-0.15   -0.30/-0.28   -0.21/-0.02   -0.18/-0.05
atr1.5     -0.13/-0.11   -0.13/-0.10   -0.14/-0.11   -0.19/-0.17   -0.14/-0.02   -0.12/-0.03
atr2.0     -0.10/-0.07   -0.10/-0.08   -0.09/-0.07   -0.14/-0.10   -0.11/-0.01   -0.09/-0.02
atr3.0     -0.07/-0.08   -0.07/-0.06   -0.08/-0.06   -0.09/-0.07   -0.07/-0.01   -0.06/-0.01
swing      -0.12/-0.11   -0.13/-0.09   -0.12/-0.09   -0.18/-0.15   -0.15/-0.03   -0.12/-0.05
```

## vol_thrust · time exit after 48 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.14/-0.17   -0.13/-0.17   -0.11/-0.13   -0.33/-0.35   -0.10/-0.14   -0.11/-0.06
atr1.5     -0.07/-0.10   -0.07/-0.10   -0.07/-0.08   -0.19/-0.20   -0.07/-0.11   -0.07/-0.04
atr2.0     -0.06/-0.07   -0.07/-0.05   -0.07/-0.04   -0.14/-0.13   -0.07/-0.06   -0.06/-0.01
atr3.0     -0.06/-0.05   -0.06/-0.04   -0.06/-0.02   -0.09/-0.08   -0.05/-0.05   -0.04/-0.01
swing      -0.08/-0.07   -0.07/-0.07   -0.04/-0.03   -0.15/-0.11   -0.08/-0.10   -0.06/-0.06
```

## vol_thrust · time exit after 96 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.14/-0.17   -0.12/-0.17   -0.10/-0.14   -0.33/-0.35   -0.10/-0.12   -0.10/-0.07
atr1.5     -0.06/-0.10   -0.06/-0.11   -0.05/-0.09   -0.19/-0.21   -0.07/-0.09   -0.06/-0.04
atr2.0     -0.05/-0.08   -0.05/-0.06   -0.05/-0.06   -0.14/-0.13   -0.06/-0.04   -0.05/-0.01
atr3.0     -0.05/-0.04   -0.06/-0.04   -0.05/-0.03   -0.09/-0.08   -0.05/-0.04   -0.03/-0.02
swing      -0.07/-0.06   -0.07/-0.07   -0.04/-0.07   -0.15/-0.12   -0.09/-0.09   -0.05/-0.07
```

## bos · time exit after 48 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.17/-0.19   -0.16/-0.19   -0.15/-0.18   -0.36/-0.33   -0.14/-0.09   -0.13/-0.06
atr1.5     -0.10/-0.12   -0.10/-0.11   -0.09/-0.10   -0.22/-0.19   -0.08/-0.03   -0.09/-0.02
atr2.0     -0.07/-0.09   -0.07/-0.08   -0.08/-0.07   -0.14/-0.13   -0.05/-0.02   -0.07/-0.02
atr3.0     -0.05/-0.05   -0.06/-0.03   -0.07/-0.02   -0.08/-0.07   -0.04/-0.02   -0.04/-0.01
swing      -0.06/-0.05   -0.06/-0.06   -0.07/-0.04   -0.09/-0.07   -0.04/-0.01   -0.05/-0.01
```

## bos · time exit after 96 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.17/-0.19   -0.16/-0.19   -0.15/-0.18   -0.36/-0.33   -0.14/-0.06   -0.12/-0.05
atr1.5     -0.10/-0.12   -0.10/-0.11   -0.09/-0.09   -0.21/-0.19   -0.07/-0.02   -0.06/-0.02
atr2.0     -0.07/-0.08   -0.07/-0.07   -0.07/-0.05   -0.14/-0.11   -0.05/-0.00   -0.06/-0.01
atr3.0     -0.05/-0.03   -0.05/-0.03   -0.04/-0.01   -0.07/-0.06   -0.04/+0.00   -0.04/-0.01
swing      -0.05/-0.05   -0.05/-0.05   -0.04/-0.01   -0.08/-0.06   -0.03/+0.00   -0.05/-0.01
```

## rsi_cross · time exit after 48 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.16/-0.14   -0.15/-0.14   -0.16/-0.13   -0.28/-0.24   -0.17/-0.16   -0.19/-0.17
atr1.5     -0.11/-0.10   -0.11/-0.10   -0.10/-0.11   -0.17/-0.16   -0.10/-0.10   -0.13/-0.11
atr2.0     -0.10/-0.09   -0.09/-0.08   -0.08/-0.10   -0.12/-0.11   -0.08/-0.08   -0.10/-0.08
atr3.0     -0.07/-0.06   -0.07/-0.07   -0.06/-0.07   -0.09/-0.07   -0.06/-0.06   -0.07/-0.06
swing      -0.12/-0.12   -0.11/-0.11   -0.12/-0.10   -0.20/-0.17   -0.11/-0.10   -0.14/-0.13
```

## rsi_cross · time exit after 96 h · mean R, design / check

```
                 rr1.5           rr2           rr3      half+rr3         trail         ema50
atr1.0     -0.16/-0.15   -0.15/-0.14   -0.16/-0.13   -0.28/-0.24   -0.15/-0.14   -0.19/-0.17
atr1.5     -0.10/-0.10   -0.11/-0.11   -0.09/-0.10   -0.17/-0.16   -0.10/-0.09   -0.13/-0.11
atr2.0     -0.10/-0.09   -0.08/-0.09   -0.08/-0.08   -0.12/-0.10   -0.08/-0.07   -0.10/-0.09
atr3.0     -0.07/-0.07   -0.07/-0.08   -0.07/-0.06   -0.10/-0.04   -0.05/-0.05   -0.07/-0.06
swing      -0.11/-0.11   -0.10/-0.10   -0.10/-0.10   -0.20/-0.17   -0.11/-0.08   -0.14/-0.13
```

## The design-period winner of each entry set, then stressed on the check period

Chosen by mean R on the design period only (at least 300 trades). 60 exits were tried per entry set, so the best of them is flattered by luck: the check period is the honest number.

```
entry      exit (stop, target, hold)    design R  check R     n  win%  slip x2  late 1 maker tgt     t
random     atr3.0, ema50, 96h              -0.06    -0.01  3075    32    -0.04   -0.02     -0.01  -0.8
vol_thrust atr3.0, ema50, 96h              -0.03    -0.02  1949    30    -0.04   -0.03     -0.02  -0.6
bos        swing, trail, 96h               -0.03    +0.00  1990    32    -0.02   -0.01     +0.00   0.1
rsi_cross  atr3.0, trail, 96h              -0.05    -0.05  2977    29    -0.07   -0.04     -0.05  -2.7
```

## The cost floor: what fees and slippage take out of a trade, by stop width

Round trip = 2 x 5 bps fee + 2 x 3 bps slippage = 16 bps (+ funding). Cost as a share of the amount risked, using each coin's median 1h ATR.

```
coin    median ATR  stop 1.0 ATR  stop 1.5 ATR  stop 2.0 ATR  stop 3.0 ATR
BTC          66 bps           24%           16%           12%            8%
ETH          89 bps           18%           12%            9%            6%
SOL         126 bps           13%            8%            6%            4%
BNB          75 bps           21%           14%           11%            7%
XRP          98 bps           16%           11%            8%            5%
DOGE        117 bps           14%            9%            7%            5%
```
