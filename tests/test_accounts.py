"""The mainnet sub / main accounts (arbbot/accounts.py): the dashboard's ticket and Trade button follow the active one,
the switch is saved, and an order meant for the other account is never sent."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from arbbot.accounts import Accounts
from arbbot.server import Dashboard


class Ticket:
    def __init__(self, name, balance):
        self.name, self.last_error = name, ""
        self.account = {"assets": [{"asset": "USDT", "walletBalance": str(balance), "availableBalance": str(balance)}]}
        self.risk, self.orders = [], []

    def _usdt(self, key):
        return float(self.account["assets"][0][key])

    def view(self):
        return {"venue": "mainnet", "who": self.name, "balance": self._usdt("walletBalance")}

    async def place_order(self, body):
        self.orders.append(body)
        return {"id": 1, "status": "new"}


class Trader:
    def __init__(self, name):
        self.name, self.taken = name, []

    def view(self):
        return {"who": self.name, "manual_only": True}

    async def take(self, sym, tf, plan=None):
        self.taken.append(sym)
        return {"symbol": sym, "status": "open"}


class AccountsTests(unittest.TestCase):
    def test_active_is_saved_and_checked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "active.json"
            a = Accounts(p)
            a.add("sub", "SUB", Ticket("sub", 10))
            a.add("main", "MAIN", Ticket("main", 50))
            self.assertEqual((a.active, a.ticket.name), ("sub", "sub"))      # the first one until switched
            a.switch("main")
            self.assertEqual(a.ticket.name, "main")
            a.check("main")
            a.check(None)                                                    # an order that names no account
            with self.assertRaisesRegex(ValueError, "nothing was sent"):
                a.check("sub")
            with self.assertRaises(ValueError):
                a.switch("other")
            b = Accounts(p)                                                  # after a restart
            b.add("sub", "SUB", Ticket("sub", 10))
            b.add("main", "MAIN", Ticket("main", 50))
            self.assertEqual(b.active, "main")
            rows = {r["name"]: r for r in b.view()["accounts"]}
            self.assertEqual((rows["sub"]["balance"], rows["main"]["balance"]), (10.0, 50.0))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sub, self.main = Ticket("sub", 10), Ticket("main", 50)
        self.sub_tr, self.main_tr = Trader("sub"), Trader("main")
        self.acc = Accounts(Path(self.tmp.name) / "a.json")
        self.acc.add("sub", "SUB", self.sub, self.sub_tr)
        self.acc.add("main", "MAIN", self.main, self.main_tr)
        cfg = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=1), feed=SimpleNamespace(interval_s=1.0))
        self.dash = Dashboard(SimpleNamespace(swing=None), cfg, Path("web"), paper=self.sub, signal_trader=self.sub_tr,
                              accounts=self.acc)
        server = TestServer(self.dash.make_app())
        await server.start_server()
        self.dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
        self.client = TestClient(server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def post(self, path, body, token=True):
        return self.client.post(path, json=body, headers={"X-Token": self.dash.token} if token else {})

    async def test_switch_moves_the_ticket_and_the_trade_button(self):
        self.assertEqual((await (await self.client.get("/api/paper/state")).json())["who"], "sub")
        self.assertEqual((await self.post("/api/accounts", {"active": "main"}, token=False)).status, 403)
        r = await self.post("/api/accounts", {"active": "main"})
        self.assertEqual((await r.json())["active"], "main")
        self.assertEqual((await (await self.client.get("/api/paper/state")).json())["who"], "main")
        self.assertEqual((await (await self.client.get("/api/signal-trader")).json())["who"], "main")
        r = await self.post("/api/signal-trader", {"take": "SOLUSDT", "account": "main"})
        self.assertEqual(r.status, 200)
        self.assertEqual((self.main_tr.taken, self.sub_tr.taken), (["SOLUSDT"], []))
        r = await self.post("/api/paper/order", {"symbol": "SOLUSDT", "side": "buy", "type": "market", "account": "main"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.main.orders, [{"symbol": "SOLUSDT", "side": "buy", "type": "market"}])

    async def test_an_order_for_the_other_account_is_never_sent(self):
        r = await self.post("/api/signal-trader", {"take": "SOLUSDT", "account": "main"})   # the page still showed MAIN
        self.assertEqual(r.status, 409)
        self.assertIn("nothing was sent", (await r.json())["error"])
        r = await self.post("/api/paper/order", {"symbol": "SOLUSDT", "side": "buy", "type": "market", "account": "main"})
        self.assertEqual(r.status, 409)
        self.assertEqual((self.sub.orders, self.main.orders, self.sub_tr.taken), ([], [], []))
        v = await (await self.client.get("/api/accounts")).json()
        self.assertEqual([a["label"] for a in v["accounts"]], ["SUB", "MAIN"])


if __name__ == "__main__":
    unittest.main()
