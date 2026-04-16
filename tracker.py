"""
tracker.py — Fyers WebSocket-powered live paper-trade state machine.

Flow:
  1. start()       — connect WebSocket (no symbols needed yet)
  2. add_stock()   — called per incoming notification; filter/score/subscribe
  3. _on_message() — tick handler; checks TP / SL / time for every tracked stock
  4. force_exit_all() — called at market close (15:25 IST)

Dynamic subscription: new symbols can be subscribed at any time without
restarting the WebSocket. Re-subscription happens automatically on reconnect.
"""

# ─── Logging ──────────────────────────────────────────────────────────────────
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from LiveTrader import database, telegram_manager, trendlyne_manager, symbol_master
from LiveTrader.config import FYERS_CLIENT_ID, FYERS_ACCESS_TOKEN, FYERS_LOG_PATH
from LiveTrader.strategy_engine import (
    EventData,
    _LIVE_ENTRY_MIN,
    _LIVE_FINAL_EXIT,
    _LIVE_HOLD_MIN,
    _LIVE_SL,
    _LIVE_TP,
    _SCORE_HIGH_CONV,
    filter_event,
    score_trade,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Trade status constants ───────────────────────────────────────────────────
WAITING_ENTRY = "WAITING_ENTRY"
ENTERED       = "ENTERED"
EXITED        = "EXITED"
SKIP          = "SKIP"


# ─── Per-stock trade state ────────────────────────────────────────────────────

@dataclass
class TradeState:
    trade_id:     int
    isin:         str
    fyers_symbol: str
    company_name: str
    raw_symbol:   str
    order_type:   str
    direction:    str       # BUY | SHORT
    score:        int
    status:       str = WAITING_ENTRY

    # Filled once we start receiving ticks
    day_open:     float | None = None
    entry_price:  float | None = None
    entry_time:   datetime | None = None
    entry_ts:     float = 0.0      # epoch seconds at entry (for elapsed-time checks)
    tp:           float | None = None
    sl:           float | None = None
    hold_price:   float | None = None  # must reach by 5-min or exit
    last_ltp:     float | None = None
    notif_time:   datetime = field(default_factory=lambda: datetime.now(IST))
    _lock:        threading.Lock = field(default_factory=threading.Lock, repr=False)


# ─── Live tracker ─────────────────────────────────────────────────────────────

class LiveTracker:
    """Thread-safe live paper-trading engine."""

    def __init__(self) -> None:
        self._trades:    dict[str, TradeState] = {}   # fyers_symbol → state
        self._trades_lock = threading.Lock()
        self._ws:        data_ws.FyersDataSocket | None = None
        self._ws_authed  = threading.Event()           # set after auth success

        self._fyers = fyersModel.FyersModel(
            client_id=FYERS_CLIENT_ID,
            token=FYERS_ACCESS_TOKEN,
            log_path=FYERS_LOG_PATH,
        )

    # ── Startup / shutdown ────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect the Fyers WebSocket. Safe to call with zero symbols."""
        log.info("─── Starting Fyers WebSocket ───")
        # write_to_file=True makes the WS thread a daemon so main() can run the HTTP loop
        self._ws = data_ws.FyersDataSocket(
            access_token=f"{FYERS_CLIENT_ID}:{FYERS_ACCESS_TOKEN}",
            log_path=FYERS_LOG_PATH,
            write_to_file=True,
            on_message=self._on_message,
            on_error=self._on_error,
            on_connect=self._on_connect,
            on_close=self._on_close,
            reconnect=True,
            reconnect_retry=50,
        )
        self._ws.connect()
        # connect() sleeps 2s internally; wait up to 15s for auth
        if not self._ws_authed.wait(timeout=15):
            log.warning("WebSocket auth timeout — will continue, retrying in background")

    def stop(self) -> None:
        if self._ws:
            self._ws.close_connection()

    # ── WebSocket callbacks ───────────────────────────────────────────────────

    def _on_connect(self) -> None:
        log.info("WebSocket connected (waiting for auth...)")

    def _on_error(self, msg: dict) -> None:
        log.error("WebSocket error: %s", msg)

    def _on_close(self, msg: dict) -> None:
        log.warning("WebSocket closed: %s", msg)
        self._ws_authed.clear()

    def _on_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return

        # ── Auth success → re-subscribe all active trades ─────────────────────
        # type="cn" is AUTH_TYPE from fyers defines.py
        if msg.get("type") == "cn" and msg.get("s") == "ok":
            log.info("WebSocket authenticated successfully")
            self._ws_authed.set()
            self._resubscribe_all()
            return

        # ── Ignore non-tick control messages ─────────────────────────────────
        if "ltp" not in msg:
            return

        symbol = msg.get("symbol")
        if not symbol:
            return

        with self._trades_lock:
            state = self._trades.get(symbol)

        if state and state.status not in (EXITED, SKIP):
            self._process_tick(state, msg)

    def _resubscribe_all(self) -> None:
        """Re-subscribe all non-exited trades after reconnect / auth."""
        with self._trades_lock:
            active = [s for s, t in self._trades.items() if t.status not in (EXITED, SKIP)]
        if active and self._ws:
            log.info("Re-subscribing %d active symbol(s) after auth", len(active))
            try:
                self._ws.subscribe(active)
            except Exception as exc:
                log.error("Re-subscribe failed: %s", exc)

    # ── Tick processing ───────────────────────────────────────────────────────

    def _process_tick(self, state: TradeState, msg: dict) -> None:
        ltp = msg.get("ltp")
        if not ltp:
            return

        with state._lock:
            state.last_ltp = ltp

            # Capture day_open from first tick (open_price field in websocket)
            if state.day_open is None:
                open_px = msg.get("open_price") or ltp
                if open_px:
                    state.day_open = open_px
                    log.info("[%s] day_open = ₹%.2f", state.fyers_symbol, open_px)

            # ── WAITING_ENTRY: watch for entry trigger ────────────────────────
            if state.status == WAITING_ENTRY and state.day_open:
                if state.direction == "BUY":
                    trigger = state.day_open * (1 + _LIVE_ENTRY_MIN)
                    if ltp >= trigger and ltp > state.day_open:
                        self._enter(state, ltp)
                elif state.direction == "SHORT":
                    trigger = state.day_open * (1 - _LIVE_ENTRY_MIN)
                    if ltp <= trigger and ltp < state.day_open:
                        self._enter(state, ltp)

            # ── ENTERED: check TP / SL / time ─────────────────────────────────
            elif state.status == ENTERED:
                elapsed_min = (time.time() - state.entry_ts) / 60.0

                if state.direction == "BUY":
                    if ltp >= state.tp:
                        self._exit(state, ltp, "TAKE_PROFIT")
                        return
                    if ltp <= state.sl:
                        self._exit(state, ltp, "STOP_LOSS")
                        return
                    if elapsed_min >= 5 and ltp < state.hold_price:
                        self._exit(state, ltp, "TIME_EXIT_5MIN")
                        return

                elif state.direction == "SHORT":
                    if ltp <= state.tp:        # tp is lower for short
                        self._exit(state, ltp, "TAKE_PROFIT")
                        return
                    if ltp >= state.sl:        # sl is higher for short
                        self._exit(state, ltp, "STOP_LOSS")
                        return
                    if elapsed_min >= 5 and ltp > state.hold_price:
                        self._exit(state, ltp, "TIME_EXIT_5MIN")
                        return

                # Hard time-based exit
                if elapsed_min >= _LIVE_FINAL_EXIT:
                    self._exit(state, ltp, f"HARD_EXIT_{_LIVE_FINAL_EXIT}MIN")

    def _enter(self, state: TradeState, ltp: float) -> None:
        """Record entry; set TP / SL / hold targets."""
        tp_pct = _LIVE_TP * (0.8 if state.score < _SCORE_HIGH_CONV else 1.0)

        if state.direction == "BUY":
            state.tp         = ltp * (1 + tp_pct)
            state.sl         = ltp * (1 - _LIVE_SL)
            state.hold_price = ltp * (1 + _LIVE_HOLD_MIN)
        else:   # SHORT
            state.tp         = ltp * (1 - tp_pct)
            state.sl         = ltp * (1 + _LIVE_SL)
            state.hold_price = ltp * (1 - _LIVE_HOLD_MIN)

        state.entry_price = ltp
        state.entry_ts    = time.time()
        state.entry_time  = datetime.now(IST)
        state.status      = ENTERED

        database.update_trade(state.trade_id, {
            "status":      ENTERED,
            "entry_price": ltp,
            "entry_time":  state.entry_time.isoformat(),
            "day_open":    state.day_open,
        })
        log.info(
            "[%s] ENTERED %s @ ₹%.2f | TP=₹%.2f  SL=₹%.2f",
            state.fyers_symbol, state.direction, ltp, state.tp, state.sl,
        )
        telegram_manager.send_entry(
            state.company_name, state.fyers_symbol, state.order_type,
            state.direction, ltp, state.tp, state.sl,
        )

    def _exit(self, state: TradeState, ltp: float, reason: str) -> None:
        """Record exit; persist to DB; send Telegram; unsubscribe."""
        if state.direction == "BUY":
            pnl_pct = (ltp - state.entry_price) / state.entry_price * 100
        else:
            pnl_pct = (state.entry_price - ltp) / state.entry_price * 100

        state.status   = EXITED
        exit_time      = datetime.now(IST)

        database.update_trade(state.trade_id, {
            "status":      EXITED,
            "exit_price":  ltp,
            "exit_time":   exit_time.isoformat(),
            "pnl_pct":     round(pnl_pct, 4),
            "exit_reason": reason,
        })
        sign = "+" if pnl_pct >= 0 else ""
        log.info(
            "[%s] EXITED %s @ ₹%.2f | P&L=%s%.2f%%  reason=%s",
            state.fyers_symbol, state.direction, ltp, sign, pnl_pct, reason,
        )

        # Route time-based exits to a distinct Telegram message
        is_time_exit = "TIME_EXIT" in reason or "HARD_EXIT" in reason or "MARKET_CLOSE" in reason
        if is_time_exit:
            telegram_manager.send_time_exit(
                state.company_name, state.fyers_symbol, state.order_type,
                state.direction, state.entry_price, ltp, pnl_pct, reason,
            )
        else:
            telegram_manager.send_exit(
                state.company_name, state.fyers_symbol, state.order_type,
                state.direction, state.entry_price, ltp, pnl_pct, reason,
            )

        # Unsubscribe from websocket
        if self._ws:
            try:
                self._ws.unsubscribe([state.fyers_symbol])
            except Exception as exc:
                log.warning("Unsubscribe failed for %s: %s", state.fyers_symbol, exc)

    # ── Add a new stock from notification ─────────────────────────────────────

    def add_stock(self, notification: dict) -> None:
        """
        Process one inbound notification.

        Expected keys:
            company_name   (str, required)
            symbol         (str, NSE or BSE ticker, optional if isin provided)
            isin           (str, preferred — fastest lookup)
            date           (str, YYYY-MM-DD — notification date; skip if not today)
            order_type     (str, e.g. "BULK_DEAL", "BLOCK_DEAL")
            order_value_cr (float, optional — block order size in ₹ Crore)
        """
        company    = notification.get("company_name", "Unknown")
        raw_symbol = notification.get("symbol", "")
        isin       = notification.get("isin", "")
        notif_date = notification.get("date", "")
        order_type = notification.get("order_type", "UNKNOWN")
        order_val  = float(notification.get("order_value_cr", 0.0))

        log.info(
            "─── Notification  company=%s  symbol=%s  isin=%s  date=%s  type=%s ───",
            company, raw_symbol, isin, notif_date, order_type,
        )

        # ── Date guard: skip anything not from today ──────────────────────────
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if notif_date and notif_date != today_str:
            log.info(
                "Skipping old notification for %s (notif_date=%s, today=%s)",
                company, notif_date, today_str,
            )
            return

        # ── Resolve ISIN → Fyers ticker ───────────────────────────────────────
        resolved_isin, fyers_sym = symbol_master.resolve(
            isin=isin or None,
            symbol=raw_symbol or None,
        )
        resolved_isin = resolved_isin or isin or ""

        if not fyers_sym:
            log.warning("Symbol not found for %s (ISIN=%s symbol=%s)", company, resolved_isin, raw_symbol)
            _db_skip(resolved_isin, None, company, raw_symbol, order_type, "SYMBOL_NOT_FOUND")
            telegram_manager.send_skip(
                company, raw_symbol or resolved_isin, resolved_isin, order_type,
                "Symbol not found in Fyers master",
            )
            return

        # ── Duplicate guard ───────────────────────────────────────────────────
        with self._trades_lock:
            existing = self._trades.get(fyers_sym)
        if existing and existing.status not in (EXITED, SKIP):
            log.info("Already tracking %s — ignoring duplicate notification", fyers_sym)
            return

        # ── Build EventData from Trendlyne ────────────────────────────────────
        row = trendlyne_manager.get_by_isin(resolved_isin)
        if not row and raw_symbol:
            row = trendlyne_manager.get_by_symbol(raw_symbol)
        ev = _build_event_data(row, order_value_cr=order_val)

        # ── Hard filter ───────────────────────────────────────────────────────
        skip, skip_reason = filter_event(ev)
        if skip:
            log.info("SKIP [%s] %s: %s", fyers_sym, company, skip_reason)
            _db_skip(resolved_isin, fyers_sym, company, raw_symbol, order_type, skip_reason)
            telegram_manager.send_skip(company, fyers_sym, resolved_isin, order_type, skip_reason)
            return

        # ── Score ─────────────────────────────────────────────────────────────
        score, direction = score_trade(ev)
        if direction == "SKIP":
            skip_reason = f"LOW_SCORE ({score}/100)"
            log.info("SKIP [%s] %s: %s", fyers_sym, company, skip_reason)
            _db_skip(resolved_isin, fyers_sym, company, raw_symbol, order_type, skip_reason,
                     confidence_score=score)
            telegram_manager.send_skip(company, fyers_sym, resolved_isin, order_type, skip_reason)
            return

        # ── Insert DB record ──────────────────────────────────────────────────
        notif_dt = datetime.now(IST)
        trade_id = database.insert_trade({
            "isin":            resolved_isin,
            "fyers_symbol":    fyers_sym,
            "company_name":    company,
            "raw_symbol":      raw_symbol,
            "order_type":      order_type,
            "notification_dt": notif_dt.isoformat(),
            "direction":       direction,
            "confidence_score": score,
            "skip_reason":     None,
            "status":          WAITING_ENTRY,
            "day_open":        None,
            "entry_price":     None,
            "entry_time":      None,
            "exit_price":      None,
            "exit_time":       None,
            "pnl_pct":         None,
            "exit_reason":     None,
        })

        state = TradeState(
            trade_id=trade_id,
            isin=resolved_isin,
            fyers_symbol=fyers_sym,
            company_name=company,
            raw_symbol=raw_symbol,
            order_type=order_type,
            direction=direction,
            score=score,
            notif_time=notif_dt,
        )

        with self._trades_lock:
            self._trades[fyers_sym] = state

        # ── Subscribe to WebSocket ────────────────────────────────────────────
        if self._ws and self._ws_authed.is_set():
            try:
                self._ws.subscribe([fyers_sym])
                log.info("Subscribed to WebSocket: %s", fyers_sym)
            except Exception as exc:
                log.error("subscribe() failed for %s: %s", fyers_sym, exc)
        else:
            log.warning("WS not ready — %s will be subscribed on next auth", fyers_sym)

        # ── Fetch current quote for reference price in Telegram ───────────────
        ref_price = self._fetch_ltp(fyers_sym) or 0.0

        log.info(
            "TRACKING [%s] %s → %s | score=%d | ref=₹%.2f",
            fyers_sym, company, direction, score, ref_price,
        )
        telegram_manager.send_tracking(
            company, fyers_sym, resolved_isin, order_type,
            direction, score, ref_price,
        )

    # ── Market close force-exit ───────────────────────────────────────────────

    def force_exit_all(self, reason: str = "MARKET_CLOSE") -> None:
        """Exit all currently ENTERED trades at current market price."""
        with self._trades_lock:
            active = [t for t in self._trades.values() if t.status == ENTERED]

        if not active:
            log.info("force_exit_all: no active trades to exit")
            return

        log.info("force_exit_all: exiting %d trade(s) — reason=%s", len(active), reason)
        for state in active:
            ltp = self._fetch_ltp(state.fyers_symbol) or state.last_ltp or state.entry_price
            if ltp:
                with state._lock:
                    if state.status == ENTERED:  # re-check inside lock
                        self._exit(state, ltp, reason)

    def _fetch_ltp(self, fyers_symbol: str) -> float | None:
        """Fetch current LTP via Fyers REST quotes API."""
        try:
            resp = self._fyers.quotes({"symbols": fyers_symbol})
            if resp.get("s") == "ok":
                d = resp.get("d", [{}])[0].get("v", {})
                return d.get("lp") or d.get("ltp") or d.get("ask")
        except Exception as exc:
            log.warning("quotes() failed for %s: %s", fyers_symbol, exc)
        return None

    def get_status_summary(self) -> dict:
        """Return a dict summarising active / exited trades for logging."""
        with self._trades_lock:
            trades = list(self._trades.values())
        return {
            "waiting": sum(1 for t in trades if t.status == WAITING_ENTRY),
            "entered": sum(1 for t in trades if t.status == ENTERED),
            "exited":  sum(1 for t in trades if t.status == EXITED),
            "skip":    sum(1 for t in trades if t.status == SKIP),
        }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _db_skip(
    isin: str,
    fyers_symbol: str | None,
    company_name: str,
    raw_symbol: str,
    order_type: str,
    skip_reason: str,
    confidence_score: int = 0,
) -> None:
    database.insert_trade({
        "isin":            isin,
        "fyers_symbol":    fyers_symbol,
        "company_name":    company_name,
        "raw_symbol":      raw_symbol,
        "order_type":      order_type,
        "notification_dt": datetime.now(IST).isoformat(),
        "direction":       SKIP,
        "confidence_score": confidence_score,
        "skip_reason":     skip_reason,
        "status":          SKIP,
        "day_open":        None,
        "entry_price":     None,
        "entry_time":      None,
        "exit_price":      None,
        "exit_time":       None,
        "pnl_pct":         None,
        "exit_reason":     None,
    })


def _build_event_data(row: dict | None, order_value_cr: float = 0.0) -> EventData:
    """Build EventData from a Trendlyne CSV row; all fields are optional."""

    def _f(key: str) -> float | None:
        try:
            v = (row or {}).get(key, "").strip()
            return float(v) if v else None
        except (ValueError, AttributeError):
            return None

    def _i(key: str) -> int | None:
        v = _f(key)
        return int(v) if v is not None else None

    return EventData(
        order_value_cr = order_value_cr,
        market_cap_cr  = _f("Market Capitalization"),
        rsi            = _f("Day RSI"),
        day_chg_pct    = _f("Day change %"),
        week_chg_pct   = _f("Week change %"),
        month_chg_pct  = _f("Month Change %"),
        vol_week_avg   = _i("Week Volume Avg"),
        vol_month_avg  = _i("Month Volume Avg"),
        price_15s_pct  = None,  # not available before entry — scored without it
        industry       = (row or {}).get("Industry Name", ""),
    )
