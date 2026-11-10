# Step 3: entry triggers after fees, alone and with filters

Data: BTC, ETH, SOL, BNB, XRP, DOGE, USD-M perpetual 1h candles 2021-11-11 to 2026-11-10, long and short. Every trade: enter at the next open, stop 1 ATR(1h) away, target 3R, exit after 48 h, taker fee 5 bps + slippage 3 bps per fill (no slippage on the target), funding 1 bp per 8 h. R = result in units of the initial risk after all costs; bps = net result per trade; t = mean R over its standard error (trades of different coins overlap in time, so treat |t| under 3 as noise). `random` = entries at random candles under the same filter and exits: the baseline.

```
trigger     filter | design n   win%      R   bps     t | check  n   win%      R   bps     t 
random      none   | 10963    25%  -0.18    -18 -11.0  |  3729    24%  -0.23    -20  -8.2  
random      trend  |  3939    25%  -0.18    -16  -6.8  |  1594    24%  -0.24    -22  -5.8  
random      zone   |  8761    25%  -0.19    -18 -10.3  |  2965    24%  -0.22    -17  -7.2  
random      both   |  3160    25%  -0.20    -16  -6.6  |  1256    23%  -0.27    -24  -5.7  

rsi_cross   none   | 10140    26%  -0.16    -14  -9.7  |  3545    26%  -0.13     -8  -4.5  
rsi_cross   trend  |  3709    25%  -0.21    -20  -7.8  |  1577    26%  -0.15    -13  -3.6  
rsi_cross   zone   |  9129    26%  -0.17    -15  -9.6  |  3182    27%  -0.12     -7  -3.9  
rsi_cross   both   |  3406    25%  -0.22    -20  -7.5  |  1448    26%  -0.15    -12  -3.2  

bos         none   |  8958    26%  -0.15    -17  -8.5  |  3095    25%  -0.18    -22  -6.0  
bos         trend  |  3213    25%  -0.18    -17  -6.0  |  1352    25%  -0.18    -21  -4.0  
bos         zone   |  6621    25%  -0.18    -17  -8.3  |  2255    25%  -0.18    -21  -5.0  
bos         both   |  2369    25%  -0.20    -18  -5.6  |   953    25%  -0.18    -21  -3.2  

sweep       none   |  9544    25%  -0.21    -19 -12.1  |  3187    25%  -0.17    -16  -5.7  
sweep       trend  |  3529    24%  -0.24    -20  -8.4  |  1383    24%  -0.21    -22  -4.6  
sweep       zone   |  8460    24%  -0.22    -20 -11.9  |  2832    25%  -0.17    -16  -5.4  
sweep       both   |  3148    24%  -0.24    -22  -8.0  |  1232    24%  -0.22    -22  -4.5  

engulf      none   | 16477    25%  -0.19    -18 -14.3  |  5614    25%  -0.16    -16  -7.0  
engulf      trend  |  5952    25%  -0.20    -16  -8.9  |  2403    25%  -0.18    -15  -5.1  
engulf      zone   | 13575    25%  -0.20    -18 -13.4  |  4622    25%  -0.19    -18  -7.4  
engulf      both   |  4902    25%  -0.21    -17  -8.6  |  1943    25%  -0.19    -18  -4.9  

vol_thrust  none   |  6864    27%  -0.11     -8  -5.3  |  2569    26%  -0.13    -14  -3.8  
vol_thrust  trend  |  2506    25%  -0.18    -15  -5.3  |  1117    25%  -0.19    -19  -3.6  
vol_thrust  zone   |  5332    27%  -0.12     -8  -5.0  |  1952    26%  -0.15    -14  -3.8  
vol_thrust  both   |  1921    25%  -0.19    -14  -4.9  |   824    23%  -0.27    -30  -4.7  

sweep_bos   none   |  3287    25%  -0.19    -16  -6.2  |  1139    26%  -0.15    -12  -2.8  
sweep_bos   trend  |  1186    26%  -0.15    -10  -3.0  |   486    25%  -0.18    -19  -2.4  
sweep_bos   zone   |  2576    24%  -0.21    -17  -6.4  |   893    27%  -0.12     -6  -2.0  
sweep_bos   both   |   936    26%  -0.18    -10  -3.2  |   377    26%  -0.15    -15  -1.7  

```

## Does a filter help? Mean R, design + check pooled (n in brackets)

```
trigger                  none           trend            zone            both
random         -0.19 (14692)    -0.20 (5533)   -0.20 (11726)    -0.22 (4416)
rsi_cross      -0.15 (13685)    -0.20 (5286)   -0.16 (12311)    -0.20 (4854)
bos            -0.16 (12053)    -0.18 (4565)    -0.18 (8876)    -0.19 (3322)
sweep          -0.20 (12731)    -0.23 (4912)   -0.21 (11292)    -0.23 (4380)
engulf         -0.18 (22091)    -0.19 (8355)   -0.19 (18197)    -0.20 (6845)
vol_thrust      -0.12 (9433)    -0.18 (3623)    -0.13 (7284)    -0.22 (2745)
sweep_bos       -0.18 (4426)    -0.16 (1672)    -0.19 (3469)    -0.17 (1313)
```

## How the trades ended (no filter, design + check)

```
trigger       stop   target     time
random           75%     23%      2%
rsi_cross        74%     23%      3%
bos              74%     24%      2%
sweep            75%     23%      2%
engulf           75%     23%      2%
vol_thrust       73%     25%      2%
sweep_bos        74%     24%      2%
```
