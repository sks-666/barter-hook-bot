# Step 1: trend labels vs forward returns

Data: BTC, ETH, SOL, BNB, XRP, DOGE, USD-M perpetual daily candles 2021-11-12 to 2026-11-09. Forward return = next open to close h days later, no fees (this step only names the trend). Cells: mean return and number of days.

## T1  ·  flips/year 13.1  ·  mean run 30 days

**design (first 60%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.97%  5370     +0.96%  2645     +0.97%  2725          –     0    -0.01%     0.9     0.6
10    +2.07%  5370     +2.07%  2645     +2.07%  2725          –     0    -0.00%     0.6    -0.0
20    +4.74%  5370     +4.99%  2645     +4.49%  2725          –     0    +0.50%     0.6     0.3
```

**check (next 20%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.58%  2190     +0.46%  1602     +0.91%   588          –     0    -0.45%    -1.5    -0.6
10    +0.92%  2190     +0.70%  1602     +1.52%   588          –     0    -0.81%     n/a    -0.9
20    +1.28%  2190     +0.67%  1602     +2.95%   588          –     0    -2.28%     n/a    -0.8
```

## T2  ·  flips/year 6.8  ·  mean run 55 days

**design (first 60%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +1.07%  5310     +1.14%  2350     +1.10%  2381     +0.63%   579    +0.04%     0.8     0.1
10    +2.34%  5310     +2.56%  2350     +2.48%  2381     +0.88%   579    +0.08%     1.2     0.3
20    +5.05%  5310     +5.36%  2350     +5.22%  2381     +3.07%   579    +0.13%     0.8     0.3
```

**check (next 20%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.58%  2190     +0.50%  1511     +2.03%   396     -1.00%   283    -1.53%     n/a    -1.4
10    +0.92%  2190     +0.68%  1511     +4.04%   396     -2.14%   283    -3.37%     n/a    -0.9
20    +1.28%  2190     +0.36%  1511     +8.72%   396     -4.21%   283    -8.36%     n/a    -0.9
```

## T3  ·  flips/year 25.0  ·  mean run 15 days

**design (first 60%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.56%  6289     +1.03%  1878     +0.10%  1885     +0.54%  2526    +0.93%    -0.4     2.3
10    +1.24%  6289     +2.43%  1878     +0.52%  1885     +0.89%  2526    +1.92%    -0.7     0.9
20    +2.80%  6289     +3.55%  1878     +1.13%  1885     +3.49%  2526    +2.42%    -1.3    -0.2
```

**check (next 20%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.58%  2190     +0.48%   715     +0.40%   675     +0.82%   800    +0.08%    -0.6     0.1
10    +0.92%  2190     +0.47%   715     +0.72%   675     +1.49%   800    -0.25%    -0.1     0.4
20    +1.28%  2190     -0.56%   715     +2.03%   675     +2.29%   800    -2.59%     n/a    -1.1
```

## T4  ·  flips/year 8.7  ·  mean run 42 days

**design (first 60%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +1.07%  5310     +1.02%  2136     +1.27%  2160     +0.74%  1014    -0.25%     1.0     0.1
10    +2.34%  5310     +2.38%  2136     +2.94%  2160     +0.97%  1014    -0.57%     1.3    -0.0
20    +5.05%  5310     +5.45%  2136     +6.02%  2160     +2.11%  1014    -0.57%     1.0    -0.1
```

**check (next 20%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.58%  2190     +0.41%  1363     +1.47%   368     +0.38%   459    -1.06%     n/a    -0.8
10    +0.92%  2190     +0.65%  1363     +3.28%   368     -0.17%   459    -2.64%     n/a    -0.7
20    +1.28%  2190     +0.33%  1363     +7.63%   368     -0.98%   459    -7.30%     n/a    -1.4
```

## T5  ·  flips/year 16.5  ·  mean run 22 days

**design (first 60%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +1.07%  5310     +1.30%  1527     +1.25%  1585     +0.77%  2198    +0.04%     1.2     0.1
10    +2.34%  5310     +2.84%  1527     +2.87%  1585     +1.61%  2198    -0.03%     1.4    -0.0
20    +5.05%  5310     +5.28%  1527     +5.39%  1585     +4.64%  2198    -0.12%     1.5     0.4
```

**check (next 20%)**

```
h      all days            up                down              range         up−down   t(btc)  t(pool)
5     +0.58%  2190     +0.85%  1038     +0.78%   264     +0.21%   888    +0.07%     n/a    -0.3
10    +0.92%  2190     +0.99%  1038     +1.62%   264     +0.63%   888    -0.62%     n/a     1.0
20    +1.28%  2190     +0.37%  1038     +3.95%   264     +1.56%   888    -3.58%     n/a     0.5
```

t(btc): up vs down, BTC only, non-overlapping samples (n/a when fewer than 5 of either). t(pool): the same for all six coins pooled; the coins move together, so treat it as an overstatement. |t| under 2 means the difference is not distinguishable from noise.
