"""Command line for the Freqtrade processes the dashboard manages (they outlive the dashboard on purpose).

    .venv/bin/python -m freqkit status            which processes are running (from their pid files) and answering
    .venv/bin/python -m freqkit stop [trade|webserver|all]   stop them (default: all)

Reads the same config.toml section as the dashboard: --config picks the file (default config.paper.toml).
"""
import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arbbot.config import load  # noqa: E402
from arbbot.main import load_dotenv  # noqa: E402
from freqkit.manager import KINDS, FreqtradeManager  # noqa: E402
from freqkit.store import FreqStore  # noqa: E402


async def amain(args) -> int:
    load_dotenv(ROOT / ".env")
    cfg = load(args.config)
    async with aiohttp.ClientSession() as session:
        fm = FreqtradeManager(ROOT, cfg.freqtrade, FreqStore(ROOT / "data" / "freqtrade.db"), session)
        for proc in fm.procs.values():
            await fm._adopt(proc)
        if args.command == "status":
            print(f"runner: {fm.runner.name} · mode: {fm.store.mode()} · user_data: {fm.user_data}")
            for proc in fm.procs.values():
                v = proc.view()
                who = v["pid"] or v["service"] or "-"
                print(f"  {proc.kind:10s} {proc.state:8s} {who!s:8} port {proc.port}"
                      + (f" · API {'ok' if await proc.api.ping() else 'not answering'}" if proc.state != "stopped" else ""))
            return 0
        kinds = KINDS if args.what == "all" else (args.what,)
        for kind in kinds:
            proc = fm.procs[kind]
            if proc.state == "stopped":
                print(f"{kind}: not running")
                continue
            await fm._stop(proc)
            print(f"{kind}: stopped")
        return 0


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m freqkit", description=__doc__.splitlines()[0])
    p.add_argument("--config", default=str(ROOT / "config.paper.toml"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    stop = sub.add_parser("stop")
    stop.add_argument("what", nargs="?", default="all", choices=("all", *KINDS))
    sys.exit(asyncio.run(amain(p.parse_args(argv))))


if __name__ == "__main__":
    main()
