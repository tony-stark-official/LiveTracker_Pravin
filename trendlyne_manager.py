"""
trendlyne_manager.py — loads Trendlyne CSV into memory and refreshes every
TRENDLYNE_RELOAD_HOURS hours from the path set in config (or env var).
"""

# ─── Logging ──────────────────────────────────────────────────────────────────
import logging
import csv
import threading
import time
from pathlib import Path

from LiveTrader.config import TRENDLYNE_CSV_PATH, TRENDLYNE_RELOAD_HOURS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── In-memory store ──────────────────────────────────────────────────────────
_lock             = threading.RLock()
_by_nse:  dict[str, dict] = {}
_by_bse:  dict[str, dict] = {}
_by_isin: dict[str, dict] = {}


def _do_load() -> None:
    path = Path(TRENDLYNE_CSV_PATH)
    if not path.exists():
        log.error("Trendlyne CSV not found at: %s", path)
        return

    by_nse:  dict[str, dict] = {}
    by_bse:  dict[str, dict] = {}
    by_isin: dict[str, dict] = {}

    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                nse  = row.get("NSEcode", "").strip()
                bse  = row.get("BSEcode", "").strip()
                isin = row.get("ISIN", "").strip()
                if nse:
                    by_nse[nse.upper()] = row
                if bse:
                    by_bse[bse] = row
                if isin:
                    by_isin[isin] = row
    except Exception as exc:
        log.error("Failed to read Trendlyne CSV: %s", exc)
        return

    with _lock:
        _by_nse.clear();  _by_nse.update(by_nse)
        _by_bse.clear();  _by_bse.update(by_bse)
        _by_isin.clear(); _by_isin.update(by_isin)

    log.info(
        "Trendlyne CSV loaded: %d NSE / %d BSE / %d ISIN  ←  %s",
        len(by_nse), len(by_bse), len(by_isin), path,
    )


def load() -> None:
    """Blocking initial load. Call once at startup."""
    _do_load()


def get_by_symbol(symbol: str) -> dict | None:
    """Look up a row by NSE ticker (case-insensitive) or numeric BSE code."""
    sym = symbol.strip()
    with _lock:
        row = _by_nse.get(sym.upper())
        if not row and sym.isdigit():
            row = _by_bse.get(sym)
    return row


def get_by_isin(isin: str) -> dict | None:
    """Look up a row by ISIN."""
    with _lock:
        return _by_isin.get(isin.strip())


def _reload_loop() -> None:
    interval = TRENDLYNE_RELOAD_HOURS * 3600
    while True:
        time.sleep(interval)
        log.info("─── Trendlyne CSV: scheduled reload ───")
        _do_load()


def start_reload_thread() -> None:
    """Spawn a daemon thread that reloads the CSV every TRENDLYNE_RELOAD_HOURS."""
    t = threading.Thread(target=_reload_loop, daemon=True, name="trendlyne-reload")
    t.start()
    log.info("Trendlyne auto-reload every %d hour(s)", TRENDLYNE_RELOAD_HOURS)
