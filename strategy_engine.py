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
_LIVE_TP         = 0.020   # +2.0% take-profit
_LIVE_SL         = 0.015   # -1.5% stop-loss
_LIVE_HOLD_MIN   = 0.008   # must reach +0.8% by 5-min or exit
_LIVE_FINAL_EXIT = 15      # hard exit after 15 minutes

# ─── Score gates ──────────────────────────────────────────────────────────────
_SCORE_ENTER    = 50   # minimum to enter any trade
_SCORE_HIGH_CONV = 70  # high-conviction: full TP target; below: 80% TP

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
    if ev.order_value_cr > 0 and ev.order_value_cr < _MIN_ORDER_CR:
        return True, f"ORDER_TOO_SMALL ({ev.order_value_cr:.2f} Cr)"

    if ev.market_cap_cr is not None and ev.market_cap_cr < _MIN_MCAP_CR:
        return True, f"MCAP_TOO_SMALL ({ev.market_cap_cr:.0f} Cr)"

    vol = ev.vol_week_avg or ev.vol_month_avg or 0
    if vol < _MIN_VOL_WEEK:
        return True, f"ILLIQUID (vol_week={vol})"

    # Dead stock pattern: zero price movement on a small-cap
    if ev.price_15s_pct == 0.0 and ev.market_cap_cr is not None and ev.market_cap_cr < 200:
        return True, "DEAD_STOCK_PATTERN"

    # Sell-off already in progress — don't chase
    if ev.price_15s_pct is not None and ev.price_15s_pct < -1.5:
        return True, f"SELL_OFF_IN_PROGRESS ({ev.price_15s_pct:.2f}%)"

    return False, ""


# ─── Scorer ───────────────────────────────────────────────────────────────────

def score_trade(ev: EventData) -> tuple[int, Literal["BUY", "SHORT", "SKIP"]]:
    """
    Score 0–100 and return (score, direction).
    Live-only: starts at 30 (same-day 94% win rate premium baked in).
    """
    score = 30  # base: always live

    rsi = ev.rsi
    if rsi is not None:
        if 55 <= rsi <= 72:
            score += 20   # ideal momentum zone
        elif 45 <= rsi < 55 or 72 < rsi <= 76:
            score += 10
        elif rsi > 76:
            score -= 10   # overbought — sell-off risk

    wk = ev.week_chg_pct
    if wk is not None:
        if wk < 10:
            score += 15
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

    p15 = ev.price_15s_pct
    if p15 is not None:
        if p15 > 0.5:
            score += 10   # price already moving: momentum confirmation
        elif p15 > 0.0:
            score += 5
        elif p15 < -0.5:
            score -= 20   # sell-off started — abort

    day = ev.day_chg_pct
    if day is not None and day >= 1.0:
        score += 5

    score = max(0, min(100, score))

    # ── Short signal ─────────────────────────────────────────────────────────
    short_score = 0
    if rsi is not None and rsi > 70:
        short_score += 20
    if wk is not None and wk > 25:
        short_score += 15
    if p15 is not None and p15 < -0.5:
        short_score += 20
    if ev.order_value_cr >= 200:
        short_score += 10

    if short_score >= 50 and score < _SCORE_ENTER:
        return short_score, "SHORT"

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
