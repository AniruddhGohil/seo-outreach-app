"""
database.py – Persistent storage via Turso (cloud SQLite) with local SQLite fallback.

Turso keeps data alive across Streamlit Cloud restarts (which wipe the local filesystem).
If turso_url / turso_token are not set in st.secrets, falls back to local leads.db.
"""
import os
import sqlite3
import pandas as pd
from datetime import datetime
from typing import Optional, List

# ── Turso credentials (from Streamlit secrets or env vars) ────────────────────
try:
    import streamlit as st
    _TURSO_URL   = st.secrets.get("turso_url",   "")
    _TURSO_TOKEN = st.secrets.get("turso_token",  "")
except Exception:
    _TURSO_URL   = os.environ.get("TURSO_URL",   "")
    _TURSO_TOKEN = os.environ.get("TURSO_TOKEN",  "")

_USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)

try:
    import libsql_experimental as libsql  # type: ignore
    _LIBSQL_OK = True
except ImportError:
    _LIBSQL_OK = False

# Local replica path — /tmp is always writable on Streamlit Cloud
_LOCAL_DB = "/tmp/leads.db" if _USE_TURSO else "leads.db"


# ─────────────────────────────────────────────────────────────────────────────
# Connection wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _SyncConn:
    """
    Wraps a libsql_experimental connection so it behaves like sqlite3:
    - Pulls fresh data from Turso on creation (conn.sync).
    - Pushes writes back to Turso on commit (conn.sync only when dirty).
    - Supports the 'with conn:' context manager — commits on success,
      rolls back on exception, closes on exit.
    """

    _WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")

    def __init__(self, raw):
        self._raw   = raw
        self._dirty = False

    # ── Forwarded methods ─────────────────────────────────────────────────────

    def execute(self, sql, params=()):
        cur = self._raw.execute(sql, params)
        if sql.strip().upper().startswith(self._WRITE_PREFIXES):
            self._dirty = True
        return cur

    def executemany(self, sql, seq):
        cur = self._raw.executemany(sql, seq)
        self._dirty = True
        return cur

    def cursor(self):
        return self._raw.cursor()

    # ── Commit / rollback / close ─────────────────────────────────────────────

    def commit(self):
        self._raw.commit()
        if self._dirty:
            self._raw.sync()   # push to Turso
            self._dirty = False

    def rollback(self):
        self._raw.rollback()
        self._dirty = False

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def get_conn():
    """Return a database connection — Turso (cloud) or local SQLite."""
    if _USE_TURSO and _LIBSQL_OK:
        raw = libsql.connect(_LOCAL_DB, sync_url=_TURSO_URL, auth_token=_TURSO_TOKEN)
        raw.sync()          # pull latest from Turso into local replica
        return _SyncConn(raw)
    return sqlite3.connect(_LOCAL_DB, check_same_thread=False)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_sql(sql: str, conn, params=None) -> pd.DataFrame:
    """
    pandas-compatible SQL reader that works with both sqlite3 and libsql.
    Replaces pd.read_sql_query() which may not recognise libsql connections.
    """
    cur = conn.execute(sql, params or [])
    if cur.description is None:
        return pd.DataFrame()
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def _row_to_dict(cursor, row) -> dict:
    """Convert a cursor row to a plain dict using column names."""
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _catch_operational(exc) -> bool:
    """Return True if exc is an OperationalError from either sqlite3 or libsql."""
    return isinstance(exc, (sqlite3.OperationalError, Exception)) and (
        "duplicate column" in str(exc).lower()
        or "already exists" in str(exc).lower()
        or type(exc).__name__ == "OperationalError"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schema init & migrations
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    """Create tables and apply any missing schema migrations."""
    with get_conn() as conn:
        # ── Leads table ───────────────────────────────────────────────────────
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

        # Migrations — fail silently if column already exists
        for col, typedef in [
            ("email_source",      "TEXT"),
            ("brevo_message_id",  "TEXT"),
            ("opened_at",         "TEXT"),
            ("clicked_at",        "TEXT"),
            ("followup1_sent_at", "TEXT"),
            ("followup2_sent_at", "TEXT"),
            ("email_template",    "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

        # ── Search history table ──────────────────────────────────────────────
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
    Insert a lead row.  Returns True if new, False if duplicate.
    Leads with no email are always inserted (no unique constraint on NULL).
    """
    status       = lead.get("status", "new")
    email        = lead.get("email") or None
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
        return False
    except Exception as e:
        if "UNIQUE" in str(e).upper() or "unique" in str(e).lower():
            return False
        return False


def is_duplicate_lead(website: str = "", phone: str = "") -> bool:
    """Return True if a lead with the same website OR phone already exists."""
    with get_conn() as conn:
        c = conn.cursor()
        if website and website.strip():
            c.execute("SELECT 1 FROM leads WHERE website = ? LIMIT 1", (website.strip(),))
            if c.fetchone():
                return True
        if phone and phone.strip():
            c.execute("SELECT 1 FROM leads WHERE phone = ? LIMIT 1", (phone.strip(),))
            if c.fetchone():
                return True
    return False


def get_lead_by_email(email: str) -> Optional[dict]:
    """Return the first lead row that has this email address, or None."""
    if not email or not email.strip():
        return None
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM leads WHERE email = ? LIMIT 1",
            (email.strip().lower(),),
        )
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None


def get_leads(status: Optional[str] = None) -> pd.DataFrame:
    with get_conn() as conn:
        if status and status != "all":
            return _read_sql(
                "SELECT * FROM leads WHERE status=? ORDER BY created_at DESC",
                conn, params=(status,),
            )
        return _read_sql("SELECT * FROM leads ORDER BY created_at DESC", conn)


def get_lead_by_id(lead_id: int) -> Optional[dict]:
    """Return a single lead row as a dict, or None if not found."""
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM leads WHERE id = ? LIMIT 1", (lead_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None


def set_leads_queued(lead_ids: List[int]):
    """Mark a batch of leads as 'queued' to prevent double-sends."""
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
        return _read_sql(
            """SELECT * FROM leads
               WHERE email IS NOT NULL AND email != '' AND status = ?
               ORDER BY created_at DESC""",
            conn, params=(status,),
        )


def update_status(lead_id: int, status: str,
                  brevo_message_id: str = "",
                  email_template: str = ""):
    with get_conn() as conn:
        conn.execute(
            """UPDATE leads
               SET status=?, email_sent_at=?, brevo_message_id=?, email_template=?
               WHERE id=?""",
            (status, datetime.now().isoformat(),
             brevo_message_id or None,
             email_template or None,
             lead_id),
        )
        conn.commit()


def update_tracking(lead_id: int, opened_at: str = "", clicked_at: str = ""):
    """Update open/click timestamps from Brevo polling data."""
    with get_conn() as conn:
        if opened_at:
            conn.execute(
                "UPDATE leads SET opened_at=? WHERE id=?", (opened_at, lead_id)
            )
        if clicked_at:
            conn.execute(
                "UPDATE leads SET clicked_at=? WHERE id=?", (clicked_at, lead_id)
            )
        conn.commit()


def record_followup(lead_id: int, touch: int):
    """Mark follow-up 1 or 2 as sent."""
    col = "followup1_sent_at" if touch == 1 else "followup2_sent_at"
    with get_conn() as conn:
        conn.execute(
            f"UPDATE leads SET {col}=? WHERE id=?",
            (datetime.now().isoformat(), lead_id),
        )
        conn.commit()


def get_leads_for_followup(touch: int = 1, days_after: int = 3) -> pd.DataFrame:
    """
    Return leads due for a follow-up:
    - touch=1: sent 3+ days ago, no follow-up 1 yet
    - touch=2: follow-up 1 sent 7+ days ago, no follow-up 2 yet
    """
    col      = "followup1_sent_at" if touch == 1 else "followup2_sent_at"
    prev_col = "email_sent_at"     if touch == 1 else "followup1_sent_at"
    with get_conn() as conn:
        return _read_sql(
            f"""SELECT * FROM leads
                WHERE status = 'sent'
                  AND {col} IS NULL
                  AND {prev_col} IS NOT NULL
                  AND datetime({prev_col}) <= datetime('now', '-{days_after} days')
                ORDER BY {prev_col} ASC""",
            conn,
        )


def delete_leads(lead_ids: List[int]):
    if not lead_ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" * len(lead_ids))
        conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", lead_ids)
        conn.commit()


def get_stats() -> dict:
    with get_conn() as conn:
        cur = conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")
        rows = cur.fetchall()
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
    Returns the row ID so counts can be updated with update_search_result().
    """
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO search_history (keyword, location, country, results_count, new_leads)
            VALUES (?, ?, ?, ?, ?)
        """, (keyword.strip(), location.strip(), country, results_count, new_leads))
        conn.commit()
        return cur.lastrowid


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
        return _read_sql(
            "SELECT * FROM search_history ORDER BY created_at DESC LIMIT ?",
            conn, params=(limit,),
        )


def delete_search_history():
    """Wipe all search history records."""
    with get_conn() as conn:
        conn.execute("DELETE FROM search_history")
        conn.commit()
