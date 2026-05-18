"""
database.py – Persistent storage via Turso HTTP API with local SQLite fallback.

Turso HTTP API needs only the 'requests' library (already a dependency).
No native binaries — works on Streamlit Cloud out of the box.

Set in st.secrets:
    turso_url   = "libsql://your-db.turso.io"
    turso_token = "your-auth-token"

If those secrets are absent, falls back to local leads.db (data lost on restart).
"""
import os
import sqlite3
import pandas as pd
import requests as _requests
from datetime import datetime
from typing import Optional, List

# ── Turso credentials ─────────────────────────────────────────────────────────
try:
    import streamlit as st
    _TURSO_URL   = st.secrets.get("turso_url",   "")
    _TURSO_TOKEN = st.secrets.get("turso_token",  "")
except Exception:
    _TURSO_URL   = os.environ.get("TURSO_URL",   "")
    _TURSO_TOKEN = os.environ.get("TURSO_TOKEN",  "")

_USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)
# libsql:// → https:// for the HTTP pipeline API
_TURSO_HTTP = _TURSO_URL.replace("libsql://", "https://") if _TURSO_URL else ""


# ─────────────────────────────────────────────────────────────────────────────
# Turso HTTP API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_turso_arg(v):
    """Convert a Python value to a Turso HTTP API argument object."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _from_turso_val(v: dict):
    """Convert a Turso HTTP API value object to a Python native type."""
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    return v.get("value")  # text / blob → str


def _turso_execute(sql: str, params=()) -> dict:
    """
    Execute one SQL statement via Turso HTTP pipeline API.
    Returns the 'result' dict:  {cols, rows, last_insert_rowid, affected_row_count}
    Raises Exception on SQL error or HTTP failure.
    """
    args = [_to_turso_arg(p) for p in params]
    payload = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": args}},
            {"type": "close"},
        ]
    }
    resp = _requests.post(
        f"{_TURSO_HTTP}/v2/pipeline",
        headers={
            "Authorization": f"Bearer {_TURSO_TOKEN}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    first = resp.json()["results"][0]
    if first["type"] == "error":
        raise Exception(first["error"]["message"])
    return first["response"]["result"]


# ─────────────────────────────────────────────────────────────────────────────
# Cursor / Connection shims — sqlite3-compatible interface over Turso HTTP
# ─────────────────────────────────────────────────────────────────────────────

class _TursoCursor:
    """Cursor-like object built from a Turso HTTP API result."""

    def __init__(self, result: dict):
        cols        = [c["name"] for c in result.get("cols", [])]
        raw_rows    = result.get("rows", [])
        self._rows  = [tuple(_from_turso_val(v) for v in row) for row in raw_rows]
        self.description = (
            [(c, None, None, None, None, None, None) for c in cols]
            if cols else None
        )
        last_id         = result.get("last_insert_rowid")
        self.lastrowid  = int(last_id) if last_id is not None else None
        self.rowcount   = result.get("affected_row_count", 0)
        self._pos       = 0

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows


class _TursoProxyCursor:
    """Cursor object returned by _TursoConn.cursor() — delegates execute() back."""

    def __init__(self, conn: "_TursoConn"):
        self._conn = conn
        self._cur: Optional[_TursoCursor] = None

    def execute(self, sql, params=()):
        self._cur = self._conn.execute(sql, params)
        return self

    def fetchone(self):
        return self._cur.fetchone() if self._cur else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur else []

    @property
    def description(self):
        return self._cur.description if self._cur else None

    @property
    def lastrowid(self):
        return self._cur.lastrowid if self._cur else None


class _TursoConn:
    """
    sqlite3-compatible connection backed by Turso HTTP API.
    Supports: execute(), cursor(), commit() (no-op), rollback() (no-op),
              close() (no-op), and 'with conn:' context manager.
    """

    def execute(self, sql, params=()):
        result = _turso_execute(sql, params)
        return _TursoCursor(result)

    def cursor(self):
        return _TursoProxyCursor(self)

    def commit(self):
        pass   # HTTP API is auto-commit per request

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False   # don't suppress exceptions


# ─────────────────────────────────────────────────────────────────────────────
# Public connection factory
# ─────────────────────────────────────────────────────────────────────────────

def get_conn():
    """Return a Turso HTTP connection, or a local SQLite connection as fallback."""
    if _USE_TURSO:
        return _TursoConn()
    return sqlite3.connect("leads.db", check_same_thread=False)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_sql(sql: str, conn, params=None) -> pd.DataFrame:
    """
    pandas-compatible SQL reader for both sqlite3 and _TursoConn.
    Replaces pd.read_sql_query() which doesn't recognise custom connection types.
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


def _is_unique_error(exc: Exception) -> bool:
    """Return True if exc is a UNIQUE constraint violation from either backend."""
    msg = str(exc).upper()
    return "UNIQUE" in msg or isinstance(exc, sqlite3.IntegrityError)


# ─────────────────────────────────────────────────────────────────────────────
# Schema init & migrations
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    """Create tables and apply any missing schema migrations."""
    with get_conn() as conn:
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

        # Migrations — silently skip if column already exists
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
                pass

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
    """Insert a lead. Returns True if new, False if duplicate email."""
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
    except Exception as e:
        if _is_unique_error(e):
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
    """Return the first lead with this email address, or None."""
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
    """Return leads due for a follow-up touch (1 = day 3, 2 = day 7)."""
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
    """Record a search. Returns the new row ID."""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO search_history (keyword, location, country, results_count, new_leads)
            VALUES (?, ?, ?, ?, ?)
        """, (keyword.strip(), location.strip(), country, results_count, new_leads))
        conn.commit()
        return cur.lastrowid


def update_search_result(search_id: int, results_count: int, new_leads: int):
    """Update result counts for a previously saved search."""
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
