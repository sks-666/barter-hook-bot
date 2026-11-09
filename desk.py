"""Launcher that works from any directory:  python /path/to/binance-arb-bot/desk.py research|status|ladder|graveyard ..."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskkit.cli import main  # noqa: E402

sys.exit(main())
