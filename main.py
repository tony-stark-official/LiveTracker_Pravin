"""
main.py — LiveTrader entry point.

Startup sequence:
  1. Load Trendlyne CSV + start 2-hour reload thread
  2. Download Fyers symbol master (ISIN → ticker)
  3. Init SQLite database
  4. Start HTTP server (immediately handles /token and /status)
  5. Block waiting for Windows worker to POST /token (Fyers access token)
  6. Once token arrives: init LiveTracker, connect Fyers WebSocket
  7. Background thread force-exits all trades at 15:25 IST (market close)

Windows worker pushes token at ~8:25 AM:
  POST http://<this_host>:NOTIFY_PORT/token
  Headers: X-Secret: <LIVETRADER_WORKER_SECRET>
  Body:    {"token": "eyJ...", "client_id": "FRMEOR7L6Y-100"}

Notification endpoint (active after token received):
  POST http://localhost:8765/notify
  Content-Type: application/json
  {
    "company_name":   "Infosys Ltd",
    "symbol":         "INFY",
    "isin":           "INE009A01021",
    "date":           "2026-04-17",
    "order_type":     "BULK_DEAL",
    "order_value_cr": 45.0
  }

Usage:
  cd <parent of LiveTrader/>
  python -m LiveTrader.main
"""

import hmac
import json
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from LiveTrader import database, symbol_master, trendlyne_manager
from LiveTrader.config import (
    FYERS_CLIENT_ID,
    FYERS_LOG_PATH,
    LIVETRADER_WORKER_SECRET,
    NOTIFY_HOST,
    NOTIFY_PORT,
)
from LiveTrader import telegram_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(Path(FYERS_LOG_PATH).parent / "livetrader.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Shared state between HTTP handler and main thread ─────────────────────────
_token_event   = threading.Event()   # set when /token push received
_received_token: dict = {}           # {"token": "...", "client_id": "..."}
_tracker       = None                # LiveTracker — set after token arrives
_tracker_lock  = threading.Lock()


# ── Force-exit scheduler ──────────────────────────────────────────────────────

def _market_close_watcher(tracker) -> None:
    """
    Background thread: force-exits all trades at 15:25 IST, disconnects WebSocket,
    sends Telegram summary, then exits the process cleanly.
    Next day the process is restarted by cron/systemd — fresh token, clean slate.
    """
    CLOSE_HOUR, CLOSE_MIN = 15, 25
    while True:
        now = datetime.now(IST)
        is_trading_day = now.weekday() < 5  # Mon–Fri only
        if is_trading_day and (now.hour > CLOSE_HOUR or (now.hour == CLOSE_HOUR and now.minute >= CLOSE_MIN)):
            log.info("─── 15:25 IST: closing all trades and disconnecting ───")
            tracker.force_exit_all("MARKET_CLOSE")
            summary = tracker.get_status_summary()
            telegram_manager.send_system(
                f"Market closed (15:25 IST).\n"
                f"Trades today → entered: {summary['entered']}  "
                f"exited: {summary['exited']}  "
                f"expired: {summary['waiting']}  skip: {summary['skip']}\n"
                f"Shutting down — will restart tomorrow at 08:00."
            )
            tracker.stop()
            log.info("WebSocket disconnected. Token cleared. Exiting for the day.")
            import os as _os
            _os.kill(_os.getpid(), signal.SIGTERM)
            return
        time.sleep(30)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_POST(self):
        path = self.path.rstrip("/")

        if path == "/token":
            self._handle_token()
        elif path == "/notify":
            self._handle_notify()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path.rstrip("/") == "/status":
            self._handle_status()
        else:
            self.send_error(404)

    # ── /token — Windows worker pushes Fyers access token ────────────────────

    def _handle_token(self):
        # Verify shared secret
        secret = self.headers.get("X-Secret", "")
        if LIVETRADER_WORKER_SECRET and not hmac.compare_digest(secret, LIVETRADER_WORKER_SECRET):
            log.warning("[TOKEN] Rejected /token push — wrong secret")
            self.send_error(403)
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_error(400, "Invalid JSON")
            return

        token     = body.get("token", "").strip()
        client_id = body.get("client_id", "").strip() or FYERS_CLIENT_ID

        if not token:
            self.send_error(400, "Missing token")
            return

        if _token_event.is_set():
            log.info("[TOKEN] Token refresh received — stored for next restart")
        else:
            log.info("[TOKEN] Fyers token received from Windows worker")

        _received_token["token"]     = token
        _received_token["client_id"] = client_id
        _token_event.set()

        self._json(200, {"status": "ok"})

    # ── /notify — upstream pipeline POSTs stock notifications ────────────────

    def _handle_notify(self):
        with _tracker_lock:
            tracker = _tracker

        if tracker is None:
            log.warning("[NOTIFY] Request received but tracker not ready yet (waiting for token)")
            self._json(503, {"error": "not_ready", "detail": "Waiting for Fyers token from Windows worker"})
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

        threading.Thread(
            target=_safe_add_stock,
            args=(tracker, data),
            daemon=True,
        ).start()

        self._json(200, {"status": "ok"})

    # ── /status — health check ────────────────────────────────────────────────

    def _handle_status(self):
        with _tracker_lock:
            tracker = _tracker

        ready   = tracker is not None
        today   = datetime.now(IST).strftime("%Y-%m-%d")
        trades  = database.get_today_trades(today) if ready else []
        summary = tracker.get_status_summary() if ready else {}

        body = json.dumps({
            "status":     "running",
            "ready":      ready,
            "time_ist":   datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "trades_today": len(trades),
            "live_summary": summary,
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)


def _safe_add_stock(tracker, data: dict) -> None:
    try:
        tracker.add_stock(data)
    except Exception as exc:
        log.exception("Unhandled error in add_stock for %s: %s", data.get("company_name"), exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _tracker

    Path(FYERS_LOG_PATH).mkdir(parents=True, exist_ok=True)

    log.info("══════════════════════════════════════════")
    log.info("  LiveTrader — Paper Trading Engine")
    log.info("══════════════════════════════════════════")

    # 1. Load Trendlyne CSV
    log.info("[1/3] Loading Trendlyne CSV ...")
    trendlyne_manager.load()
    trendlyne_manager.start_reload_thread()

    # 2. Load Fyers symbol master
    log.info("[2/3] Downloading Fyers symbol master ...")
    symbol_master.preload()

    # 3. Init database
    log.info("[3/3] Initialising database ...")
    database.init_db()
    flushed = database.flush_stale_entered_trades(datetime.now(IST).isoformat())
    if flushed:
        log.warning("Flushed %d stale ENTERED trade(s) from previous session (SYSTEM_RESTART)", flushed)

    # 4. Start HTTP server in background — ready to receive /token push immediately
    server = HTTPServer((NOTIFY_HOST, NOTIFY_PORT), _Handler)

    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping ...")
        with _tracker_lock:
            t = _tracker
        if t:
            t.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="http-server")
    server_thread.start()

    log.info("──────────────────────────────────────────")
    log.info("  HTTP server listening on %s:%d", NOTIFY_HOST, NOTIFY_PORT)
    log.info("  Waiting for Fyers token from Windows worker (POST /token) ...")
    log.info("──────────────────────────────────────────")

    telegram_manager.send_system(
        f"LiveTrader started  {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
        f"Waiting for Fyers token from Windows worker on port {NOTIFY_PORT} ..."
    )

    # 5. Block until Windows worker pushes /token (no hard timeout — operator restarts if stuck)
    TOKEN_WAIT_HOURS = 4   # warn if still no token after 4 hours
    if not _token_event.wait(timeout=TOKEN_WAIT_HOURS * 3600):
        log.error("No Fyers token received after %d hours — still waiting (check Windows worker)", TOKEN_WAIT_HOURS)
        telegram_manager.send_system(
            f"WARNING: No Fyers token received after {TOKEN_WAIT_HOURS}h. "
            f"Check livetrader_worker on Windows."
        )
        _token_event.wait()  # keep waiting indefinitely

    token     = _received_token["token"]
    client_id = _received_token.get("client_id") or FYERS_CLIENT_ID

    log.info("[TOKEN] Initialising LiveTracker with received token ...")

    # 6. Init tracker with the token
    from LiveTrader.tracker import LiveTracker
    tracker = LiveTracker(client_id=client_id, access_token=token)
    tracker.start()

    with _tracker_lock:
        _tracker = tracker

    # 7. Start market-close watcher
    threading.Thread(
        target=_market_close_watcher,
        args=(tracker,),
        daemon=True,
        name="market-close-watcher",
    ).start()

    log.info("──────────────────────────────────────────")
    log.info("  Ready — accepting notifications on %s:%d/notify", NOTIFY_HOST, NOTIFY_PORT)
    log.info("  GET  /status  for health check")
    log.info("──────────────────────────────────────────")

    telegram_manager.send_system(
        f"LiveTrader READY  {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
        f"Fyers token received. Listening for notifications on port {NOTIFY_PORT}."
    )

    # Keep main thread alive
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
        log.info("LiveTrader stopped.")


if __name__ == "__main__":
    main()
