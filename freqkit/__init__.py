"""freqkit: the Freqtrade trading subsystem of binance-arb-bot.

Freqtrade (https://github.com/freqtrade/freqtrade) runs as its own process (a venv binary or the official Docker image)
and is driven only through its documented REST API. This package holds the client (client.py), the settings and event
store (store.py), the process manager with the dry-run / live safety rules (manager.py) and the dashboard routes
(routes.py). Nothing here imports the freqtrade Python package: the bot's own venv stays as small as it was.
"""
__version__ = "1.0.0"
