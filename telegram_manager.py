"""
telegram_manager.py — formatted trade alerts via Telegram Bot API.

All send_* functions are fire-and-forget with automatic retries.
Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID in .env.
"""

import logging
import time

import requests

from LiveTrader.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

log = logging.getLogger(__name__)


def _send(text: str, retries: int = 3) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        log.warning("Telegram not configured — skipping alert")
        return False

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": text, "parse_mode": "HTML"}

    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.warning("Telegram attempt %d/%d failed: %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)

    log.error("Telegram: failed to send after %d retries", retries)
    return False


# ─── Message builders ─────────────────────────────────────────────────────────

def send_skip(
    company: str,
    fyers_symbol: str,
    isin: str,
    order_type: str,
    reason: str,
) -> None:
    text = (
        "⏭️ <b>SKIPPED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 Symbol  : <code>{fyers_symbol or 'N/A'}</code>\n"
        f"🔖 ISIN    : <code>{isin or 'N/A'}</code>\n"
        f"📋 Order   : <b>{order_type}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"❌ <i>{reason}</i>"
    )
    _send(text)


def send_tracking(
    company: str,
    fyers_symbol: str,
    isin: str,
    order_type: str,
    direction: str,
    score: int,
    ref_price: float,
    event_value_cr: float | None = None,
    market_cap_cr: float | None = None,
    order_impact_pct: float | None = None,
    industry: str | None = None,
    rsi: float | None = None,
    note: str | None = None,
) -> None:
    emoji  = "🟢" if direction == "BUY" else "🔴"
    conv   = "HIGH" if score >= 70 else "NORMAL"
    event_line  = f"🏷️ Event   : ₹{event_value_cr:.2f} Cr\n" if event_value_cr else ""
    mcap_line   = f"💹 MCap    : ₹{market_cap_cr:.2f} Cr\n" if market_cap_cr else ""
    ref_line    = f"💰 Ref Price  : ₹{ref_price:.2f}\n" if ref_price else ""
    impact_line = f"🔥 Impact     : {order_impact_pct:.1f}% of MCap\n" if order_impact_pct else ""
    sector_line = f"🏭 Industry   : {industry}\n" if industry else ""
    rsi_line    = f"📉 RSI        : {rsi:.1f}\n" if rsi is not None else ""
    note_line   = f"\n📅 <i>{note}</i>" if note else ""
    text = (
        f"{emoji} <b>TRACKING — {direction}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 Symbol  : <code>{fyers_symbol}</code>\n"
        f"🔖 ISIN    : <code>{isin}</code>\n"
        f"📋 Order   : <b>{order_type}</b>\n"
        f"{event_line}"
        f"{mcap_line}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Score      : <b>{score}/100</b> ({conv})\n"
        f"{impact_line}"
        f"{sector_line}"
        f"{rsi_line}"
        f"{ref_line}"
        f"⏳ Waiting for entry trigger...{note_line}"
    )
    _send(text)


def send_entry(
    company: str,
    fyers_symbol: str,
    order_type: str,
    direction: str,
    entry_price: float,
    tp: float,
    sl: float,
    entry_time_str: str | None = None,
    event_value_cr: float | None = None,
    market_cap_cr: float | None = None,
) -> None:
    emoji = "📈" if direction == "BUY" else "📉"
    tp_pct = abs(tp - entry_price) / entry_price * 100
    sl_pct = abs(sl - entry_price) / entry_price * 100
    event_line = f"🏷️ Event   : ₹{event_value_cr:.2f} Cr\n" if event_value_cr else ""
    mcap_line  = f"💹 MCap    : ₹{market_cap_cr:.2f} Cr\n" if market_cap_cr else ""
    time_tag   = f"  <i>({entry_time_str})</i>" if entry_time_str else ""
    text = (
        f"{emoji} <b>ENTERED — {direction}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 Symbol  : <code>{fyers_symbol}</code>\n"
        f"📋 Order   : <b>{order_type}</b>\n"
        f"{event_line}"
        f"{mcap_line}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Entry   : ₹{entry_price:.2f}{time_tag}\n"
        f"🎯 TP      : ₹{tp:.2f}  <i>(+{tp_pct:.1f}%)</i>\n"
        f"🛑 SL      : ₹{sl:.2f}  <i>(-{sl_pct:.1f}%)</i>"
    )
    _send(text)


def send_exit(
    company: str,
    fyers_symbol: str,
    order_type: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    exit_reason: str,
) -> None:
    is_win  = pnl_pct > 0
    emoji   = "✅" if is_win else "❌"
    outcome = "WIN" if is_win else "LOSS"
    sign    = "+" if pnl_pct >= 0 else ""
    text = (
        f"{emoji} <b>{outcome} — {direction}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 Symbol  : <code>{fyers_symbol}</code>\n"
        f"📋 Order   : <b>{order_type}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Entry   : ₹{entry_price:.2f}\n"
        f"💰 Exit    : ₹{exit_price:.2f}\n"
        f"📊 P&L     : <b>{sign}{pnl_pct:.2f}%</b>\n"
        f"🔍 Reason  : <i>{exit_reason}</i>"
    )
    _send(text)


def send_time_exit(
    company: str,
    fyers_symbol: str,
    order_type: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    exit_reason: str,
) -> None:
    """Same as send_exit but with a clock emoji to distinguish time-based exits."""
    sign = "+" if pnl_pct >= 0 else ""
    text = (
        f"⏰ <b>TIME EXIT — {direction}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 Symbol  : <code>{fyers_symbol}</code>\n"
        f"📋 Order   : <b>{order_type}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Entry   : ₹{entry_price:.2f}\n"
        f"💰 Exit    : ₹{exit_price:.2f}\n"
        f"📊 P&L     : <b>{sign}{pnl_pct:.2f}%</b>\n"
        f"🔍 Reason  : <i>{exit_reason}</i>"
    )
    _send(text)


def send_system(message: str) -> None:
    """Generic system / status message."""
    _send(f"⚙️ <b>SYSTEM</b>\n{message}")
