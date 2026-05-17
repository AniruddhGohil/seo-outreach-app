"""
bg_state.py – Persistent background email-sender state.

Imported as a regular Python module, so Python's module cache keeps
this alive across Streamlit reruns.  The background thread and the
Streamlit UI both read/write the STATE dict (protected by LOCK).

Usage:
    import bg_state
    with bg_state.LOCK:
        snap = dict(bg_state.STATE)   # read a thread-safe snapshot
"""
import threading

LOCK = threading.Lock()

# Reference to the running thread (for is_alive() checks)
_thread: threading.Thread | None = None

# Shared mutable state updated by the worker thread
STATE: dict = {
    "running":           False,   # True while thread is sending
    "cancel_requested":  False,   # set to True to stop after current email
    "total":             0,       # total emails queued in this batch
    "done":              0,       # emails attempted so far
    "sent":              0,       # successfully delivered
    "failed":            0,       # SMTP failures
    "current_biz":       "",      # business name being emailed right now
    "current_email":     "",      # recipient address being emailed right now
    "errors":            [],      # list of "email: error_msg" strings
    "started_at":        None,    # datetime string
    "finished_at":       None,    # datetime string (set when done/cancelled)
    "delay_secs":        120,     # delay between sends (for display)
}
