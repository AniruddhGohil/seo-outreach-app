"""
database.py – SQLite persistence layer for leads.
"""
import sqlite3
import pandas as pd
from datetime import datetime
from typing import Optional, List

DB_PATH = "leads.db"


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    """Create tables if they don't exist yet."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                business_name TEXT    NOT NULL,
                email         TEXT,
                phone         TEXT,
                website       TEXT,
                address       TEXT,
                city          TEXT,
                country       TEXT,
                keyword       TEXT,
                source        TEXT,
                status        TEXT    DEFAULT 'new',
                email_sent_at TEXT,
                created_at    TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_email
            ON leads(email) WHERE email IS NOT NULL AND email != ''
        """)
        conn.commit()


def insert_lead(lead: dict) -> bool:
    """
    Insert a lead row.  Returns True if it was new, False if it already existed.
    Leads with no email are always inserted (no unique constraint).
    """
    status = lead.get("status", "new")
    email  = lead.get("email") or None   # normalise empty string → None

    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO leads
                  (business_name, email, phone, website, address,
                   city, country, keyword, source, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                lead.get("business_name"), email,
                lead.get("phone"),         lead.get("website"),
                lead.get("address"),       lead.get("city"),
                lead.get("country"),       lead.get("keyword"),
                lead.get("source"),        status,
            ))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        # Duplicate email
        return False
    except Exception:
        return False


def get_leads(status: Optional[str] = None) -> pd.DataFrame:
    with get_conn() as conn:
        if status and status != "all":
            return pd.read_sql_query(
                "SELECT * FROM leads WHERE status=? ORDER BY created_at DESC",
                conn, params=(status,)
            )
        return pd.read_sql_query(
            "SELECT * FROM leads ORDER BY created_at DESC", conn
        )


def get_leads_with_email(status: str = "new") -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(
            """SELECT * FROM leads
               WHERE email IS NOT NULL AND email != '' AND status = ?
               ORDER BY created_at DESC""",
            conn, params=(status,)
        )


def update_status(lead_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET status=?, email_sent_at=? WHERE id=?",
            (status, datetime.now().isoformat(), lead_id)
        )
        conn.commit()


def delete_leads(lead_ids: List[int]):
    if not lead_ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" * len(lead_ids))
        conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", lead_ids)
        conn.commit()


def get_stats() -> dict:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")
        rows = c.fetchall()
    stats: dict = {"total": 0}
    for status, count in rows:
        stats[status] = count
        stats["total"] += count
    return stats
