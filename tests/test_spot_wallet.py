"""The spot wallet shown beside the futures balance (arbbot/testnet_ticket.spot_wallet): every coin valued in USDT."""
import unittest

from arbbot.testnet_ticket import spot_wallet


class SpotWallet(unittest.TestCase):
    def test_values_coins_in_usdt(self):
        bal = [{"asset": "USDT", "free": "100", "locked": "5"}, {"asset": "BTC", "free": "0.01", "locked": "0"},
               {"asset": "LDETH", "free": "1", "locked": "0"}, {"asset": "USDC", "free": "20", "locked": "0"},
               {"asset": "SHIB", "free": "0.0001", "locked": "0"}, {"asset": "NOPE", "free": "3", "locked": "0"},
               {"asset": "ZERO", "free": "0", "locked": "0"}]
        prices = [{"symbol": "BTCUSDT", "price": "50000"}, {"symbol": "ETHUSDT", "price": "3000"},
                  {"symbol": "SHIBUSDT", "price": "0.00001"}, {"symbol": "ETHBTC", "price": "0.06"}]
        w = spot_wallet(bal, prices)
        self.assertAlmostEqual(w["total_usd"], 105 + 500 + 3000 + 20, places=4)
        self.assertEqual(w["usdt"], 105)
        names = [a["asset"] for a in w["assets"]]
        self.assertEqual(names[:4], ["LDETH", "BTC", "USDT", "USDC"])      # biggest first, Simple Earn at ETH's price
        self.assertNotIn("SHIB", names)                                      # dust left out of the list
        self.assertNotIn("ZERO", names)
        self.assertIsNone(next(a for a in w["assets"] if a["asset"] == "NOPE")["usd"])   # no price: listed, not counted

    def test_empty(self):
        self.assertEqual(spot_wallet([], []), {"total_usd": 0.0, "usdt": 0.0, "assets": []})


class Liquidation(unittest.TestCase):
    def test_binance_isolated_formula(self):
        from arbbot.testnet_ticket import liquidation_price
        tiers = [[50_000, 0.004, 0, 125], [250_000, 0.005, 50, 100]]
        # BTC long $1,000 at 10x from 100,000: margin 100, 0.4% maintenance -> ~90,361 (Binance's calculator)
        self.assertAlmostEqual(liquidation_price(True, 100_000, 1_000, 10, tiers), 90_361.45, places=1)
        self.assertAlmostEqual(liquidation_price(False, 100_000, 1_000, 10, tiers), 109_561.75, places=1)
        # a bigger position falls in the next tier (rate 0.5%, amount 50)
        q, m = 1.0, 100_000 / 10
        self.assertAlmostEqual(liquidation_price(True, 100_000, 100_000, 10, tiers), (m + 50 - q * 100_000) / (q * 0.005 - q))
        # unknown tiers: a cautious 1%; 1x long never liquidates above 0
        self.assertAlmostEqual(liquidation_price(True, 100, 20, 3), 100 * (1 - 1 / 3) / 0.99)
        self.assertEqual(liquidation_price(True, 100, 20, 1, [[1e9, 0.0, 0, 1]]), 0.0)


if __name__ == "__main__":
    unittest.main()
