"""
telegram_manager.py — formatted trade alerts via Telegram Bot API.

All send_* functions are fire-and-forget with automatic retries.
Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID in .env.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from LiveTrader.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _now_str() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


def _fmt_dur(duration_min: float) -> str:
    s = int(duration_min * 60)
    return f"{s // 60}m {s % 60}s"


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
    order_value_cr: float = 0.0,
    market_cap_cr: float | None = None,
    industry: str | None = None,
) -> None:
    order_line  = f"📋 {order_type}" + (f"  |  ₹{order_value_cr:.2f} Cr" if order_value_cr else "")
    mcap_str    = f"₹{market_cap_cr:.0f} Cr" if market_cap_cr else None
    sector_str  = industry or None
    meta_parts  = [p for p in [mcap_str, sector_str] if p]
    meta_line   = f"\n💹 {' | '.join(meta_parts)}" if meta_parts else ""
    text = (
        f"⏭️ <b>SKIPPED</b>  <i>{_now_str()}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 <code>{fyers_symbol or 'N/A'}</code>\n"
        f"{order_line}"
        f"{meta_line}\n"
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
    conviction: str = "",
    event_value_cr: float | None = None,
    market_cap_cr: float | None = None,
    order_impact_pct: float | None = None,
    industry: str | None = None,
    rsi: float | None = None,
    note: str | None = None,
) -> None:
    conv        = conviction or ("HIGH" if score >= 70 else "NORMAL")
    event_str   = f"  |  ₹{event_value_cr:.2f} Cr" if event_value_cr else ""
    mcap_str    = f"₹{market_cap_cr:.0f} Cr" if market_cap_cr else "N/A"
    impact_str  = f"  🔥 {order_impact_pct:.1f}% of MCap" if order_impact_pct else ""
    sector_line = f"\n🏭 {industry}" if industry else ""
    rsi_str     = f"  |  RSI: {rsi:.1f}" if rsi is not None else ""
    note_line   = f"\n📅 <i>{note}</i>" if note else ""
    trigger_px  = f"₹{ref_price * 1.003:.2f}" if ref_price else "N/A"
    text = (
        f"🟢 <b>TRACKING — BUY</b>  <i>{_now_str()}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 <code>{fyers_symbol}</code>\n"
        f"📋 {order_type}{event_str}\n"
        f"💹 MCap: {mcap_str}{impact_str}"
        f"{sector_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Score: <b>{score}/100</b>  ({conv} conviction){rsi_str}\n"
        f"💰 Ref Price: ₹{ref_price:.2f}\n"
        f"⏳ Entry trigger: {trigger_px}  <i>(+0.3% above open)</i>"
        f"{note_line}"
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
    industry: str | None = None,
    rsi: float | None = None,
    score: int = 0,
) -> None:
    conv        = "HIGH" if score >= 70 else "NORMAL"
    tp_pct      = (tp - entry_price) / entry_price * 100
    sl_pct      = (entry_price - sl) / entry_price * 100
    event_str   = f"  |  ₹{event_value_cr:.2f} Cr" if event_value_cr else ""
    mcap_str    = f"₹{market_cap_cr:.0f} Cr" if market_cap_cr else "N/A"
    rsi_str     = f"  |  RSI: {rsi:.1f}" if rsi is not None else ""
    sector_line = f"\n🏭 {industry}" if industry else ""
    time_tag    = f"  <i>({entry_time_str})</i>" if entry_time_str else ""
    text = (
        f"📈 <b>ENTERED</b>  <i>{_now_str()}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 <code>{fyers_symbol}</code>\n"
        f"📋 {order_type}{event_str}\n"
        f"💹 MCap: {mcap_str}  |  Score: {score} ({conv}){rsi_str}"
        f"{sector_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Entry:  ₹{entry_price:.2f}{time_tag}\n"
        f"🎯 TP:     ₹{tp:.2f}  <i>(+{tp_pct:.1f}%)</i>\n"
        f"🛑 SL:     ₹{sl:.2f}  <i>(-{sl_pct:.1f}%)</i>"
    )
    _send(text)


def send_exit(
    company: str,
    fyers_symbol: str,
    order_type: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    exit_reason: str,
    entry_time_str: str | None = None,
    exit_time_str: str | None = None,
    duration_min: float = 0.0,
    event_value_cr: float = 0.0,
    market_cap_cr: float | None = None,
) -> None:
    is_win      = pnl_pct > 0
    emoji       = "✅" if is_win else "❌"
    outcome     = "WIN" if is_win else "LOSS"
    sign        = "+" if pnl_pct >= 0 else ""
    event_str   = f"  |  ₹{event_value_cr:.2f} Cr" if event_value_cr else ""
    mcap_str    = f"  |  MCap ₹{market_cap_cr:.0f} Cr" if market_cap_cr else ""
    entry_tag   = f"  <i>({entry_time_str})</i>" if entry_time_str else ""
    exit_tag    = f"  <i>({exit_time_str})</i>" if exit_time_str else ""
    dur_line    = f"⏱️ Held:   {_fmt_dur(duration_min)}\n" if duration_min else ""
    text = (
        f"{emoji} <b>{outcome} — {exit_reason}</b>  <i>{_now_str()}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 <code>{fyers_symbol}</code>\n"
        f"📋 {order_type}{event_str}{mcap_str}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Entry:  ₹{entry_price:.2f}{entry_tag}\n"
        f"💰 Exit:   ₹{exit_price:.2f}{exit_tag}\n"
        f"{dur_line}"
        f"📊 P&L:    <b>{sign}{pnl_pct:.2f}%</b>"
    )
    _send(text)


def send_time_exit(
    company: str,
    fyers_symbol: str,
    order_type: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    exit_reason: str,
    entry_time_str: str | None = None,
    exit_time_str: str | None = None,
    duration_min: float = 0.0,
    event_value_cr: float = 0.0,
    market_cap_cr: float | None = None,
) -> None:
    sign        = "+" if pnl_pct >= 0 else ""
    event_str   = f"  |  ₹{event_value_cr:.2f} Cr" if event_value_cr else ""
    mcap_str    = f"  |  MCap ₹{market_cap_cr:.0f} Cr" if market_cap_cr else ""
    entry_tag   = f"  <i>({entry_time_str})</i>" if entry_time_str else ""
    exit_tag    = f"  <i>({exit_time_str})</i>" if exit_time_str else ""
    dur_line    = f"⏱️ Held:   {_fmt_dur(duration_min)}\n" if duration_min else ""
    text = (
        f"⏰ <b>TIME EXIT — {exit_reason}</b>  <i>{_now_str()}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>{company}</b>\n"
        f"📌 <code>{fyers_symbol}</code>\n"
        f"📋 {order_type}{event_str}{mcap_str}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Entry:  ₹{entry_price:.2f}{entry_tag}\n"
        f"💰 Exit:   ₹{exit_price:.2f}{exit_tag}\n"
        f"{dur_line}"
        f"📊 P&L:    <b>{sign}{pnl_pct:.2f}%</b>"
    )
    _send(text)


def send_system(message: str) -> None:
    """Generic system / status message."""
    _send(f"⚙️ <b>SYSTEM</b>  <i>{_now_str()}</i>\n{message}")
