# Step 3: entry triggers after fees, alone and with filters

Data: BTC, ETH, SOL, BNB, XRP, DOGE, USD-M perpetual 1h candles 2021-11-11 to 2026-11-10, long and short. Every trade: enter at the next open, stop 2.5 ATR(1h) away, target 1.5R, exit after 24 h, taker fee 5 bps + slippage 3 bps per fill (no slippage on the target), funding 1 bp per 8 h. R = result in units of the initial risk after all costs; bps = net result per trade; t = mean R over its standard error (trades of different coins overlap in time, so treat |t| under 3 as noise). `random` = entries at random candles under the same filter and exits: the baseline.

```
trigger     filter | design n   win%      R   bps     t | check  n   win%      R   bps     t 
random      none   |  8954    42%  -0.09    -22  -8.4  |  3011    43%  -0.09    -18  -4.9  
random      trend  |  3247    42%  -0.10    -22  -5.9  |  1299    42%  -0.13    -29  -4.6  
random      zone   |  7267    43%  -0.09    -24  -8.1  |  2428    42%  -0.10    -20  -5.1  
random      both   |  2630    42%  -0.11    -26  -5.9  |  1043    42%  -0.15    -34  -5.0  

rsi_cross   none   |  7117    45%  -0.07    -15  -6.3  |  2430    44%  -0.09    -12  -4.3  
rsi_cross   trend  |  2618    43%  -0.12    -28  -6.3  |  1069    44%  -0.12    -19  -3.9  
rsi_cross   zone   |  6472    45%  -0.08    -18  -6.7  |  2200    44%  -0.09    -14  -4.2  
rsi_cross   both   |  2432    43%  -0.13    -31  -6.5  |   986    44%  -0.13    -23  -4.1  

bos         none   |  7497    42%  -0.05    -14  -4.5  |  2576    42%  -0.07    -23  -3.7  
bos         trend  |  2714    41%  -0.09    -21  -4.5  |  1144    42%  -0.09    -24  -3.1  
bos         zone   |  5665    43%  -0.05    -12  -3.9  |  1940    42%  -0.08    -25  -3.7  
bos         both   |  2051    41%  -0.10    -23  -4.4  |   832    42%  -0.10    -24  -2.9  

sweep       none   |  7804    43%  -0.09    -21  -8.4  |  2607    44%  -0.07    -15  -3.3  
sweep       trend  |  2873    43%  -0.11    -23  -5.9  |  1140    44%  -0.07    -19  -2.4  
sweep       zone   |  6947    43%  -0.10    -22  -8.4  |  2327    44%  -0.07    -15  -3.5  
sweep       both   |  2570    43%  -0.11    -24  -5.5  |  1023    43%  -0.08    -18  -2.6  

engulf      none   | 12408    43%  -0.08    -18  -8.9  |  4188    44%  -0.07    -18  -4.4  
engulf      trend  |  4527    42%  -0.09    -21  -6.1  |  1812    44%  -0.08    -19  -3.6  
engulf      zone   | 10452    43%  -0.08    -16  -8.1  |  3496    43%  -0.08    -19  -4.8  
engulf      both   |  3798    43%  -0.09    -19  -5.5  |  1499    43%  -0.10    -21  -3.7  

vol_thrust  none   |  6123    43%  -0.06    -11  -4.5  |  2254    43%  -0.07    -16  -3.0  
vol_thrust  trend  |  2244    42%  -0.10    -20  -4.6  |   983    42%  -0.11    -21  -3.2  
vol_thrust  zone   |  4869    43%  -0.07    -12  -4.6  |  1760    42%  -0.10    -25  -4.2  
vol_thrust  both   |  1775    42%  -0.12    -24  -4.9  |   749    39%  -0.18    -47  -4.9  

sweep_bos   none   |  3158    42%  -0.06    -14  -3.5  |  1098    43%  -0.06    -11  -2.0  
sweep_bos   trend  |  1151    40%  -0.09    -17  -2.9  |   468    44%  -0.08    -22  -1.7  
sweep_bos   zone   |  2479    42%  -0.07    -11  -3.3  |   871    44%  -0.05     -7  -1.5  
sweep_bos   both   |   907    40%  -0.09    -12  -2.6  |   367    46%  -0.06    -13  -1.2  

```

## Does a filter help? Mean R, design + check pooled (n in brackets)

```
trigger                  none           trend            zone            both
random         -0.09 (11965)    -0.11 (4546)    -0.10 (9695)    -0.12 (3673)
rsi_cross       -0.08 (9547)    -0.12 (3687)    -0.08 (8672)    -0.13 (3418)
bos            -0.06 (10073)    -0.09 (3858)    -0.06 (7605)    -0.10 (2883)
sweep          -0.09 (10411)    -0.10 (4013)    -0.09 (9274)    -0.10 (3593)
engulf         -0.08 (16596)    -0.09 (6339)   -0.08 (13948)    -0.09 (5297)
vol_thrust      -0.06 (8377)    -0.10 (3227)    -0.08 (6629)    -0.14 (2524)
sweep_bos       -0.06 (4256)    -0.09 (1619)    -0.06 (3350)    -0.08 (1274)
```

## How the trades ended (no filter, design + check)

```
trigger       stop   target     time
random           41%     21%     38%
rsi_cross        40%     19%     41%
bos              40%     25%     35%
sweep            41%     21%     38%
engulf           41%     22%     37%
vol_thrust       43%     26%     31%
sweep_bos        40%     25%     35%
```
