"""
main.py — LiveTrader entry point.

Startup sequence:
  1. Load Trendlyne CSV + start 2-hour reload thread
  2. Download Fyers symbol master (ISIN → ticker)
  3. Init SQLite database
  4. Connect Fyers WebSocket (no symbols yet — safe to start empty at 08:30)
  5. Start HTTP server on NOTIFY_PORT to receive notifications
  6. Background thread force-exits all trades at 15:25 IST (market close)

Notification endpoint:
  POST http://localhost:8765/notify
  Content-Type: application/json

  {
    "company_name":   "Infosys Ltd",
    "symbol":         "INFY",         // NSE or BSE ticker (optional if isin given)
    "isin":           "INE009A01021",  // ISIN (preferred — fastest lookup)
    "date":           "2026-04-17",    // YYYY-MM-DD — skip if not today
    "order_type":     "BULK_DEAL",     // free-text from your notification system
    "order_value_cr": 45.0             // optional: block order ₹ Crore
  }

Response: 200 {"status": "ok"} or 4xx on bad input.

Usage:
  cd "Praving AlgoTrading"
  python -m LiveTrader.main
"""

# ─── Logging ──────────────────────────────────────────────────────────────────
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from LiveTrader import database, symbol_master, trendlyne_manager
from LiveTrader.config import FYERS_LOG_PATH, NOTIFY_HOST, NOTIFY_PORT
from LiveTrader.tracker import LiveTracker
from LiveTrader import telegram_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Force-exit scheduler ─────────────────────────────────────────────────────

def _market_close_watcher(tracker: LiveTracker) -> None:
    """Background thread: force-exits all entered trades at 15:25 IST."""
    CLOSE_HOUR, CLOSE_MIN = 15, 25
    triggered = False
    while True:
        now = datetime.now(IST)
        if now.hour > CLOSE_HOUR or (now.hour == CLOSE_HOUR and now.minute >= CLOSE_MIN):
            if not triggered:
                log.info("─── Market close: force-exiting all active trades ───")
                triggered = True
                tracker.force_exit_all("MARKET_CLOSE")
                summary = tracker.get_status_summary()
                telegram_manager.send_system(
                    f"Market closed (15:25 IST).\n"
                    f"Trades today → entered: {summary['entered']}  "
                    f"exited: {summary['exited']}  "
                    f"expired: {summary['waiting']}  skip: {summary['skip']}"
                )
        else:
            triggered = False  # reset for next day if process stays up overnight
        time.sleep(30)


# ─── HTTP notification receiver ───────────────────────────────────────────────

def _make_handler(tracker: LiveTracker):
    class NotificationHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/notify":
                self.send_error(404, "Use POST /notify")
                return

            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self.send_error(400, "Empty body")
                return

            try:
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.send_error(400, f"Invalid JSON: {exc}")
                return

            if "company_name" not in data:
                self.send_error(422, "Missing required field: company_name")
                return

            # Process in background thread so HTTP response is instant
            threading.Thread(
                target=_safe_add_stock,
                args=(tracker, data),
                daemon=True,
            ).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

        def do_GET(self):
            """Health check + today's trade summary."""
            if self.path.rstrip("/") == "/status":
                summary = tracker.get_status_summary()
                today   = datetime.now(IST).strftime("%Y-%m-%d")
                trades  = database.get_today_trades(today)
                body    = json.dumps({
                    "status":  "running",
                    "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                    "trades_today": len(trades),
                    "live_summary": summary,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):  # suppress default access log spam
            log.debug("HTTP %s", fmt % args)

    return NotificationHandler


def _safe_add_stock(tracker: LiveTracker, data: dict) -> None:
    try:
        tracker.add_stock(data)
    except Exception as exc:
        log.exception("Unhandled error in add_stock for %s: %s", data.get("company_name"), exc)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # Ensure log directory exists
    Path(FYERS_LOG_PATH).mkdir(parents=True, exist_ok=True)

    log.info("══════════════════════════════════════════")
    log.info("  LiveTrader — Paper Trading Engine")
    log.info("══════════════════════════════════════════")

    # 1. Load Trendlyne CSV
    log.info("[1/4] Loading Trendlyne CSV ...")
    trendlyne_manager.load()
    trendlyne_manager.start_reload_thread()

    # 2. Load Fyers symbol master
    log.info("[2/4] Downloading Fyers symbol master ...")
    symbol_master.preload()

    # 3. Init database
    log.info("[3/4] Initialising database ...")
    database.init_db()

    # 4. Connect WebSocket
    log.info("[4/4] Connecting Fyers WebSocket ...")
    tracker = LiveTracker()
    tracker.start()

    # 5. Start market-close watcher
    watcher = threading.Thread(
        target=_market_close_watcher,
        args=(tracker,),
        daemon=True,
        name="market-close-watcher",
    )
    watcher.start()

    # 6. Announce readiness
    telegram_manager.send_system(
        f"LiveTrader started  {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
        f"Listening for notifications on port {NOTIFY_PORT}"
    )

    log.info("──────────────────────────────────────────")
    log.info("  Ready — listening on %s:%d", NOTIFY_HOST, NOTIFY_PORT)
    log.info("  POST /notify  to submit a new stock")
    log.info("  GET  /status  for health check")
    log.info("──────────────────────────────────────────")

    # 7. Start HTTP server (blocks until Ctrl+C)
    server = HTTPServer((NOTIFY_HOST, NOTIFY_PORT), _make_handler(tracker))

    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping ...")
        tracker.stop()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
        log.info("LiveTrader stopped.")


if __name__ == "__main__":
    main()
