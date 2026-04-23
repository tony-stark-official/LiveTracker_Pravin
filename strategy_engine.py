"""
strategy_engine.py — live-only filter and scoring for order-flow events.

All trades are same-day live (notifications during market hours).
No prev-day logic. No simulation functions. No candle replay.

KEY FINDINGS (49-event dataset, Apr 3–15 2026):
  Same-day live  : 94% win rate at 2-5 min, avg +2.06% EOD
  Dead stocks    : MCap < 100 Cr OR vol_week < 15k → skip
  RSI sweet spot : 50–75; RSI > 76 + week_chg > 25% = overbought trap
  TP / SL        : Winners +3–8%, losers -3–7% → TP +2%, SL -1.5%
"""

from dataclasses import dataclass, field
from typing import Literal

# ─── Live trade thresholds ────────────────────────────────────────────────────
_LIVE_ENTRY_MIN  = 0.003   # +0.3% above day-open to trigger entry
_LIVE_TP         = 0.025   # +2.5% take-profit (HIGH conviction); NORMAL gets 80% = +2.0%
_LIVE_SL         = 0.015   # -1.5% stop-loss (NORMAL conviction)
_LIVE_SL_HIGH    = 0.010   # -1.0% stop-loss (HIGH conviction ≥70 → R:R = 2.5:1)
_LIVE_HOLD_MIN   = 0.008   # must reach +0.8% by 5-min or exit
_LIVE_FINAL_EXIT = 15      # hard exit after 15 minutes

# ─── Score gates ──────────────────────────────────────────────────────────────
_SCORE_ENTER    = 65   # minimum to enter any trade (raised from 50 — quality over quantity)
_SCORE_HIGH_CONV = 70  # high-conviction: full TP + tighter SL; below: 80% TP + wider SL

# ─── Hard filters ─────────────────────────────────────────────────────────────
_MIN_MCAP_CR  = 100.0   # market cap in ₹ Crore
_MIN_ORDER_CR = 15.0    # block order value in ₹ Crore (0 = unknown, skip check)
_MIN_VOL_WEEK = 15_000  # weekly avg volume


# ─── Data structure ───────────────────────────────────────────────────────────

@dataclass
class EventData:
    """
    Fields from the order-flow notification + Trendlyne snapshot.
    All fields except order_value_cr are optional (None = unknown).
    order_value_cr = 0 means not provided → order-size filter is skipped.
    """
    order_value_cr:   float = 0.0        # ₹ Crore; 0 = unknown
    market_cap_cr:    float | None = None
    rsi:              float | None = None
    day_chg_pct:      float | None = None
    week_chg_pct:     float | None = None
    month_chg_pct:    float | None = None
    vol_week_avg:     int | None = None
    vol_month_avg:    int | None = None
    price_15s_pct:    float | None = None  # % change from ref price at ~15s after event
    industry:         str = ""
    alfa_reason:      bool = False          # True = earning call where AI flagged strong beat
    profit_growth_qoq: float | None = None  # QoQ net profit growth % — financial results only

    order_impact_pct: float = field(init=False)

    def __post_init__(self) -> None:
        if self.market_cap_cr and self.market_cap_cr > 0 and self.order_value_cr:
            self.order_impact_pct = (self.order_value_cr / self.market_cap_cr) * 100
        else:
            self.order_impact_pct = 0.0


# ─── Hard filter ──────────────────────────────────────────────────────────────

def filter_event(ev: EventData) -> tuple[bool, str]:
    """
    Returns (should_skip, reason).
    Any True → do not trade.
    """
    # Skip order-size check when value is unknown (0)
    # Allow small absolute order through if it's a high relative impact on the company
    if ev.order_value_cr > 0 and ev.order_value_cr < _MIN_ORDER_CR:
        if ev.order_impact_pct < 10:
            return True, f"ORDER_TOO_SMALL ({ev.order_value_cr:.2f} Cr, {ev.order_impact_pct:.1f}% impact)"

    # Allow small-cap through when order impact is large — these are the hidden gems
    if ev.market_cap_cr is not None and ev.market_cap_cr < _MIN_MCAP_CR:
        if ev.order_impact_pct < 20:
            return True, f"MCAP_TOO_SMALL ({ev.market_cap_cr:.0f} Cr, {ev.order_impact_pct:.1f}% impact)"

    vol = ev.vol_week_avg or ev.vol_month_avg or 0
    is_small_cap_gem = (
        ev.market_cap_cr is not None
        and ev.market_cap_cr < _MIN_MCAP_CR
        and ev.order_impact_pct >= 20
    )
    is_qualitative_event = ev.alfa_reason or ev.profit_growth_qoq is not None
    if vol < _MIN_VOL_WEEK and not is_small_cap_gem and not is_qualitative_event:
        return True, f"ILLIQUID (vol_week={vol})"

    return False, ""


# ─── Scorer ───────────────────────────────────────────────────────────────────

def score_trade(ev: EventData) -> tuple[int, Literal["BUY", "SKIP"]]:
    """
    Score 0–100 and return (score, direction).
    Live-only: starts at 30 (same-day 94% win rate premium baked in).
    """
    score = 30  # base: always live

    rsi = ev.rsi
    if rsi is not None:
        if 55 <= rsi <= 72:
            score += 20   # ideal momentum zone
        elif 72 < rsi <= 76:
            score += 10   # nearing overbought — caution
        elif 45 <= rsi < 55:
            score += 5    # neutral build — reduced reward vs ideal zone
        elif rsi > 76:
            score -= 10   # overbought — sell-off risk
        # rsi < 45: 0 points — downtrend, no reward for a momentum buy

    wk = ev.week_chg_pct
    if wk is not None:
        if wk < -5:
            score -= 5    # declining trend on the week — negative signal
        elif wk < 10:
            score += 15   # fresh/flat: hasn't run up yet
        elif wk < 20:
            score += 10
        elif wk < 30:
            score += 5
        else:
            score -= 10   # > 30% weekly — profit-taking territory

    impact = ev.order_impact_pct
    if impact >= 15:
        score += 15
    elif impact >= 5:
        score += 10
    elif impact >= 2:
        score += 5

    vol = ev.vol_week_avg or ev.vol_month_avg or 0
    if vol >= 100_000:
        score += 10
    elif vol >= 15_000:
        score += 5

    day = ev.day_chg_pct
    if day is not None:
        if day >= 1.0:
            score += 5    # already moving positively today
        elif day < 0:
            score -= 5    # declining on the day — momentum not confirmed

    # Earning call: AI-flagged strong beat (qualitative signal, no order value)
    if ev.alfa_reason:
        score += 30   # replaces order_impact scoring for this event type

    # Financial result: QoQ net profit growth momentum
    if ev.profit_growth_qoq is not None:
        if ev.profit_growth_qoq > 200:
            score += 20   # massive growth or deep turnaround — very strong signal
        elif ev.profit_growth_qoq > 50:
            score += 10   # solid growth
        elif ev.profit_growth_qoq < 0:
            score -= 15   # declining profits — don't chase a bounce

    score = max(0, min(100, score))

    if score >= _SCORE_ENTER:
        return score, "BUY"

    return score, "SKIP"


# ─── Quick pre-check (no candles needed) ─────────────────────────────────────

def evaluate_notification(event_data: EventData) -> dict:
    """
    Instant pre-trade check on notification arrival.
    Returns a decision dict: {action, score, conviction, direction, reason}.
    """
    skip, reason = filter_event(event_data)
    if skip:
        return {"action": "SKIP", "reason": reason, "score": 0, "direction": "SKIP"}

    score, direction = score_trade(event_data)
    conviction = "HIGH" if score >= _SCORE_HIGH_CONV else "NORMAL"
    return {
        "action":     direction,
        "score":      score,
        "conviction": conviction,
        "direction":  direction,
        "reason":     f"score={score} conviction={conviction}",
    }
