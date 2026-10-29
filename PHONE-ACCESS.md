# Open the testnet dashboard on your phone

The testnet bot's dashboard (port 8767) also opens on a phone or tablet that is on the **same Wi-Fi** as this laptop.
The paper bot (port 8765) only opens on this laptop.

## What was checked on this laptop

- The testnet dashboard (port 8767) accepts connections from other devices on the network.
- This laptop's Wi-Fi address was `192.168.1.77` when this was written.
- Opening the address without the key shows "This dashboard is locked" (HTTP 401). That is expected: only links
  with the key get in.

## Get your phone link

The link includes a private key, so it is not written in this file. Print it from the project folder:

```bash
grep -h phone: logs/testnet-bot.out
```

It looks like `http://192.168.1.77:8767/?key=<your key>`.

## On your phone

1. Connect the phone to the **same Wi-Fi** as this laptop. Mobile data will not work.
2. Open the whole link (with `?key=...`) in Safari or Chrome. You should see the dashboard with the
   **LIVE · TESTNET** badge. If it says "This dashboard is locked", part of the key was cut off; copy the whole link
   again.
3. Bookmark it. The phone remembers the key for 180 days, so afterwards `http://192.168.1.77:8767` alone works.

## If it does not load

- **Mobile data:** the phone must be on the same Wi-Fi, not mobile data.
- **VPN:** this laptop also has the address `10.5.0.2`, which is what NordVPN uses. VPNs often block local-network
  traffic. Turn on "allow LAN / local network" in the VPN settings, or turn the VPN off. Check the phone for a VPN too.
- **Firewall:** if Ubuntu's firewall (ufw) is on, allow the port:

  ```bash
  sudo ufw allow 8767/tcp
  ```

- **Address changed:** after a router restart the laptop's address can change. Check it, then use the new address
  in the link:

  ```bash
  hostname -I
  ```

## Away from home

It does not work on mobile data or another Wi-Fi. For that, use a private VPN such as Tailscale, or the read-only
hosted dashboard in `vercel-dashboard/`. Do not open the port to the internet.

## Keep the key private

Anyone with the link who is on your Wi-Fi can control the testnet dashboard (pause, flatten, place testnet orders).
Do not share it. To make a new key (the old link and bookmarks stop working):

```bash
rm data/lan-key-8767.txt && ./stop-bots.sh
```

```bash
./start-bots.sh
```

Answer **N** to the mainnet question, then print the new link with the `grep` command above.
