# Step 2: zones vs random levels

Data: BTC, ETH, SOL, BNB, XRP, DOGE, USD-M perpetual 4h candles 2021-11-11 to 2026-11-09; trend label = rule T2 on daily candles. A touch is followed for up to 30 candles (5 days): does price go +1 ATR in the zone's favour (up for a support, down for a resistance) before -1 ATR against it? Cells: share of those that went the zone's way, and the number of touches. A random price level gives the baseline. No fees: this measures the reaction, not a trade.

## First touch vs random levels of the same size and distance

```
zone        period  zone: bounce%   n   random: bounce%   n     diff      z   neither move
swing_sr    design   45.9%  2631         48.9%  2656           -3.0p   -2.2   0%
swing_sr    check    44.4%   789         48.1%   909           -3.7p   -1.5   0%
range20     design   46.6%  1104         50.2%  2587           -3.5p   -2.0   0%
range20     check    43.2%   352         45.0%   863           -1.8p   -0.6   0%
range55     design   47.1%   524         46.8%  2510           +0.4p    0.2   0%
range55     check    46.6%   208         51.6%   866           -5.0p   -1.3   0%
order_block design   47.7%   828         45.4%  2944           +2.3p    1.2   0%
order_block check    44.3%   282         45.2%   982           -0.9p   -0.3   0%
fvg         design   50.8%  2677         47.2%  2830           +3.6p    2.7   0%
fvg         check    51.3%   972         46.1%  1016           +5.3p    2.4   0%
ma50        design   49.1%  2337         46.5%  3102           +2.6p    1.9   0%
ma50        check    50.0%   780         47.5%  1017           +2.5p    1.1   0%
ma200       design   49.0%  1203         47.3%  3034           +1.6p    1.0   0%
ma200       check    54.5%   409         46.0%  1030           +8.5p    2.9   0%
```

## Stricter baseline: random levels born on the same candle as each real zone (static zones, first touches)

Same moment, same width, random distance from price. This removes 'the zone appeared right after a rally' as an explanation.

```
zone        period  zone: bounce%   n   same-time random        diff      z
swing_sr    design   45.9%  2631         46.7%  8574           -0.8p   -0.7
swing_sr    check    44.4%   789         46.4%  2612           -2.1p   -1.0
order_block design   47.7%   828         48.2%  2601           -0.5p   -0.3
order_block check    44.3%   282         53.5%   906           -9.2p   -2.7
fvg         design   50.8%  2677         50.2%  7449           +0.6p    0.6
fvg         check    51.3%   972         49.2%  2768           +2.2p    1.2
```

## Does a later touch hold as well as the first? (design + check)

```
zone        1st touch        2nd touch        3rd+ touch
swing_sr     45.6%  3420      46.8%  1698      43.5%  1558
range20      45.8%  1456          –     0          –     0
range55      47.0%   732          –     0          –     0
order_block  46.8%  1110      47.5%   610      49.6%   722
fvg          51.0%  3649      48.1%  2077      46.3%  2544
ma50         49.3%  3117          –     0          –     0
ma200        50.4%  1612          –     0          –     0
```

## Does the daily trend change it? First touches, design + check

with = a support in an up-trend or a resistance in a down-trend (T2). random = the baseline above, all periods pooled.

```
zone        with trend       against trend    daily range      random level
swing_sr     46.4%  1389      45.8%  1284      41.6%   329      48.7%  3565
range20      43.9%   492      45.8%   649      47.5%   139      48.9%  3450
range55      45.7%   164      46.5%   411      50.7%    71      48.0%  3376
order_block  50.1%   439      46.3%   419      43.0%   100      45.3%  3926
fvg          50.6%  1426      52.5%  1336      50.9%   332      46.9%  3846
ma50         47.3%  1213      51.8%  1167      47.3%   315      46.7%  4119
ma200        50.8%   725      52.6%   587      42.9%   140      47.0%  4064
```

## How many zones and touches there were

```
zone         first touches  later touches
swing_sr            3423           3258
range20             1456              0
range55              732              0
order_block         1111           1336
fvg                 3653           4626
ma50                3121              0
ma200               1614              0
```

z: two-proportion z-test of zone vs random, first touches. Touches of nearby zones of different types overlap in time, and the six coins move together, so treat |z| under about 3 as not distinguishable from noise.
