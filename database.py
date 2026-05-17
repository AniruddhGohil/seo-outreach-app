"""
database.py – SQLite persistence layer for leads and search history.
Schema version: 3  (search_history table, get_lead_by_email helper)
"""
import sqlite3
import pandas as pd
from datetime import datetime
from typing import Optional, List

DB_PATH = "leads.db"


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    """Create tables and apply any missing schema migrations."""
    with get_conn() as conn:
        # ── Leads table ──────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                business_name TEXT    NOT NULL,
                email         TEXT,
                email_source  TEXT,
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
        # Migration v2: add email_source column to existing databases
        try:
            conn.execute("ALTER TABLE leads ADD COLUMN email_source TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

        # ── Search history table ─────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword        TEXT NOT NULL,
                location       TEXT NOT NULL,
                country        TEXT NOT NULL,
                results_count  INTEGER DEFAULT 0,
                new_leads      INTEGER DEFAULT 0,
                created_at     TEXT DEFAULT (datetime('now'))
            )
        """)

        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Leads
# ─────────────────────────────────────────────────────────────────────────────

def insert_lead(lead: dict) -> bool:
    """
    Insert a lead row.  Returns True if it was new, False if it already existed.
    Leads with no email are always inserted (no unique constraint on email).
    """
    status       = lead.get("status", "new")
    email        = lead.get("email") or None        # normalise empty string → None
    email_source = lead.get("email_source") or None

    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO leads
                  (business_name, email, email_source, phone, website, address,
                   city, country, keyword, source, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                lead.get("business_name"), email, email_source,
                lead.get("phone"),         lead.get("website"),
                lead.get("address"),       lead.get("city"),
                lead.get("country"),       lead.get("keyword"),
                lead.get("source"),        status,
            ))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        # Duplicate email – lead already exists
        return False
    except Exception:
        return False


def is_duplicate_lead(website: str = "", phone: str = "") -> bool:
    """
    Return True if a lead with the same website OR phone already exists in the DB.
    Used to skip scraping / email look-up for businesses already stored.
    """
    with get_conn() as conn:
        c = conn.cursor()
        if website and website.strip():
            c.execute(
                "SELECT 1 FROM leads WHERE website = ? LIMIT 1",
                (website.strip(),),
            )
            if c.fetchone():
                return True
        if phone and phone.strip():
            c.execute(
                "SELECT 1 FROM leads WHERE phone = ? LIMIT 1",
                (phone.strip(),),
            )
            if c.fetchone():
                return True
    return False


def get_lead_by_email(email: str) -> Optional[dict]:
    """
    Return the first lead row that has this email address, or None.
    Used to detect email-level duplicates before inserting a new lead.
    """
    if not email or not email.strip():
        return None
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            "SELECT * FROM leads WHERE email = ? LIMIT 1",
            (email.strip().lower(),),
        )
        row = c.fetchone()
        return dict(row) if row else None


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


def get_lead_by_id(lead_id: int) -> Optional[dict]:
    """Return a single lead row as a dict, or None if not found."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM leads WHERE id = ? LIMIT 1", (lead_id,))
        row = c.fetchone()
        return dict(row) if row else None


def set_leads_queued(lead_ids: List[int]):
    """
    Mark a batch of leads as 'queued' so the background sender can pick them up.
    They are removed from the 'new' queue immediately, preventing double-sends.
    """
    if not lead_ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" * len(lead_ids))
        conn.execute(
            f"UPDATE leads SET status='queued' WHERE id IN ({placeholders})",
            lead_ids,
        )
        conn.commit()


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


# ─────────────────────────────────────────────────────────────────────────────
# Search history
# ─────────────────────────────────────────────────────────────────────────────

def save_search(keyword: str, location: str, country: str,
                results_count: int = 0, new_leads: int = 0) -> int:
    """
    Record a search query in history.
    Returns the row ID so counts can be updated afterwards with update_search_result().
    """
    with get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO search_history (keyword, location, country, results_count, new_leads)
            VALUES (?, ?, ?, ?, ?)
        """, (keyword.strip(), location.strip(), country, results_count, new_leads))
        conn.commit()
        return cursor.lastrowid


def update_search_result(search_id: int, results_count: int, new_leads: int):
    """Update the result counts for a previously saved search."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE search_history SET results_count=?, new_leads=? WHERE id=?
        """, (results_count, new_leads, search_id))
        conn.commit()


def get_search_history(limit: int = 30) -> pd.DataFrame:
    """Return recent searches, most recent first."""
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT * FROM search_history ORDER BY created_at DESC LIMIT ?
        """, conn, params=(limit,))


def delete_search_history():
    """Wipe all search history records."""
    with get_conn() as conn:
        conn.execute("DELETE FROM search_history")
        conn.commit()
