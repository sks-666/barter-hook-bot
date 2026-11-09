"""deskkit: the research gates and promotion ladder of the one-person desk protocol, run on the swingkit strategies.

  stats      deflated Sharpe, drift, losing-streak limit, Kelly, consistency test (the paper's equations)
  rules      the desk rules: thresholds and windows, from desk_rules.toml
  graveyard  every configuration ever tested, so the number of trials is counted honestly
  research   walk-forward windows, noise and plateau tests, capacity, the gates
  ladder     paper -> live promotion rungs, health checks (streak, drift), retirement
Nothing here places an order; it only reads candles and journals and writes data/desk/*.
"""
