"""
database.py — SQLite with WAL mode for live paper-trade records.

One row per notification processed (SKIP, WAITING_ENTRY, ENTERED, EXITED).
The tracker updates the row in-place as the trade progresses.
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "live_trades.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                -- notification payload
                isin             TEXT,
                fyers_symbol     TEXT,
                company_name     TEXT,
                raw_symbol       TEXT,
                order_type       TEXT,
                notification_dt  TEXT,
                -- decision
                direction        TEXT,      -- BUY | SHORT | SKIP
                confidence_score INTEGER,
                skip_reason      TEXT,
                -- trade lifecycle
                status           TEXT,      -- WAITING_ENTRY | ENTERED | EXITED | SKIP
                day_open         REAL,
                entry_price      REAL,
                entry_time       TEXT,
                exit_price       REAL,
                exit_time        TEXT,
                pnl_pct          REAL,
                exit_reason      TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_status ON live_trades(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_date   ON live_trades(notification_dt)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_isin   ON live_trades(isin)")
    log.info("Database ready: %s", DB_PATH)


def insert_trade(trade: dict) -> int:
    """Insert a new trade row and return its id."""
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO live_trades (
                isin, fyers_symbol, company_name, raw_symbol, order_type,
                notification_dt, direction, confidence_score, skip_reason,
                status, day_open, entry_price, entry_time,
                exit_price, exit_time, pnl_pct, exit_reason
            ) VALUES (
                :isin, :fyers_symbol, :company_name, :raw_symbol, :order_type,
                :notification_dt, :direction, :confidence_score, :skip_reason,
                :status, :day_open, :entry_price, :entry_time,
                :exit_price, :exit_time, :pnl_pct, :exit_reason
            )
        """, trade)
        return cur.lastrowid


def update_trade(trade_id: int, updates: dict) -> None:
    """Partially update an existing trade row."""
    if not updates:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    params = dict(updates, _id=trade_id)
    # rename 'id' key to avoid conflict with WHERE clause
    with _conn() as conn:
        conn.execute(
            f"UPDATE live_trades SET {set_clause} WHERE id = :_id",
            params,
        )


def get_today_trades(date_str: str) -> list[dict]:
    """Return all trade rows where notification_dt starts with date_str."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM live_trades WHERE notification_dt LIKE ? ORDER BY id",
            (f"{date_str}%",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_active_trades() -> list[dict]:
    """Return all trades that are not yet EXITED or SKIP (for recovery on restart)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM live_trades WHERE status NOT IN ('EXITED', 'SKIP') ORDER BY id",
        ).fetchall()
    return [dict(r) for r in rows]
