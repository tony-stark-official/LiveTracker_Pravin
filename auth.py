"""
Fyers token management — fully automatic browser login.

Flow:
  Always: delete old token → full SeleniumBase browser login → save new token.
  No validity check, no caching. Every call generates a fresh token.

Note: Fyers discontinued the refresh_token API from 1st April 2026 (SEBI requirement).
      Every session now requires a fresh browser 2FA login.
"""
from __future__ import annotations

import datetime
import email.utils
import logging
import time
import urllib.parse

log = logging.getLogger("pipeline.fyers.auth")


class FyersAuthManager:
    def __init__(
        self,
        db,             # FyersDB
        app_id: str,
        secret_id: str,
        redirect_uri: str,
        mobile: str,
        pin: str,
        totp_key: str,
    ):
        self.db = db
        self.app_id = app_id
        self.secret_id = secret_id
        self.redirect_uri = redirect_uri
        self.mobile = mobile
        self.pin = pin
        self.totp_key = totp_key
        self._time_offset = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def force_login(self) -> str | None:
        """
        Delete old token, run a fresh browser login, save and return new token.
        Always called from scratch — no validity checks, no caching.
        """
        log.info("[FYERS] Deleting old token and running fresh browser login...")
        self.db.delete_token()
        access_token, _ = self._full_login_browser()
        if access_token:
            self.db.save_token(access_token)
            log.info("[FYERS] Browser login successful — new token saved")
            return access_token
        log.error("[FYERS] Browser login failed")
        return None

    # ── Full browser login ─────────────────────────────────────────────────────

    def _sync_clock(self):
        """Sync internal clock offset with Google's server time (for TOTP accuracy)."""
        import requests
        try:
            r = requests.get("https://www.google.com", timeout=5)
            server_date = r.headers.get("Date", "")
            if server_date:
                server_ts = email.utils.parsedate_to_datetime(server_date).timestamp()
                self._time_offset = server_ts - time.time()
                log.info(f"[FYERS] Clock synced — offset: {self._time_offset:.2f}s")
        except Exception:
            self._time_offset = 0.0

    def _get_totp(self) -> str:
        import pyotp
        totp = pyotp.TOTP(self.totp_key)
        return totp.at(time.time() + self._time_offset)

    def _full_login_browser(self) -> tuple[str, str] | tuple[None, None]:
        """
        Opens a SeleniumBase Undetected Chrome browser and completes the
        Fyers login flow: mobile → Cloudflare captcha → TOTP → PIN → auth_code.
        Returns (access_token, "") or (None, None) on failure.
        """
        try:
            from fyers_apiv3 import fyersModel
            from seleniumbase import SB
        except ImportError as e:
            log.error(f"[FYERS] Missing dependency for browser login: {e}")
            return None, None

        self._sync_clock()

        try:
            session = fyersModel.SessionModel(
                client_id=self.app_id,
                secret_key=self.secret_id,
                redirect_uri=self.redirect_uri,
                response_type="code",
                grant_type="authorization_code",
            )
            auth_url = session.generate_authcode()
        except Exception as e:
            log.error(f"[FYERS] Failed to generate auth URL: {e}")
            return None, None

        for attempt in range(1, 4):
            log.info(f"[FYERS] Browser login attempt {attempt}/3...")
            try:
                with SB(uc=True, test=True, locale="en", headless=False) as sb:
                    sb.uc_open_with_reconnect(auth_url, reconnect_time=3)

                    # Mobile number
                    sb.wait_for_element("#mobile-code", timeout=10)
                    sb.type("#mobile-code", self.mobile.replace(" ", ""))

                    # Cloudflare Turnstile captcha
                    sb.uc_gui_click_captcha()

                    # Continue button
                    sb.wait_for_element_clickable("#mobileNumberSubmit", timeout=10)
                    sb.click("#mobileNumberSubmit")

                    # TOTP — wait for OTP container
                    sb.wait_for_element("#otp-container", timeout=10)

                    # Safe buffer: avoid generating TOTP near 30s/60s boundary
                    real_now = datetime.datetime.fromtimestamp(
                        time.time() + self._time_offset
                    )
                    if (27 <= real_now.second < 30) or real_now.second > 57:
                        sb.sleep(4)

                    otp = self._get_totp()
                    otp_inputs = ["#first", "#second", "#third", "#fourth", "#fifth", "#sixth"]
                    for i, digit in enumerate(otp):
                        sb.add_text(f"#otp-container input{otp_inputs[i]}", digit)
                        sb.sleep(0.1)

                    # PIN
                    sb.wait_for_element("#verifyPinForm", timeout=15)
                    pin_inputs = ["#first", "#second", "#third", "#fourth"]
                    for i, digit in enumerate(self.pin):
                        sb.add_text(f"#pin-container input{pin_inputs[i]}", digit)
                        sb.sleep(0.1)

                    # Wait for redirect with auth_code in URL
                    found_url = None
                    for _ in range(30):
                        curr = sb.get_current_url()
                        if "auth_code=" in curr:
                            found_url = curr
                            break
                        time.sleep(1)

                    if not found_url:
                        raise RuntimeError("auth_code redirect not detected within 30s")

                    parsed = urllib.parse.urlparse(found_url)
                    auth_code = urllib.parse.parse_qs(parsed.query).get(
                        "auth_code", [None]
                    )[0]
                    if not auth_code:
                        raise RuntimeError("auth_code missing from redirect URL")

                    session.set_token(auth_code)
                    response = session.generate_token()

                    if response and "access_token" in response:
                        access_token = response["access_token"]
                        log.info("[FYERS] Browser login successful")
                        return access_token, ""

                    raise RuntimeError(f"Token generation failed: {response}")

            except Exception as e:
                log.error(f"[FYERS] Browser login attempt {attempt} failed: {e}")
                if attempt < 3:
                    time.sleep(5)

        return None, None
