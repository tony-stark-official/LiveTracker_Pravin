"""
config.py — all configurable settings for LiveTrader.

Copy .env.example to .env and fill in your values before running.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Fyers credentials ──────────────────────────────────────────────────────────
FYERS_CLIENT_ID    = os.environ.get("FYERS_CLIENT_ID", "")
FYERS_ACCESS_TOKEN = os.environ.get("FYERS_ACCESS_TOKEN", "")

# ── Trendlyne CSV ─────────────────────────────────────────────────────────────
# Override via env var TRENDLYNE_CSV_PATH to point to any path you like.
TRENDLYNE_CSV_PATH = os.environ.get(
    "TRENDLYNE_CSV_PATH",
    str(Path(__file__).parent.parent / "strategy" / "trendlyne_data.csv"),
)

# Reload interval in hours (background thread re-reads the CSV from disk)
TRENDLYNE_RELOAD_HOURS = int(os.environ.get("TRENDLYNE_RELOAD_HOURS", "2"))

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# ── HTTP notification receiver ────────────────────────────────────────────────
# Your upstream notification system POSTs to http://localhost:NOTIFY_PORT/notify
NOTIFY_HOST = os.environ.get("NOTIFY_HOST", "0.0.0.0")
NOTIFY_PORT = int(os.environ.get("NOTIFY_PORT", "8765"))

# ── Fyers logs ────────────────────────────────────────────────────────────────
FYERS_LOG_PATH = str(Path(__file__).parent / "logs")
