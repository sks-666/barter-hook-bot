import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from backfill_fee_split import backfill  # noqa: E402

from arbbot.portfolio import Store  # noqa: E402


class BackfillTests(unittest.TestCase):
    def test_estimates_old_rows_only_and_the_bot_still_loads_them(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            s = Store(path)
            base = dict(opened_at=1.0, closed_at=2.0, notional=200.0, pnl_usd=1.0, pnl_bps=50.0, entry_basis_bps=1.0,
                        exit_basis_bps=0.0, funding_usd=0.0, reason="converged")
            s.db.execute("INSERT INTO trades(closed_at, symbol, pnl_usd, json) VALUES (2, 'OLDUSDT', 1, ?)",
                         (json.dumps(dict(base, symbol="OLDUSDT", fees_usd=0.6, live=False)),))
            s.db.execute("INSERT INTO trades(closed_at, symbol, pnl_usd, json) VALUES (3, 'NEWUSDT', 1, ?)",
                         (json.dumps(dict(base, symbol="NEWUSDT", fees_usd=0.6, fee_entry_usd=0.3, fee_exit_usd=0.3,
                                          fee_spot_usd=0.4, fee_fut_usd=0.2)),))
            rows = backfill(str(path), 10.0, 5.0)
            self.assertEqual([r[0] for r in rows], ["OLDUSDT"])
            self.assertEqual(backfill(str(path), 10.0, 5.0), [])                     # a second run changes nothing
            old, new = Store(path).recent_trades(10)
            self.assertAlmostEqual(old.fee_spot_usd, 0.4)
            self.assertAlmostEqual(old.fee_fut_usd, 0.2)
            self.assertAlmostEqual(old.fee_entry_usd + old.fee_exit_usd, 0.6)
            self.assertTrue(old.fee_estimated)
            self.assertFalse(new.fee_estimated)


if __name__ == "__main__":
    unittest.main()
