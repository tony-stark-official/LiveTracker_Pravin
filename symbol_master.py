"""
symbol_master.py — resolves ISIN to a Fyers ticker string.

Two-layer lookup:
  1. ISIN → Fyers ticker  (from Fyers public symbol master CSVs)
  2. NSE/BSE symbol → ISIN  (from Trendlyne CSV via trendlyne_manager)

Fyers master is downloaded once at startup and kept in memory.
Priority order: NSE CM → BSE CM → NSE F&O → BSE F&O
"""

# ─── Logging ──────────────────────────────────────────────────────────────────
import logging
import csv
import io
import threading
import time

import requests

from LiveTrader import trendlyne_manager

log = logging.getLogger(__name__)

# ─── Fyers public symbol master URLs ─────────────────────────────────────────
_FYERS_MASTER_URLS = [
    ("NSE Capital Market", "https://public.fyers.in/sym_details/NSE_CM.csv"),
    ("BSE Capital Market", "https://public.fyers.in/sym_details/BSE_CM.csv"),
    ("NSE F&O",            "https://public.fyers.in/sym_details/NSE_FO.csv"),
    ("BSE F&O",            "https://public.fyers.in/sym_details/BSE_FO.csv"),
]

# Column indices in Fyers master CSV (0-based)
_COL_ISIN   = 5
_COL_TICKER = 9

_lock           = threading.Lock()
_fyers_by_isin: dict[str, str] = {}


def preload(max_retries: int = 3) -> None:
    """Download all Fyers symbol master CSVs and build ISIN → ticker map.
    Call once at startup. Subsequent calls are no-ops if already loaded.
    """
    with _lock:
        if _fyers_by_isin:
            log.info("Fyers symbol master already loaded (%d ISINs)", len(_fyers_by_isin))
            return

    tmp: dict[str, str] = {}

    for label, url in _FYERS_MASTER_URLS:
        resp = None
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                break
            except Exception as exc:
                log.warning("%s — attempt %d/%d failed: %s", label, attempt + 1, max_retries, exc)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
        else:
            log.error("Skipping %s after %d failures", label, max_retries)
            continue

        reader = csv.reader(io.StringIO(resp.text))
        next(reader, None)  # skip header
        count = 0
        for row in reader:
            if len(row) > _COL_TICKER:
                isin   = row[_COL_ISIN].strip()
                ticker = row[_COL_TICKER].strip()
                if isin and ticker and isin not in tmp:
                    tmp[isin] = ticker
                    count += 1
        log.info("%-25s %d symbols", label, count)

    with _lock:
        _fyers_by_isin.update(tmp)

    log.info("Fyers symbol master ready: %d total ISINs", len(_fyers_by_isin))


def isin_to_fyers(isin: str) -> str | None:
    """Return Fyers ticker (e.g. 'NSE:SBIN-EQ') for a given ISIN, or None."""
    with _lock:
        return _fyers_by_isin.get(isin.strip())


def symbol_to_isin(symbol: str) -> str | None:
    """Resolve NSE/BSE symbol code → ISIN via Trendlyne CSV."""
    row = trendlyne_manager.get_by_symbol(symbol)
    if row:
        return row.get("ISIN", "").strip() or None
    return None


def resolve(
    isin: str | None = None,
    symbol: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Returns (resolved_isin, fyers_ticker).
    Accepts ISIN directly (fastest) or falls back to symbol lookup.
    Either or both can be None on failure.
    """
    resolved = isin.strip() if isin else None

    if not resolved and symbol:
        resolved = symbol_to_isin(symbol)

    if not resolved:
        return None, None

    ticker = isin_to_fyers(resolved)
    return resolved, ticker
