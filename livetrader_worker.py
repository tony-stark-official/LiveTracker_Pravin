"""
livetrader_worker.py — Windows-side Fyers auth worker for Pravin's LiveTrader.

Runs on the Windows PC via Task Scheduler (triggered at 08:25 IST every trading day).

Flow:
  1. Starts at 08:25 — immediately kicks off Fyers browser login in a background thread
  2. FyersAuthManager runs SeleniumBase login with Pravin's credentials (3 attempts)
  3. On success: POSTs the token to Linux LiveTrader at LIVETRADER_LINUX_URL/token
  4. Linux LiveTrader receives the token and starts the Fyers WebSocket
  5. Worker stays alive as an HTTP server (port 8769) for health checks + manual re-trigger

Env vars (from .env.livetrader in the same folder):
  # Pravin's Fyers credentials
  FYERS_APP_ID          = FRMEOR7L6Y-100
  FYERS_SECRET_ID       = 2DBTZW0AQJ
  FYERS_REDIRECT_URI    = https://trade.fyers.in/api-login/redirect-uri/index.html
  FYERS_MOBILE          = 6359970942
  FYERS_PIN             = 9624
  FYERS_TOTP_KEY        = 67F4X4C7J55BNIDAUMGOPGSSRCDW7WQT

  # Worker
  LIVETRADER_WORKER_PORT   = 8769
  LIVETRADER_WORKER_SECRET = <shared secret — must match Linux side>
  LIVETRADER_LINUX_URL     = http://<linux_tailscale_ip>:8765

Setup:
  1. pip install fyers-apiv3 seleniumbase pyotp requests python-dotenv
  2. Create .env.livetrader in this folder with the vars above
  3. Add Task Scheduler task: runs this file at 08:25 on trading days

Task Scheduler command:
  Program : pythonw.exe
  Args    : "C:\\path\\to\\LiveTrader\\livetrader_worker.py"
  Start in: C:\\path\\to\\LiveTrader
"""
from __future__ import annotations

import gc
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

# Load from .env.livetrader in the same directory as this file
load_dotenv(Path(__file__).parent / ".env.livetrader")

# Ensure the parent directory is on sys.path so LiveTrader package imports work
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
WORKER_PORT    = int(os.getenv("LIVETRADER_WORKER_PORT", "8769"))
WORKER_SECRET  = os.getenv("LIVETRADER_WORKER_SECRET", "")
LINUX_URL      = os.getenv("LIVETRADER_LINUX_URL", "")    # http://<ip>:8765

FYERS_APP_ID      = os.getenv("FYERS_APP_ID", "")
FYERS_SECRET_ID   = os.getenv("FYERS_SECRET_ID", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "")
FYERS_MOBILE      = os.getenv("FYERS_MOBILE", "")
FYERS_PIN         = os.getenv("FYERS_PIN", "")
FYERS_TOTP_KEY    = os.getenv("FYERS_TOTP_KEY", "")

_LOGIN_MAX_ATTEMPTS = 3   # outer retry loop (each attempt is itself 3 tries inside FyersAuthManager)
_RETRY_WAIT_SECONDS = 30  # wait between outer attempts
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            str(Path(__file__).parent / "livetrader_worker.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("livetrader_worker")

# ── Minimal token DB stub (no persistence needed on the Windows worker side) ──

class _TokenDB:
    def delete_token(self): pass
    def save_token(self, token: str): pass


# ── State ─────────────────────────────────────────────────────────────────────
_last_token: str | None = None          # most recent token obtained
_login_in_progress = threading.Lock()   # prevent overlapping logins
_login_status = {"state": "idle", "error": ""}  # for /status endpoint


# ── Fyers login ───────────────────────────────────────────────────────────────

def _run_fyers_login() -> str | None:
    """
    Run SeleniumBase Fyers browser login for Pravin's account.
    Returns the access token string on success, None on failure.
    Outer loop retries _LOGIN_MAX_ATTEMPTS times.
    """
    try:
        from LiveTrader.auth import FyersAuthManager
    except ImportError as exc:
        log.error("[FYERS] Cannot import FyersAuthManager: %s", exc)
        _login_status["state"] = "failed"
        _login_status["error"] = str(exc)
        return None

    auth = FyersAuthManager(
        db=_TokenDB(),
        app_id=FYERS_APP_ID,
        secret_id=FYERS_SECRET_ID,
        redirect_uri=FYERS_REDIRECT_URI,
        mobile=FYERS_MOBILE,
        pin=FYERS_PIN,
        totp_key=FYERS_TOTP_KEY,
    )

    for attempt in range(1, _LOGIN_MAX_ATTEMPTS + 1):
        log.info("[FYERS] Login attempt %d/%d ...", attempt, _LOGIN_MAX_ATTEMPTS)
        _login_status["state"] = f"login_attempt_{attempt}"
        _login_status["error"] = ""
        try:
            token = auth.force_login()
            if token:
                log.info("[FYERS] Login succeeded on attempt %d", attempt)
                _login_status["state"] = "success"
                return token
            log.warning("[FYERS] force_login() returned None on attempt %d", attempt)
        except Exception as exc:
            log.error("[FYERS] Attempt %d raised: %s", attempt, exc, exc_info=True)
            _login_status["error"] = str(exc)
        finally:
            gc.collect()

        if attempt < _LOGIN_MAX_ATTEMPTS:
            log.info("[FYERS] Waiting %ds before retry ...", _RETRY_WAIT_SECONDS)
            time.sleep(_RETRY_WAIT_SECONDS)

    log.error("[FYERS] All %d login attempts failed", _LOGIN_MAX_ATTEMPTS)
    _login_status["state"] = "failed"
    return None


# ── Push token to Linux ───────────────────────────────────────────────────────

def _push_token_to_linux(token: str) -> bool:
    """POST the token to the Linux LiveTrader /token endpoint. Returns True on success."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not LINUX_URL:
        log.warning("[PUSH] LIVETRADER_LINUX_URL not set — token not delivered to Linux")
        return False

    url = f"{LINUX_URL.rstrip('/')}/token"
    payload = {"token": token, "client_id": FYERS_APP_ID}
    headers = {"X-Secret": WORKER_SECRET, "Content-Type": "application/json"}

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                log.info("[PUSH] Token delivered to Linux (%s)", url)
                return True
            log.warning("[PUSH] Linux returned HTTP %d on attempt %d", resp.status_code, attempt)
        except Exception as exc:
            log.warning("[PUSH] Attempt %d/3 failed: %s", attempt, exc)
        if attempt < 3:
            time.sleep(5)

    log.error("[PUSH] Failed to deliver token to Linux after 3 attempts")
    return False


# ── Startup login thread ──────────────────────────────────────────────────────

def _login_and_push():
    """Run login + push in a background thread. Called on startup and via /retrigger."""
    global _last_token

    if not _login_in_progress.acquire(blocking=False):
        log.warning("[FYERS] Login already in progress — ignoring concurrent request")
        return

    try:
        log.info("[FYERS] Starting Fyers browser login for Pravin's account ...")
        token = _run_fyers_login()
        if not token:
            log.error("[FYERS] Login failed — token NOT delivered to Linux")
            return

        _last_token = token
        _push_token_to_linux(token)
    finally:
        _login_in_progress.release()


# ── HTTP server ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.rstrip("/") == "/status":
            body = json.dumps({
                "service":      "livetrader_worker",
                "port":         WORKER_PORT,
                "login_status": _login_status,
                "token_ready":  _last_token is not None,
                "linux_url":    LINUX_URL,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"livetrader_worker ok")

    def do_POST(self):
        secret = self.headers.get("X-Secret", "")
        import hmac as _hmac
        if WORKER_SECRET and not _hmac.compare_digest(secret, WORKER_SECRET):
            self.send_response(403)
            self.end_headers()
            return

        if self.path.rstrip("/") == "/retrigger":
            # Manual re-trigger: re-run login + push (e.g. if token expired mid-day)
            threading.Thread(target=_login_and_push, daemon=True, name="retrigger-login").start()
            log.info("[HTTP] Manual /retrigger received — login started")
            self._json(200, {"status": "login_started"})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence default access logs


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Disable Windows console Quick Edit mode (prevents process freezing on click)
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value & ~0x0040)

    # Validate required credentials at startup
    missing = [k for k, v in {
        "FYERS_APP_ID":       FYERS_APP_ID,
        "FYERS_SECRET_ID":    FYERS_SECRET_ID,
        "FYERS_REDIRECT_URI": FYERS_REDIRECT_URI,
        "FYERS_MOBILE":       FYERS_MOBILE,
        "FYERS_PIN":          FYERS_PIN,
        "FYERS_TOTP_KEY":     FYERS_TOTP_KEY,
    }.items() if not v]

    if missing:
        log.error("[MAIN] Missing required env vars: %s — check .env.livetrader", missing)
        sys.exit(1)

    if not LINUX_URL:
        log.warning("[MAIN] LIVETRADER_LINUX_URL not set — token will NOT be delivered to Linux")

    # Kick off the login immediately (worker was started at 08:25 by Task Scheduler)
    threading.Thread(target=_login_and_push, daemon=True, name="startup-login").start()

    log.info("[MAIN] LiveTrader Worker starting on port %d", WORKER_PORT)
    log.info("[MAIN] Linux target: %s", LINUX_URL or "(not set)")

    server = HTTPServer(("0.0.0.0", WORKER_PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("[MAIN] Shutting down")
        server.server_close()
