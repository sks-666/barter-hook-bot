# Step 3: entry triggers after fees, alone and with filters

Data: BTC, ETH, SOL, BNB, XRP, DOGE, USD-M perpetual 1h candles 2021-11-11 to 2026-11-10, long and short. Every trade: enter at the next open, stop 1.5 ATR(1h) away, target 2R, exit after 48 h, taker fee 5 bps + slippage 3 bps per fill (no slippage on the target), funding 1 bp per 8 h. R = result in units of the initial risk after all costs; bps = net result per trade; t = mean R over its standard error (trades of different coins overlap in time, so treat |t| under 3 as noise). `random` = entries at random candles under the same filter and exits: the baseline.

```
trigger     filter | design n   win%      R   bps     t | check  n   win%      R   bps     t 
random      none   |  9641    34%  -0.12    -16  -8.3  |  3284    32%  -0.16    -19  -6.5  
random      trend  |  3476    34%  -0.13    -14  -5.6  |  1413    33%  -0.16    -20  -4.5  
random      zone   |  7794    34%  -0.13    -18  -8.3  |  2647    32%  -0.16    -17  -5.9  
random      both   |  2834    33%  -0.16    -19  -6.1  |  1125    32%  -0.18    -22  -4.6  

rsi_cross   none   |  8364    34%  -0.11    -16  -7.5  |  2895    35%  -0.10    -11  -4.0  
rsi_cross   trend  |  3056    33%  -0.15    -23  -6.2  |  1278    34%  -0.13    -15  -3.5  
rsi_cross   zone   |  7569    34%  -0.12    -17  -7.5  |  2613    35%  -0.10    -10  -3.7  
rsi_cross   both   |  2820    33%  -0.15    -23  -6.1  |  1176    34%  -0.13    -15  -3.4  

bos         none   |  8103    34%  -0.10    -16  -6.2  |  2762    34%  -0.11    -19  -4.2  
bos         trend  |  2890    34%  -0.12    -17  -4.6  |  1214    34%  -0.11    -19  -2.8  
bos         zone   |  6066    34%  -0.11    -16  -6.1  |  2052    34%  -0.10    -14  -3.1  
bos         both   |  2144    34%  -0.14    -19  -4.6  |   869    35%  -0.10    -14  -2.0  

sweep       none   |  8389    33%  -0.14    -19  -9.1  |  2818    34%  -0.12    -18  -4.7  
sweep       trend  |  3090    32%  -0.18    -24  -7.1  |  1220    34%  -0.13    -21  -3.2  
sweep       zone   |  7484    33%  -0.14    -20  -9.0  |  2512    34%  -0.13    -19  -4.7  
sweep       both   |  2772    32%  -0.18    -26  -6.9  |  1090    34%  -0.13    -21  -3.1  

engulf      none   | 13588    33%  -0.14    -21 -11.6  |  4661    34%  -0.11    -19  -5.6  
engulf      trend  |  4926    33%  -0.15    -22  -7.7  |  2001    33%  -0.13    -18  -4.3  
engulf      zone   | 11311    33%  -0.14    -20 -10.5  |  3879    33%  -0.13    -20  -5.9  
engulf      both   |  4081    33%  -0.15    -21  -7.0  |  1632    33%  -0.14    -19  -4.1  

vol_thrust  none   |  6500    35%  -0.07     -7  -3.8  |  2428    34%  -0.10    -16  -3.5  
vol_thrust  trend  |  2368    33%  -0.13    -16  -4.6  |  1059    32%  -0.14    -20  -3.3  
vol_thrust  zone   |  5086    35%  -0.07     -7  -3.7  |  1862    33%  -0.13    -19  -3.9  
vol_thrust  both   |  1827    33%  -0.15    -18  -4.8  |   786    30%  -0.22    -36  -4.6  

sweep_bos   none   |  3201    34%  -0.12    -15  -4.8  |  1107    35%  -0.09     -8  -2.1  
sweep_bos   trend  |  1155    34%  -0.12    -14  -3.0  |   471    33%  -0.14    -25  -2.3  
sweep_bos   zone   |  2518    33%  -0.14    -17  -5.1  |   872    35%  -0.07     -1  -1.5  
sweep_bos   both   |   914    34%  -0.13    -12  -2.8  |   366    34%  -0.11    -15  -1.6  

```

## Does a filter help? Mean R, design + check pooled (n in brackets)

```
trigger                  none           trend            zone            both
random         -0.13 (12925)    -0.14 (4889)   -0.14 (10441)    -0.16 (3959)
rsi_cross      -0.11 (11259)    -0.15 (4334)   -0.11 (10182)    -0.15 (3996)
bos            -0.10 (10865)    -0.12 (4104)    -0.11 (8118)    -0.13 (3013)
sweep          -0.13 (11207)    -0.16 (4310)    -0.14 (9996)    -0.17 (3862)
engulf         -0.13 (18249)    -0.15 (6927)   -0.13 (15190)    -0.15 (5713)
vol_thrust      -0.08 (8928)    -0.13 (3427)    -0.09 (6948)    -0.17 (2613)
sweep_bos       -0.11 (4308)    -0.13 (1626)    -0.12 (3390)    -0.13 (1280)
```

## How the trades ended (no filter, design + check)

```
trigger       stop   target     time
random           65%     30%      5%
rsi_cross        64%     30%      6%
bos              64%     32%      4%
sweep            65%     30%      5%
engulf           65%     30%      5%
vol_thrust       64%     32%      4%
sweep_bos        64%     31%      4%
```
