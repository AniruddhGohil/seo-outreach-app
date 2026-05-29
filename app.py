"""
app.py – SEO Outreach Engine
Streamlit web app: find SMB leads → extract emails → send cold outreach.
"""
import base64
import io
import json
import random
import threading
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_oauth import OAuth2Component

import bg_state
from database import (
    delete_leads, get_lead_by_email, get_lead_by_id, get_leads,
    get_leads_for_followup, get_leads_with_email, get_search_history,
    get_stats, init_db, insert_lead, is_duplicate_lead, record_followup,
    save_search, set_leads_queued, update_search_result, update_status,
    update_tracking, delete_search_history, test_connection,
)
from email_finder import find_email_on_website
import brevo_sender
from email_sender import send_email as _smtp_send_one
from scraper import COUNTRY_SCRAPERS, find_businesses, scrape_ecommerce
from templates import (
    EMAIL_TEMPLATE_HTML, SUBJECT_LINES,
    TEMPLATE_OPTIONS, FOLLOWUP_OPTIONS,
    build_html, build_text,
    get_random_subject, get_followup_subject,
    DEFAULT_PORTFOLIO_URL, DEFAULT_CASE_STUDY,
)


# ─────────────────────────────────────────────────────────────────────────────
# Background email sender
# ─────────────────────────────────────────────────────────────────────────────

def _background_send_worker(
    lead_ids: list,
    sender_email: str,
    app_password: str,
    sender_name: str,
    delay_secs: int,
    template: str = "short",
    brevo_key: str = "",
    is_followup: int = 0,     # 0 = first touch, 1 = follow-up 1, 2 = follow-up 2
    portfolio_url: str = "",
    case_study: str = "",
    plain_text_mode: bool = False,
):
    """
    Daemon thread: sends one email per lead_id via Brevo (preferred) or Gmail SMTP.
    Respects cancel_requested flag between sends.
    """
    use_brevo = bool(brevo_key)

    for i, lead_id in enumerate(lead_ids):
        with bg_state.LOCK:
            if bg_state.STATE["cancel_requested"]:
                break

        lead = get_lead_by_id(lead_id)
        if not lead or not lead.get("email"):
            with bg_state.LOCK:
                bg_state.STATE["done"] += 1
            continue

        biz_name  = lead.get("business_name", "")
        recipient = lead["email"]

        with bg_state.LOCK:
            bg_state.STATE["current_biz"]   = biz_name
            bg_state.STATE["current_email"] = recipient

        # ── Build email content from template ─────────────────────────────
        tpl_key = f"followup{is_followup}" if is_followup else template
        html_body = build_html(tpl_key, biz_name, sender_name, sender_email,
                               portfolio_url=portfolio_url, case_study=case_study)
        text_body = build_text(tpl_key, biz_name, sender_name, sender_email,
                               portfolio_url=portfolio_url, case_study=case_study)
        subject   = (get_followup_subject(biz_name, is_followup)
                     if is_followup else get_random_subject(biz_name, tpl_key))

        # ── Send via Brevo or Gmail SMTP ──────────────────────────────────
        message_id = ""
        if use_brevo:
            ok, result = brevo_sender.send_email(
                api_key=brevo_key,
                sender_email=sender_email,
                sender_name=sender_name,
                recipient_email=recipient,
                recipient_name=biz_name,
                subject=subject,
                html_content=html_body,
                text_content=text_body,
                plain_text_mode=plain_text_mode,
            )
            if ok:
                message_id = result
                # Sync contact to Brevo CRM only in HTML mode.
                # In plain-text mode, adding a contact to a Brevo list creates
                # bulk-sender metadata that leaks to Gmail and pushes into Promotions.
                if not plain_text_mode:
                    try:
                        brevo_sender.upsert_contact(
                            brevo_key, recipient,
                            attributes={"COMPANY": biz_name,
                                        "CITY": lead.get("city", ""),
                                        "KEYWORD": lead.get("keyword", "")},
                        )
                    except Exception:
                        pass
        else:
            ok, result = _smtp_send_one(
                sender_email=sender_email,
                app_password=app_password,
                recipient_email=recipient,
                business_name=biz_name,
                sender_name=sender_name,
            )

        # ── Update DB ─────────────────────────────────────────────────────
        if is_followup:
            if ok:
                record_followup(lead_id, is_followup)
            # Don't change status for follow-ups (already 'sent')
        else:
            update_status(
                lead_id,
                "sent" if ok else "failed",
                brevo_message_id=message_id,
                email_template=tpl_key,
            )

        with bg_state.LOCK:
            bg_state.STATE["done"] += 1
            if ok:
                bg_state.STATE["sent"] += 1
            else:
                bg_state.STATE["failed"] += 1
                bg_state.STATE["errors"].append(
                    f"{recipient} ({biz_name}): {result}"
                )

        # ── Inter-email delay (chunked for cancellation responsiveness) ───
        if i < len(lead_ids) - 1:
            jitter = random.randint(-10, 10)
            wait   = max(15, delay_secs + jitter)
            for _ in range(wait * 2):
                time.sleep(0.5)
                with bg_state.LOCK:
                    if bg_state.STATE["cancel_requested"]:
                        break

    with bg_state.LOCK:
        bg_state.STATE["running"]       = False
        bg_state.STATE["current_biz"]   = ""
        bg_state.STATE["current_email"] = ""
        bg_state.STATE["finished_at"]   = datetime.now().strftime("%d %b %Y  %H:%M:%S")


def _queue_and_send(lead_ids: list, sender_email: str,
                    app_password: str, sender_name: str,
                    delay_secs: int, template: str = "short",
                    brevo_key: str = "", is_followup: int = 0,
                    portfolio_url: str = "", case_study: str = "",
                    plain_text_mode: bool = False) -> bool:
    """
    Mark leads as 'queued' in the DB, then start the background thread.
    Returns False if a send is already running.
    """
    with bg_state.LOCK:
        if bg_state.STATE["running"]:
            return False
        # Reset state for new batch
        bg_state.STATE.update({
            "running":          True,
            "cancel_requested": False,
            "total":            len(lead_ids),
            "done":             0,
            "sent":             0,
            "failed":           0,
            "current_biz":      "",
            "current_email":    "",
            "errors":           [],
            "started_at":       datetime.now().strftime("%d %b %Y  %H:%M:%S"),
            "finished_at":      None,
            "delay_secs":       delay_secs,
        })

    if not is_followup:
        set_leads_queued(lead_ids)   # move from 'new' → 'queued' immediately

    t = threading.Thread(
        target=_background_send_worker,
        args=(lead_ids, sender_email, app_password, sender_name,
              delay_secs, template, brevo_key, is_followup,
              portfolio_url, case_study, plain_text_mode),
        daemon=True,
        name="email-sender",
    )
    bg_state._thread = t
    t.start()
    return True

# ─────────────────────────────────────────────────────────────────────────────
# Page config  (must be first Streamlit call)
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SEO Outreach Engine",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Google OAuth login gate
# ─────────────────────────────────────────────────────────────────────────────
_GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/auth"
_GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _decode_id_token(id_token: str) -> dict:
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


import hashlib as _hashlib

def _session_token(email: str, secret: str) -> str:
    """Deterministic token — same email+secret always gives same token."""
    return _hashlib.sha256(f"{email}:{secret}".encode()).hexdigest()[:32]


def _login_page() -> bool:
    # ── 1. Already authenticated this session ─────────────────────────────
    if st.session_state.get("_authenticated"):
        return True

    # ── 2. Restore from URL query param (persists across refreshes) ────────
    if not st.session_state.get("_signed_out"):
        try:
            _qp        = st.query_params
            _qt        = _qp.get("_t", "")
            _qe        = _qp.get("_e", "")
            _qn        = _qp.get("_n", "")
            _secret    = st.secrets.get("auth_secret", "seo-outreach-default-secret")
            ALLOWED_EMAILS = [e.strip().lower()
                              for e in st.secrets.get("allowed_emails", [])]
            if (_qt and _qe and _qe.lower() in ALLOWED_EMAILS
                    and _qt == _session_token(_qe.lower(), _secret)):
                st.session_state["_authenticated"] = True
                st.session_state["_user_email"]    = _qe.lower()
                st.session_state["_user_name"]     = _qn or _qe
                return True
        except Exception:
            pass

    try:
        CLIENT_ID      = st.secrets["google_client_id"]
        CLIENT_SECRET  = st.secrets["google_client_secret"]
        REDIRECT_URI   = st.secrets["redirect_uri"]
        ALLOWED_EMAILS = [e.strip().lower() for e in st.secrets["allowed_emails"]]
    except KeyError as exc:
        st.error(f"⚠️ Missing secret key: **{exc}**. Go to Streamlit Cloud → Settings → Secrets.")
        return False

    # ── Login page CSS ────────────────────────────────────────────────────────
    st.markdown("""
    <style>
        #MainMenu, footer, header,
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] { display: none !important; }

        html, body, .stApp { background: #f1f5f9 !important; }
        .block-container { max-width: 480px !important; padding: 10vh 1rem 2rem !important; }

        /* ── OAuth / Google button ── */
        [data-testid="stBaseButton-secondary"],
        [data-testid="stLinkButton"] a,
        .stLinkButton a,
        .stButton > button {
            background: white !important;
            border: 1.5px solid #e2e8f0 !important;
            border-radius: 12px !important;
            color: #1e293b !important;
            font-size: 15px !important;
            font-weight: 600 !important;
            padding: 12px 20px !important;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08) !important;
            width: 100% !important;
            transition: box-shadow .15s, border-color .15s !important;
        }
        [data-testid="stBaseButton-secondary"]:hover,
        .stButton > button:hover {
            box-shadow: 0 4px 16px rgba(0,0,0,0.12) !important;
            border-color: #a5b4fc !important;
        }
        /* ensure iframe/component inside card is visible */
        iframe { display: block !important; opacity: 1 !important; }
    </style>
    """, unsafe_allow_html=True)

    # ── Branding card ─────────────────────────────────────────────────────────
    st.markdown("""
    <div style="background:white;border-radius:20px;
                border:1px solid #e2e8f0;
                box-shadow:0 4px 24px rgba(0,0,0,0.08);
                padding:40px 36px 32px;text-align:center;margin-bottom:4px;">

      <div style="font-size:40px;margin-bottom:16px;">🚀</div>

      <div style="font-size:22px;font-weight:800;color:#0f172a;
                  letter-spacing:-0.5px;margin-bottom:6px;
                  font-family:'Inter','Segoe UI',sans-serif;">
        SEO Outreach Engine
      </div>
      <div style="font-size:13px;color:#94a3b8;margin-bottom:28px;">
        Find leads · Extract emails · Close clients
      </div>

      <div style="height:1px;background:#f1f5f9;margin:0 -36px 24px;"></div>

      <div style="font-size:11px;font-weight:700;color:#64748b;
                  text-transform:uppercase;letter-spacing:1px;margin-bottom:20px;">
        Sign in with Google to continue
      </div>

    </div>
    """, unsafe_allow_html=True)

    # ── OAuth button rendered directly — no nested columns ────────────────────
    oauth2 = OAuth2Component(
        CLIENT_ID, CLIENT_SECRET,
        _GOOGLE_AUTH_URL,
        _GOOGLE_TOKEN_URL, _GOOGLE_TOKEN_URL,
        _GOOGLE_REVOKE_URL,
    )
    result = oauth2.authorize_button(
        name="Continue with Google",
        redirect_uri=REDIRECT_URI,
        scope="openid email profile",
        icon="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg",
        use_container_width=True,
        key="google_login_btn",
    )

    st.markdown("""
    <div style="text-align:center;margin-top:20px;">
      <span style="font-size:11px;color:#94a3b8;">🔒 OAuth 2.0 · Google verified · Private access</span>
    </div>
    """, unsafe_allow_html=True)

    if result and "token" in result:
        user_info = _decode_id_token(result["token"].get("id_token", ""))
        email     = user_info.get("email", "").lower()
        name      = user_info.get("name", email)

        if not email:
            st.error("Could not retrieve your email from Google. Please try again.")
            return False

        if email in ALLOWED_EMAILS:
            st.session_state["_authenticated"] = True
            st.session_state["_user_email"]    = email
            st.session_state["_user_name"]     = name
            # Persist login across refreshes via URL query params (no cookies needed)
            try:
                _secret = st.secrets.get("auth_secret", "seo-outreach-default-secret")
                st.query_params["_t"] = _session_token(email, _secret)
                st.query_params["_e"] = email
                st.query_params["_n"] = name
            except Exception:
                pass
            st.rerun(scope="app")
        else:
            st.error(f"🚫 Access denied for `{email}`. This account has not been granted access.")

    return False


if not _login_page():
    st.stop()

init_db()

# ─────────────────────────────────────────────────────────────────────────────
# Cached sidebar API calls (TTL = 5 min) — prevents every button click from
# making slow external HTTP calls to Brevo / Serper / Turso DB.
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _cached_brevo_stats(api_key: str) -> dict:
    try:
        return brevo_sender.get_today_stats(api_key) or {}
    except Exception:
        return {}

@st.cache_data(ttl=300, show_spinner=False)
def _cached_serper_credits(api_key: str) -> dict:
    try:
        from scraper import get_serper_credits as _gsc
        return _gsc(api_key) or {}
    except Exception:
        return {}

@st.cache_data(ttl=60, show_spinner=False)
def _cached_db_status() -> tuple:
    try:
        return test_connection()
    except Exception:
        return (False, "Connection error", "unknown")

# ─────────────────────────────────────────────────────────────────────────────
# Design system – global CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* ── Reset & base ── */
html, body, [class*="css"] {
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif !important;
}
.stApp { background: #f1f5f9 !important; }
.block-container {
    padding-top: 16px !important;
    padding-bottom: 60px !important;
    max-width: 1200px !important;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background: #0f172a !important;
    border-right: 1px solid #1e293b !important;
}
[data-testid="stSidebar"] > div { background: #0f172a !important; }
[data-testid="stSidebar"] * { color: #94a3b8 !important; }

[data-testid="stSidebar"] .stTextInput input {
    background: #1e293b !important;
    border: 1px solid #334155 !important;
    color: #e2e8f0 !important;
    border-radius: 6px !important;
    font-size: 13px !important;
    padding: 8px 12px !important;
}
[data-testid="stSidebar"] .stTextInput input:focus {
    border-color: #4f46e5 !important;
    box-shadow: 0 0 0 2px rgba(79,70,229,.15) !important;
}
[data-testid="stSidebar"] .stTextInput label {
    font-size: 11px !important; font-weight: 600 !important;
    text-transform: uppercase !important; letter-spacing: 0.7px !important;
    color: #475569 !important;
}
[data-testid="stSidebar"] .stButton > button {
    background: #1e293b !important; border: 1px solid #334155 !important;
    color: #94a3b8 !important; border-radius: 6px !important;
    font-size: 13px !important; font-weight: 500 !important;
    width: 100% !important; transition: all .12s !important;
    padding: 7px 12px !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: #334155 !important; color: #e2e8f0 !important;
    border-color: #475569 !important;
}
[data-testid="stSidebar"] hr { border-color: #1e293b !important; margin: 10px 0 !important; }
[data-testid="stSidebar"] .stExpander {
    background: #1e293b !important; border: 1px solid #334155 !important;
    border-radius: 8px !important;
}
[data-testid="stSidebar"] .stExpander summary {
    color: #94a3b8 !important; font-size: 13px !important;
}
[data-testid="stSidebar"] .stSlider { padding: 0 !important; }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 0;
    background: white;
    border: none;
    border-bottom: 2px solid #e2e8f0;
    border-radius: 0;
    padding: 0;
    margin-bottom: 28px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 0 !important;
    padding: 12px 22px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    color: #64748b !important;
    background: transparent !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    margin-bottom: -2px !important;
    transition: color .15s, border-color .15s !important;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #4f46e5 !important;
}
.stTabs [aria-selected="true"] {
    color: #4f46e5 !important;
    background: transparent !important;
    border-bottom-color: #4f46e5 !important;
    box-shadow: none !important;
}

/* ── Primary button ── */
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #4f46e5, #6366f1) !important;
    border: none !important;
    border-radius: 8px !important; font-weight: 600 !important;
    font-size: 14px !important; color: white !important;
    padding: 10px 22px !important; letter-spacing: -0.1px !important;
    transition: all .15s !important;
    box-shadow: 0 2px 6px rgba(79,70,229,.35), inset 0 1px 0 rgba(255,255,255,.15) !important;
}
.stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #4338ca, #4f46e5) !important;
    box-shadow: 0 6px 16px rgba(79,70,229,.4) !important;
    transform: translateY(-1px) !important;
}
.stButton > button[kind="secondary"] {
    background: white !important; border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important; font-weight: 500 !important;
    font-size: 13px !important; color: #374151 !important;
    transition: all .15s !important;
    box-shadow: 0 1px 2px rgba(0,0,0,.04) !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: #c7d2fe !important; background: #f5f3ff !important;
    color: #4f46e5 !important;
}

/* ── Inputs ── */
.stTextInput input, .stNumberInput input {
    border-radius: 8px !important; border: 1px solid #e2e8f0 !important;
    font-size: 14px !important; padding: 9px 12px !important;
    transition: border-color .12s !important;
}
.stTextInput input:focus, .stNumberInput input:focus {
    border-color: #4f46e5 !important;
    box-shadow: 0 0 0 3px rgba(79,70,229,.12) !important;
}
div[data-baseweb="select"] > div {
    border-radius: 8px !important; border-color: #e2e8f0 !important;
    font-size: 14px !important;
}

/* ── Selectbox / dropdowns ── */
div[data-baseweb="select"] > div:first-child {
    border: 1px solid #e2e8f0 !important; border-radius: 8px !important;
}

/* ── Equal-height stat cards ── */
[data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    display: flex !important;
    flex-direction: column !important;
}
[data-testid="stHorizontalBlock"] > [data-testid="stColumn"] > div:first-child {
    flex: 1 !important;
    display: flex !important;
    flex-direction: column !important;
}
[data-testid="stHorizontalBlock"] > [data-testid="stColumn"] > div:first-child > div {
    flex: 1 !important;
}

/* ── Dataframe ── */
[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0 !important; border-radius: 12px !important;
    overflow: hidden !important; box-shadow: 0 2px 8px rgba(0,0,0,.04) !important;
}

/* ── Alerts ── */
.stAlert { border-radius: 10px !important; border: none !important; }
[data-testid="stAlert"] { border-radius: 10px !important; }

/* ── Expander ── */
.stExpander {
    border: 1px solid #e2e8f0 !important; border-radius: 12px !important;
    box-shadow: 0 1px 4px rgba(0,0,0,.04) !important;
    overflow: hidden !important;
}
.stExpander summary {
    font-weight: 600 !important; font-size: 14px !important;
    color: #374151 !important;
}

/* ── Progress bar ── */
[data-testid="stProgressBar"] > div > div {
    background: #4f46e5 !important; border-radius: 99px !important;
}

/* ── Slider ── */
[data-testid="stSlider"] [role="slider"] { background: #4f46e5 !important; }
[data-testid="stSlider"] [data-testid="stSlider"] > div > div > div {
    background: #e0e7ff !important;
}

/* ── Checkbox ── */
[data-testid="stCheckbox"] label { font-size: 13px !important; font-weight: 500 !important; color: #374151 !important; }

/* ── Toggle ── */
[data-testid="stToggle"] label { font-size: 13px !important; font-weight: 500 !important; }

/* ── Radio ── */
[data-testid="stRadio"] label { font-size: 13px !important; font-weight: 500 !important; }

/* ── Metric (native Streamlit) polish ── */
[data-testid="stMetric"] { background: white; border-radius: 12px; padding: 16px 18px;
    border: 1px solid #e2e8f0; box-shadow: 0 2px 6px rgba(0,0,0,.05); }

/* ── Textarea ── */
.stTextArea textarea {
    border-radius: 10px !important; border: 1px solid #e2e8f0 !important;
    font-size: 13px !important; font-family: inherit !important;
    transition: border-color .15s !important;
}
.stTextArea textarea:focus {
    border-color: #4f46e5 !important;
    box-shadow: 0 0 0 3px rgba(79,70,229,.12) !important;
}

/* ── Scrollbar (webkit) ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #f1f5f9; }
::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 99px; }
::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

/* ── Hide Streamlit chrome ── */
#MainMenu, footer, header,
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"] { display: none !important; }

/* ── Sidebar collapse/expand button — always visible ── */
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"] {
    display: flex !important;
    opacity: 1 !important;
    visibility: visible !important;
    background: #1e293b !important;
    border: 1px solid #334155 !important;
    border-radius: 8px !important;
    box-shadow: 0 2px 8px rgba(0,0,0,.3) !important;
    transition: background .15s !important;
}
[data-testid="collapsedControl"]:hover,
[data-testid="stSidebarCollapsedControl"]:hover {
    background: #334155 !important;
    border-color: #4f46e5 !important;
}
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapsedControl"] svg {
    fill: #94a3b8 !important;
    color: #94a3b8 !important;
}

/* ── Prevent page dimming / blur during Streamlit reruns ── */
/*
  Streamlit JS sets inline style="opacity:0.3" on stMain during reruns.
  Inline styles beat CSS !important in normal cascade.
  BUT: CSS animation values override inline styles per the CSS cascade spec
  (animations sit above the author origin, which includes inline styles).
  So a continuously-running keyframe that forces opacity:1 wins every time.
*/
@keyframes _keepOpaque { 0%, 100% { opacity: 1; filter: none; } }

[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] > section,
[data-testid="stMain"],
[data-testid="stMainBlockContainer"],
[data-testid="stVerticalBlock"],
[data-testid="stVerticalBlockBorderWrapper"],
section[tabindex="0"] {
    animation: _keepOpaque 1ms step-end infinite !important;
    pointer-events: auto !important;
    filter: none !important;
}

/* Ensure buttons and interactive elements are always clickable */
.stButton, .stButton > button,
.stSelectbox, .stTextInput, .stNumberInput,
.stCheckbox, .stRadio, .stToggle, .stSlider,
[data-testid="stTabs"], [data-baseweb="tab"],
[data-testid="stSidebar"] * {
    pointer-events: auto !important;
    position: relative;
}

/* Hide the Streamlit "running" spinner/status only — not progress bars we use */
div[class*="StatusWidget"],
[data-testid="stStatusWidget"] { display: none !important; }
</style>
""", unsafe_allow_html=True)

# (No JS iframe needed — CSS @keyframes animation handles anti-dim correctly)

# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section(title: str, subtitle: str = ""):
    sub = (f'<p style="font-size:13px;color:#64748b;margin:4px 0 0;font-weight:400;">'
           f'{subtitle}</p>') if subtitle else ""
    st.markdown(
        f'<div style="margin-bottom:24px;padding-left:12px;'
        f'border-left:3px solid #4f46e5;border-radius:0 2px 2px 0;">'
        f'<h3 style="font-size:17px;font-weight:700;color:#0f172a;margin:0;'
        f'letter-spacing:-0.3px;">{title}</h3>{sub}</div>',
        unsafe_allow_html=True,
    )


def _card(content_html: str):
    st.markdown(
        f'<div style="background:white;border-radius:14px;padding:24px 26px;'
        f'border:1px solid #e2e8f0;margin-bottom:16px;'
        f'box-shadow:0 2px 8px rgba(0,0,0,.05);">{content_html}</div>',
        unsafe_allow_html=True,
    )


def _stat_card(label: str, value, color: str, sub: str = "", icon: str = ""):
    sub_html = (
        f'<div style="font-size:11px;color:#94a3b8;margin-top:6px;font-weight:500;">'
        f'{sub}</div>'
    ) if sub else ""
    icon_bubble = (
        f'<div style="position:absolute;top:18px;right:18px;width:38px;height:38px;'
        f'border-radius:10px;background:{color}20;display:flex;align-items:center;'
        f'justify-content:center;font-size:19px;line-height:1;">{icon}</div>'
    ) if icon else ""
    st.markdown(
        f'<div style="background:white;border-radius:14px;padding:22px 20px 18px 20px;'
        f'border:1px solid #e2e8f0;box-shadow:0 2px 8px rgba(0,0,0,.06);'
        f'position:relative;overflow:hidden;min-height:112px;height:100%;'
        f'box-sizing:border-box;">'
        # Coloured top accent bar
        f'<div style="position:absolute;top:0;left:0;right:0;height:3px;'
        f'background:{color};border-radius:14px 14px 0 0;"></div>'
        f'{icon_bubble}'
        f'<div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:0.8px;margin-bottom:8px;padding-top:4px;">{label}</div>'
        f'<div style="font-size:30px;font-weight:800;color:#0f172a;line-height:1;'
        f'letter-spacing:-1.5px;">{value}</div>'
        f'{sub_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="background:{bg};color:{color};border-radius:5px;'
        f'padding:2px 8px;font-size:11px;font-weight:600;">{text}</span>'
    )


STATUS_BADGE = {
    "new":      _badge("New",      "#1d4ed8", "#eff6ff"),
    "sent":     _badge("Sent",     "#15803d", "#f0fdf4"),
    "failed":   _badge("Failed",   "#dc2626", "#fef2f2"),
    "no_email": _badge("No email", "#92400e", "#fffbeb"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    # Wordmark
    st.markdown(
        "<div style='padding:20px 0 14px;border-bottom:1px solid #1e293b;margin-bottom:14px;'>"
        "<div style='display:flex;align-items:center;gap:10px;'>"
        "<div style='width:32px;height:32px;"
        "background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:9px;"
        "display:flex;align-items:center;justify-content:center;font-size:15px;"
        "box-shadow:0 2px 8px rgba(79,70,229,.4);flex-shrink:0;'>🚀</div>"
        "<div>"
        "<div style='font-size:14px;font-weight:700;color:#f1f5f9;letter-spacing:-0.4px;'>"
        "SEO Outreach</div>"
        "<div style='font-size:10px;color:#475569;letter-spacing:0.5px;text-transform:uppercase;'>"
        "Cold Email Engine</div>"
        "</div></div></div>",
        unsafe_allow_html=True,
    )

    # User pill
    user_name  = st.session_state.get("_user_name", "")
    user_email = st.session_state.get("_user_email", "")
    initials   = "".join(w[0].upper() for w in user_name.split()[:2]) if user_name else "?"
    st.markdown(
        f"<div style='background:#1e293b;border:1px solid #334155;border-radius:10px;"
        f"padding:10px 12px;margin:8px 0;display:flex;align-items:center;gap:10px;'>"
        f"<div style='width:32px;height:32px;border-radius:50%;"
        f"background:linear-gradient(135deg,#2563eb,#4f46e5);"
        f"display:flex;align-items:center;justify-content:center;"
        f"font-size:12px;font-weight:700;color:white;flex-shrink:0;"
        f"box-shadow:0 2px 6px rgba(79,70,229,.35);'>{initials}</div>"
        f"<div style='min-width:0;flex:1;'>"
        f"<div style='font-size:12px;font-weight:600;color:#e2e8f0;"
        f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{user_name}</div>"
        f"<div style='font-size:11px;color:#64748b;white-space:nowrap;"
        f"overflow:hidden;text-overflow:ellipsis;'>{user_email}</div>"
        f"</div></div>",
        unsafe_allow_html=True,
    )
    if st.button("Sign out", use_container_width=True):
        # Clear URL query params so refresh doesn't restore the session
        try:
            st.query_params.clear()
        except Exception:
            pass
        # Clear session and mark signed_out to block query-param restore
        st.session_state.clear()
        st.session_state["_signed_out"] = True
        st.rerun(scope="app")

    # ── Database connection status ────────────────────────────────────────
    _db_ok, _db_msg, _db_backend = _cached_db_status()
    if _db_ok:
        st.markdown(
            "<div style='background:#052e16;border:1px solid #166534;"
            "border-radius:10px;padding:10px 12px;margin:8px 0;'>"
            "<div style='font-size:10px;font-weight:700;color:#4ade80;"
            "letter-spacing:0.6px;text-transform:uppercase;'>🗄️ Turso · Data Safe</div>"
            "<div style='font-size:11px;color:#86efac;margin-top:3px;'>"
            "Leads persist across restarts</div></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='background:#450a0a;border:1px solid #991b1b;"
            f"border-radius:10px;padding:10px 12px;margin:8px 0;'>"
            f"<div style='font-size:10px;font-weight:700;color:#f87171;"
            f"letter-spacing:0.6px;text-transform:uppercase;'>⚠️ Local SQLite · At Risk</div>"
            f"<div style='font-size:11px;color:#fca5a5;margin-top:3px;'>"
            f"{_db_msg}</div></div>",
            unsafe_allow_html=True,
        )

    # ── Background send indicator ─────────────────────────────────────────
    with bg_state.LOCK:
        _snap = dict(bg_state.STATE)
    if _snap["running"]:
        _pct = int(_snap["done"] / _snap["total"] * 100) if _snap["total"] else 0
        st.markdown(
            f"<div style='background:#052e16;border:1px solid #166534;border-radius:8px;"
            f"padding:10px 12px;margin:8px 0;'>"
            f"<div style='font-size:11px;font-weight:700;color:#4ade80;"
            f"letter-spacing:0.5px;margin-bottom:4px;'>📤 SENDING IN BACKGROUND</div>"
            f"<div style='font-size:12px;color:#86efac;'>"
            f"{_snap['sent']} sent · {_snap['failed']} failed · "
            f"{_snap['total'] - _snap['done']} remaining</div>"
            f"<div style='background:#166534;border-radius:99px;height:4px;"
            f"margin-top:8px;overflow:hidden;'>"
            f"<div style='width:{_pct}%;height:4px;background:#4ade80;'></div>"
            f"</div></div>",
            unsafe_allow_html=True,
        )

    # ── Daily Brevo budget tracker ────────────────────────────────────────
    _brevo_sidebar = st.secrets.get("brevo_key", "") or st.session_state.get("s_brevo", "")
    if _brevo_sidebar:
        try:
            _today = _cached_brevo_stats(_brevo_sidebar)
            _used  = int(_today.get("requests", 0))
            _limit = 300
            _left  = max(0, _limit - _used)
            _pct   = min(100, int(_used / _limit * 100))
            _bar_color = "#4ade80" if _pct < 70 else ("#facc15" if _pct < 90 else "#f87171")
            st.markdown(
                f"<div style='background:#0f2027;border:1px solid #1e3a4a;"
                f"border-radius:8px;padding:10px 12px;margin:6px 0;'>"
                f"<div style='display:flex;justify-content:space-between;"
                f"align-items:center;margin-bottom:6px;'>"
                f"<span style='font-size:11px;font-weight:700;color:#94a3b8;"
                f"letter-spacing:0.5px;'>📬 DAILY EMAIL BUDGET</span>"
                f"<span style='font-size:11px;font-weight:700;"
                f"color:{_bar_color};'>{_used}/{_limit}</span></div>"
                f"<div style='background:#1e293b;border-radius:99px;height:5px;"
                f"overflow:hidden;margin-bottom:6px;'>"
                f"<div style='width:{_pct}%;height:5px;"
                f"background:{_bar_color};border-radius:99px;'></div></div>"
                f"<div style='display:flex;justify-content:space-between;"
                f"align-items:center;'>"
                f"<span style='font-size:11px;color:#475569;'>"
                f"{_left} remaining today</span>"
                f"<span style='font-size:10px;color:#4ade80;"
                f"background:rgba(74,222,128,0.12);border:1px solid rgba(74,222,128,0.25);"
                f"border-radius:4px;padding:2px 7px;"
                f"font-weight:600;letter-spacing:0.3px;'>Brevo free tier</span>"
                f"</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        except Exception:
            pass

    # ── Serper credit tracker ─────────────────────────────────────────────
    _serper_sidebar = st.secrets.get("serper_key", "") or st.session_state.get("s_serper", "")
    if _serper_sidebar:
        try:
            _sc = _cached_serper_credits(_serper_sidebar)
            if _sc:
                _sc_left  = int(_sc.get("credits", 0))
                _sc_limit = 2500   # Serper free tier
                _sc_used  = max(0, _sc_limit - _sc_left)
                _sc_pct   = min(100, int(_sc_used / _sc_limit * 100))
                _sc_color = "#4ade80" if _sc_pct < 70 else ("#facc15" if _sc_pct < 90 else "#f87171")
                st.markdown(
                    f"<div style='background:#0f2027;border:1px solid #1e3a4a;"
                    f"border-radius:10px;padding:10px 12px;margin:8px 0;'>"
                    f"<div style='display:flex;justify-content:space-between;"
                    f"align-items:center;margin-bottom:6px;'>"
                    f"<span style='font-size:11px;font-weight:700;color:#94a3b8;"
                    f"letter-spacing:0.5px;'>🔍 SERPER CREDITS</span>"
                    f"<span style='font-size:11px;font-weight:700;"
                    f"color:{_sc_color};'>{_sc_left:,} left</span></div>"
                    f"<div style='background:#1e293b;border-radius:99px;height:5px;"
                    f"overflow:hidden;margin-bottom:6px;'>"
                    f"<div style='width:{_sc_pct}%;height:5px;"
                    f"background:{_sc_color};border-radius:99px;'></div></div>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                    f"<span style='font-size:11px;color:#475569;'>"
                    f"{_sc_used:,} used of {_sc_limit:,}</span>"
                    f"<span style='font-size:10px;color:#60a5fa;"
                    f"background:rgba(96,165,250,0.12);border:1px solid rgba(96,165,250,0.25);"
                    f"border-radius:4px;padding:2px 7px;"
                    f"font-weight:600;letter-spacing:0.3px;'>Serper free tier</span>"
                    f"</div></div>",
                    unsafe_allow_html=True,
                )
        except Exception:
            pass

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
    st.divider()

    # ── Gmail SMTP ────────────────────────────────────────────────────────
    st.markdown("<div style='font-size:11px;font-weight:700;color:#475569;"
                "text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px;'>"
                "Gmail SMTP</div>", unsafe_allow_html=True)

    _def_email = st.secrets.get("smtp_email",    "")
    _def_name  = st.secrets.get("smtp_name",     "")
    _def_pass  = st.secrets.get("smtp_password", "")

    sender_email = st.text_input("From address",   value=_def_email, placeholder="you@gmail.com", key="s_email")
    app_password = st.text_input("App password",   value=_def_pass,  type="password", key="s_pass",
                                 help="Google → Security → App Passwords")
    sender_name  = st.text_input("Display name",   value=_def_name,  placeholder="John Smith", key="s_name")

    st.divider()

    # ── API Keys ──────────────────────────────────────────────────────────
    st.markdown("<div style='font-size:11px;font-weight:700;color:#475569;"
                "text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px;'>"
                "API Keys</div>", unsafe_allow_html=True)

    _def_serper  = st.secrets.get("serper_key",        "")
    _def_fsq     = st.secrets.get("foursquare_key",    "")
    _def_gplaces = st.secrets.get("google_places_key", "")
    _def_yelp    = st.secrets.get("yelp_api_key",      "")

    with st.expander("Serper (Google Maps) — Free" + (" ✓" if _def_serper else ""), expanded=False):
        serper_key = st.text_input("API Key", value=_def_serper, type="password", key="s_serper")
        if not _def_serper:
            st.caption("Get free key at serper.dev")

    with st.expander("Foursquare — Free", expanded=False):
        foursquare_key = st.text_input("API Key", value=_def_fsq, type="password", key="s_fsq")

    with st.expander("Google Places — Optional", expanded=False):
        google_places_key = st.text_input("API Key", value=_def_gplaces, type="password", key="s_gplaces")

    with st.expander("Yelp — Optional", expanded=False):
        yelp_key = st.text_input("API Key", value=_def_yelp, type="password", key="s_yelp")

    with st.expander("Brevo — Free (recommended)" +
                     (" ✓" if st.secrets.get("brevo_key","") else ""), expanded=False):
        _def_brevo = st.secrets.get("brevo_key", "")
        brevo_key  = st.text_input("Brevo API Key", value=_def_brevo,
                                   type="password", key="s_brevo",
                                   help="app.brevo.com → Settings → API Keys (free)")
        _def_brevo_name = st.secrets.get("brevo_sender_name", "Aniruddh Gohil")
        brevo_sender_name = st.text_input(
            "Sender name (business emails)",
            value=_def_brevo_name,
            placeholder="Aniruddh Gohil",
            key="s_brevo_name",
            help="Name shown to recipients in their inbox when sent via Brevo",
        )
        if brevo_key:
            _acct = brevo_sender.validate_key(brevo_key)
            if _acct:
                _plan = next(
                    (p.get("type","") for p in _acct.get("plan",[]) if p.get("type")),
                    "free",
                )
                st.success(f"✓ Connected · {_acct.get('email','')} · {_plan}")
            else:
                st.error("Invalid key — check and retry")
        else:
            st.caption("300 emails/day free · open & click tracking included")
            st.caption("serper.dev → API Keys → create key")

    st.divider()

    # ── Rate limiting ─────────────────────────────────────────────────────
    st.markdown("<div style='font-size:11px;font-weight:700;color:#475569;"
                "text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px;'>"
                "Rate Limiting</div>", unsafe_allow_html=True)
    delay_sec = st.slider("Delay between emails (s)", 30, 180, 60)
    st.caption("60 s is safe with Brevo (dedicated infrastructure). Gmail SMTP: use 90s+.")

    st.divider()

    # ── Social proof (appears in every email sent) ────────────────────────
    st.markdown("<div style='font-size:11px;font-weight:700;color:#475569;"
                "text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px;'>"
                "Email Social Proof</div>", unsafe_allow_html=True)
    portfolio_url = st.text_input(
        "Portfolio URL",
        value=st.secrets.get("portfolio_url", DEFAULT_PORTFOLIO_URL),
        placeholder="https://yoursite.com",
        key="s_portfolio",
        help="Linked as 'View my work →' in every email signature",
    )
    case_study = st.text_input(
        "Case study line (1 sentence)",
        value=st.secrets.get("case_study", DEFAULT_CASE_STUDY),
        placeholder=DEFAULT_CASE_STUDY,
        key="s_case_study",
        help="Shown as 'Recent result:' in all templates. Edit to match your best result.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _smtp_ready() -> bool:
    return bool(st.session_state.get("s_email")
                and st.session_state.get("s_pass")
                and st.session_state.get("s_name"))


# ─────────────────────────────────────────────────────────────────────────────
# Top bar
# ─────────────────────────────────────────────────────────────────────────────
stats   = get_stats()
total   = stats.get("total",   0)
sent    = stats.get("sent",    0)
ready   = stats.get("new",     0)
queued  = stats.get("queued",  0)
failed  = stats.get("failed",  0)

# ── Title row ─────────────────────────────────────────────────────────────────
st.markdown(
    "<div style='padding:4px 0 20px;'>"
    "<div style='font-size:22px;font-weight:800;color:#0f172a;letter-spacing:-0.5px;'>"
    "SEO Outreach Engine</div>"
    "<div style='font-size:13px;color:#94a3b8;margin-top:3px;font-weight:400;'>"
    "Find leads &nbsp;·&nbsp; Extract emails &nbsp;·&nbsp; Close clients</div>"
    "</div>",
    unsafe_allow_html=True,
)

# ── Metric cards row ──────────────────────────────────────────────────────────
m1, m2, m3, m4, m5 = st.columns(5)
with m1: _stat_card("Total Leads",    total,  "#4f46e5")
with m2: _stat_card("Emails Sent",    sent,   "#10b981")
with m3: _stat_card("In Queue",       queued, "#0ea5e9")
with m4: _stat_card("Ready to Send",  ready,  "#f59e0b")
with m5: _stat_card("Failed",         failed, "#ef4444")

st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tab_find, tab_db, tab_send, tab_followup, tab_analytics = st.tabs([
    "Find Leads",
    "Leads Database",
    "Send Emails",
    "Follow-ups",
    "Analytics",
])

# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 – Find Leads
# ═════════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_find():
    _section("Find Leads",
             "Search Google Maps for businesses, then visit each website to extract contact emails.")

    # ── Search history ────────────────────────────────────────────────────────
    _hist_df = get_search_history(limit=30)
    if not _hist_df.empty:
        with st.expander(f"🕒 Recent searches ({len(_hist_df)})", expanded=False):
            # Pivot for display
            _disp = _hist_df[["keyword", "location", "country",
                               "results_count", "new_leads", "created_at"]].copy()
            _disp.columns = ["Keyword", "Location", "Country",
                             "Businesses found", "New leads saved", "Searched at"]
            _disp["Searched at"] = pd.to_datetime(
                _disp["Searched at"]).dt.strftime("%d %b %Y  %H:%M")
            st.dataframe(_disp, use_container_width=True, hide_index=True, height=220)
            # Group to show which keyword+location combos already searched
            _combos = (
                _hist_df.groupby(["keyword", "location", "country"])
                .agg(times=("id", "count"), last_at=("created_at", "max"),
                     total_leads=("new_leads", "sum"))
                .reset_index()
                .sort_values("last_at", ascending=False)
            )
            st.markdown(
                "<p style='font-size:12px;color:#9ca3af;margin:8px 0 4px;'>"
                "Unique keyword × location combinations searched so far:</p>",
                unsafe_allow_html=True,
            )
            for _, row in _combos.iterrows():
                st.markdown(
                    f"<span style='font-size:13px;'>🔍 <b>{row['keyword']}</b> "
                    f"· {row['location']}, {row['country']} "
                    f"<span style='color:#9ca3af;'>— searched {row['times']}× "
                    f"· {int(row['total_leads'])} new leads total</span></span>",
                    unsafe_allow_html=True,
                )
            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
            if st.button("🗑️ Clear search history", use_container_width=False):
                delete_search_history()
                st.rerun(scope="app")

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

    # ── Batch Search ──────────────────────────────────────────────────────────
    # ── Batch search templates ────────────────────────────────────────────────

    _HIGH_VALUE_UK = """\
HVAC engineer, Birmingham, United Kingdom
HVAC engineer, Manchester, United Kingdom
HVAC engineer, Leeds, United Kingdom
HVAC engineer, Bristol, United Kingdom
HVAC engineer, Sheffield, United Kingdom
roofing contractor, Birmingham, United Kingdom
roofing contractor, Manchester, United Kingdom
roofing contractor, Leeds, United Kingdom
roofing contractor, Bristol, United Kingdom
roofing contractor, Sheffield, United Kingdom
personal injury solicitor, Birmingham, United Kingdom
personal injury solicitor, Manchester, United Kingdom
personal injury solicitor, Leeds, United Kingdom
personal injury solicitor, Bristol, United Kingdom
personal injury solicitor, Liverpool, United Kingdom
funeral director, Birmingham, United Kingdom
funeral director, Manchester, United Kingdom
funeral director, Leeds, United Kingdom
funeral director, Bristol, United Kingdom
funeral director, Sheffield, United Kingdom
chiropractor, Birmingham, United Kingdom
chiropractor, Manchester, United Kingdom
chiropractor, Leeds, United Kingdom
chiropractor, Bristol, United Kingdom
chiropractor, Liverpool, United Kingdom
emergency plumber, Birmingham, United Kingdom
emergency plumber, Manchester, United Kingdom
emergency plumber, Leeds, United Kingdom
emergency plumber, Bristol, United Kingdom
emergency plumber, Sheffield, United Kingdom
landscape gardener, Birmingham, United Kingdom
landscape gardener, Manchester, United Kingdom
landscape gardener, Leeds, United Kingdom
landscape gardener, Bristol, United Kingdom
landscape gardener, Sheffield, United Kingdom
garden maintenance, Birmingham, United Kingdom
garden maintenance, Manchester, United Kingdom
garden maintenance, Leeds, United Kingdom
garden maintenance, Bristol, United Kingdom
garden maintenance, Liverpool, United Kingdom
rubbish removal, Birmingham, United Kingdom
rubbish removal, Manchester, United Kingdom
rubbish removal, Leeds, United Kingdom
rubbish removal, Bristol, United Kingdom
rubbish removal, Sheffield, United Kingdom
water tank installation, Birmingham, United Kingdom
water tank installation, Manchester, United Kingdom
water tank installation, Leeds, United Kingdom
water tank installation, Bristol, United Kingdom
water tank installation, Sheffield, United Kingdom"""

    _HIGH_VALUE_LONDON = """\
HVAC engineer, N1 Islington, United Kingdom
HVAC engineer, SW4 Clapham, United Kingdom
HVAC engineer, E2 Bethnal Green, United Kingdom
HVAC engineer, SE1 Bermondsey, United Kingdom
HVAC engineer, W4 Chiswick, United Kingdom
roofing contractor, CR0 Croydon, United Kingdom
roofing contractor, BR2 Bromley, United Kingdom
roofing contractor, HA3 Harrow, United Kingdom
roofing contractor, E17 Walthamstow, United Kingdom
roofing contractor, RM1 Romford, United Kingdom
personal injury solicitor, CR0 Croydon, United Kingdom
personal injury solicitor, E17 Walthamstow, United Kingdom
personal injury solicitor, UB1 Southall, United Kingdom
personal injury solicitor, IG1 Ilford, United Kingdom
personal injury solicitor, DA1 Dartford, United Kingdom
funeral director, N17 Tottenham, United Kingdom
funeral director, SE25 South Norwood, United Kingdom
funeral director, SW16 Streatham, United Kingdom
funeral director, RM1 Romford, United Kingdom
funeral director, IG1 Ilford, United Kingdom
chiropractor, SW4 Clapham, United Kingdom
chiropractor, N1 Islington, United Kingdom
chiropractor, W4 Chiswick, United Kingdom
chiropractor, SE1 Bermondsey, United Kingdom
chiropractor, E2 Bethnal Green, United Kingdom
emergency plumber, CR0 Croydon, United Kingdom
emergency plumber, BR2 Bromley, United Kingdom
emergency plumber, HA3 Harrow, United Kingdom
emergency plumber, E17 Walthamstow, United Kingdom
emergency plumber, RM1 Romford, United Kingdom
landscape gardener, SW16 Streatham, United Kingdom
landscape gardener, W4 Chiswick, United Kingdom
landscape gardener, N1 Islington, United Kingdom
landscape gardener, BR2 Bromley, United Kingdom
landscape gardener, KT1 Kingston, United Kingdom
rubbish removal, CR0 Croydon, United Kingdom
rubbish removal, E17 Walthamstow, United Kingdom
rubbish removal, IG1 Ilford, United Kingdom
rubbish removal, UB1 Southall, United Kingdom
rubbish removal, DA1 Dartford, United Kingdom
water tank installation, N1 Islington, United Kingdom
water tank installation, SW4 Clapham, United Kingdom
water tank installation, W4 Chiswick, United Kingdom
water tank installation, SE1 Bermondsey, United Kingdom
water tank installation, E2 Bethnal Green, United Kingdom"""

    _UK_TRADES = """\
emergency plumber, Birmingham, United Kingdom
boiler repair, Manchester, United Kingdom
electrician, Leeds, United Kingdom
roofer, Sheffield, United Kingdom
builder, Nottingham, United Kingdom
gas engineer, Leicester, United Kingdom
pest control, Glasgow, United Kingdom
removals company, Bristol, United Kingdom
locksmith, Edinburgh, United Kingdom
carpet cleaning, Cardiff, United Kingdom
handyman, Liverpool, United Kingdom
drainage engineer, Newcastle, United Kingdom
tree surgeon, Southampton, United Kingdom
damp proofing, Portsmouth, United Kingdom
window cleaner, Norwich, United Kingdom"""

    _UK_PROFESSIONALS = """\
mortgage broker, Leeds, United Kingdom
personal injury solicitor, Sheffield, United Kingdom
private dentist, Bristol, United Kingdom
physiotherapist, Edinburgh, United Kingdom
accountant, Birmingham, United Kingdom
estate agent, Manchester, United Kingdom
driving instructor, Nottingham, United Kingdom
cosmetic clinic, Glasgow, United Kingdom
immigration solicitor, Leicester, United Kingdom
financial advisor, Cardiff, United Kingdom
family solicitor, Liverpool, United Kingdom
private GP, Newcastle, United Kingdom
chiropractor, Southampton, United Kingdom
optician, Portsmouth, United Kingdom
will writer, Norwich, United Kingdom"""

    _UK_HEALTHCARE = """\
private dentist, Birmingham, United Kingdom
orthodontist, Manchester, United Kingdom
chiropractor, Leeds, United Kingdom
physiotherapist, Bristol, United Kingdom
osteopath, Sheffield, United Kingdom
private GP, Nottingham, United Kingdom
cosmetic dentist, Glasgow, United Kingdom
skin clinic, Edinburgh, United Kingdom
hair transplant clinic, Leicester, United Kingdom
weight loss clinic, Cardiff, United Kingdom
audiologist, Liverpool, United Kingdom
podiatrist, Newcastle, United Kingdom
fertility clinic, Southampton, United Kingdom
laser eye surgery, Portsmouth, United Kingdom
sports physio, Norwich, United Kingdom"""

    _UK_BEAUTY = """\
hair salon, Birmingham, United Kingdom
nail salon, Manchester, United Kingdom
beauty salon, Leeds, United Kingdom
laser hair removal, Bristol, United Kingdom
microblading, Sheffield, United Kingdom
lash extensions, Nottingham, United Kingdom
spray tan, Glasgow, United Kingdom
teeth whitening, Edinburgh, United Kingdom
eyebrow threading, Leicester, United Kingdom
semi permanent makeup, Cardiff, United Kingdom
botox clinic, Liverpool, United Kingdom
filler clinic, Newcastle, United Kingdom
massage therapist, Southampton, United Kingdom
tanning salon, Portsmouth, United Kingdom
waxing salon, Norwich, United Kingdom"""

    _LONDON_TRADES = """\
emergency plumber, CR0 Croydon, United Kingdom
boiler repair, BR2 Bromley, United Kingdom
electrician, HA3 Harrow, United Kingdom
roofer, E17 Walthamstow, United Kingdom
builder, SW16 Streatham, United Kingdom
gas engineer, SE25 South Norwood, United Kingdom
pest control, RM1 Romford, United Kingdom
locksmith, N17 Tottenham, United Kingdom
carpet cleaning, UB1 Southall, United Kingdom
removals company, IG1 Ilford, United Kingdom
handyman, DA1 Dartford, United Kingdom
drainage engineer, KT1 Kingston, United Kingdom
tree surgeon, EN1 Enfield, United Kingdom
damp proofing, TW3 Hounslow, United Kingdom
window cleaner, SM1 Sutton, United Kingdom"""

    _LONDON_PROFESSIONALS = """\
mortgage broker, HA3 Harrow, United Kingdom
personal injury solicitor, CR0 Croydon, United Kingdom
private dentist, BR2 Bromley, United Kingdom
physiotherapist, SW16 Streatham, United Kingdom
accountant, IG1 Ilford, United Kingdom
estate agent, E17 Walthamstow, United Kingdom
immigration solicitor, UB1 Southall, United Kingdom
cosmetic clinic, N22 Wood Green, United Kingdom
driving instructor, SE25 South Norwood, United Kingdom
financial advisor, RM1 Romford, United Kingdom
family solicitor, DA1 Dartford, United Kingdom
chiropractor, KT1 Kingston, United Kingdom
private GP, EN1 Enfield, United Kingdom
will writer, TW3 Hounslow, United Kingdom
optician, SM1 Sutton, United Kingdom"""

    _LONDON_BEAUTY = """\
hair salon, CR0 Croydon, United Kingdom
nail salon, E17 Walthamstow, United Kingdom
beauty salon, HA3 Harrow, United Kingdom
laser hair removal, UB1 Southall, United Kingdom
lash extensions, IG1 Ilford, United Kingdom
microblading, N22 Wood Green, United Kingdom
teeth whitening, SW16 Streatham, United Kingdom
botox clinic, BR2 Bromley, United Kingdom
spray tan, SE25 South Norwood, United Kingdom
semi permanent makeup, RM1 Romford, United Kingdom
waxing salon, DA1 Dartford, United Kingdom
massage therapist, KT1 Kingston, United Kingdom
eyebrow threading, TW3 Hounslow, United Kingdom
tanning salon, SM1 Sutton, United Kingdom
skin clinic, EN1 Enfield, United Kingdom"""

    # ── USA batch templates ───────────────────────────────────────────────────
    _USA_TRADES = """\
HVAC contractor, Houston, United States
HVAC contractor, Phoenix, United States
HVAC contractor, Atlanta, United States
HVAC contractor, Nashville, United States
HVAC contractor, Charlotte, United States
plumber, Dallas, United States
plumber, Indianapolis, United States
plumber, Columbus, United States
plumber, Louisville, United States
plumber, Memphis, United States
roofing contractor, Denver, United States
roofing contractor, Oklahoma City, United States
roofing contractor, Tulsa, United States
roofing contractor, Omaha, United States
roofing contractor, Raleigh, United States
electrician, Richmond, United States
electrician, Baltimore, United States
electrician, Milwaukee, United States
junk removal, Houston, United States
junk removal, Atlanta, United States
garage door repair, Phoenix, United States
garage door repair, Nashville, United States
tree service, Charlotte, United States
tree service, Raleigh, United States
pest control, Memphis, United States"""

    _USA_PROFESSIONALS = """\
personal injury attorney, Houston, United States
personal injury attorney, Atlanta, United States
personal injury attorney, Phoenix, United States
personal injury attorney, Nashville, United States
personal injury attorney, Charlotte, United States
divorce attorney, Dallas, United States
divorce attorney, Indianapolis, United States
divorce attorney, Columbus, United States
divorce attorney, Memphis, United States
divorce attorney, Louisville, United States
DUI attorney, Denver, United States
DUI attorney, Baltimore, United States
DUI attorney, Oklahoma City, United States
workers compensation attorney, Houston, United States
workers compensation attorney, Atlanta, United States
bankruptcy attorney, Phoenix, United States
bankruptcy attorney, Nashville, United States
immigration attorney, Dallas, United States
immigration attorney, Charlotte, United States
tax accountant, Houston, United States
financial advisor, Atlanta, United States
financial advisor, Indianapolis, United States
mortgage broker, Columbus, United States
real estate agent, Raleigh, United States
insurance agent, Richmond, United States"""

    _USA_HEALTHCARE = """\
dentist, Houston, United States
dentist, Phoenix, United States
dentist, Atlanta, United States
dentist, Nashville, United States
dentist, Charlotte, United States
orthodontist, Dallas, United States
orthodontist, Indianapolis, United States
orthodontist, Columbus, United States
chiropractor, Denver, United States
chiropractor, Louisville, United States
chiropractor, Memphis, United States
physical therapist, Houston, United States
physical therapist, Atlanta, United States
optometrist, Phoenix, United States
optometrist, Nashville, United States
urgent care clinic, Dallas, United States
urgent care clinic, Charlotte, United States
dermatologist, Houston, United States
dermatologist, Raleigh, United States
weight loss clinic, Atlanta, United States
weight loss clinic, Phoenix, United States
hearing aid clinic, Indianapolis, United States
podiatrist, Columbus, United States
pain management clinic, Houston, United States
cosmetic surgeon, Nashville, United States"""

    _USA_BEAUTY = """\
hair salon, Houston, United States
hair salon, Phoenix, United States
hair salon, Atlanta, United States
nail salon, Dallas, United States
nail salon, Nashville, United States
nail salon, Charlotte, United States
barber shop, Houston, United States
barber shop, Atlanta, United States
lash extensions, Phoenix, United States
lash extensions, Dallas, United States
microblading, Houston, United States
microblading, Nashville, United States
med spa, Atlanta, United States
med spa, Charlotte, United States
botox clinic, Houston, United States
botox clinic, Phoenix, United States
laser hair removal, Dallas, United States
laser hair removal, Raleigh, United States
teeth whitening, Houston, United States
teeth whitening, Atlanta, United States
waxing salon, Phoenix, United States
spray tan, Nashville, United States
permanent makeup, Charlotte, United States
eyebrow threading, Dallas, United States
hair extensions, Houston, United States"""

    _HIGH_VALUE_USA = """\
HVAC contractor, Houston, United States
HVAC contractor, Phoenix, United States
HVAC contractor, Atlanta, United States
HVAC contractor, Dallas, United States
HVAC contractor, Nashville, United States
roofing contractor, Houston, United States
roofing contractor, Phoenix, United States
roofing contractor, Denver, United States
roofing contractor, Atlanta, United States
roofing contractor, Charlotte, United States
personal injury attorney, Houston, United States
personal injury attorney, Atlanta, United States
personal injury attorney, Dallas, United States
personal injury attorney, Phoenix, United States
personal injury attorney, Nashville, United States
dentist, Houston, United States
dentist, Phoenix, United States
dentist, Dallas, United States
dentist, Atlanta, United States
dentist, Nashville, United States
plumber, Houston, United States
plumber, Phoenix, United States
plumber, Dallas, United States
plumber, Atlanta, United States
plumber, Indianapolis, United States
chiropractor, Houston, United States
chiropractor, Phoenix, United States
chiropractor, Denver, United States
chiropractor, Atlanta, United States
chiropractor, Nashville, United States
pest control, Houston, United States
pest control, Phoenix, United States
pest control, Tampa, United States
pest control, Atlanta, United States
pest control, Orlando, United States
junk removal, Houston, United States
junk removal, Phoenix, United States
junk removal, Dallas, United States
junk removal, Atlanta, United States
junk removal, Charlotte, United States
garage door repair, Houston, United States
garage door repair, Phoenix, United States
garage door repair, Denver, United States
garage door repair, Atlanta, United States
garage door repair, Nashville, United States
pool service, Houston, United States
pool service, Phoenix, United States
pool service, Tampa, United States
pool service, Orlando, United States
pool service, Jacksonville, United States"""

    _SUNBELT_USA = """\
HVAC contractor, Orlando, United States
HVAC contractor, Tampa, United States
HVAC contractor, Jacksonville, United States
HVAC contractor, Fort Lauderdale, United States
HVAC contractor, Sarasota, United States
roofing contractor, Orlando, United States
roofing contractor, Tampa, United States
roofing contractor, Jacksonville, United States
roofing contractor, Fort Lauderdale, United States
roofing contractor, Fort Myers, United States
pool service, Orlando, United States
pool service, Tampa, United States
pool service, Jacksonville, United States
pool service, Sarasota, United States
pool service, Clearwater, United States
pest control, Orlando, United States
pest control, Tampa, United States
pest control, Jacksonville, United States
pest control, Fort Lauderdale, United States
pest control, Fort Myers, United States
personal injury attorney, Orlando, United States
personal injury attorney, Tampa, United States
personal injury attorney, Jacksonville, United States
personal injury attorney, Fort Lauderdale, United States
personal injury attorney, Sarasota, United States
dentist, Orlando, United States
dentist, Tampa, United States
dentist, Jacksonville, United States
dentist, Fort Lauderdale, United States
dentist, Clearwater, United States
plumber, Houston, United States
plumber, San Antonio, United States
plumber, Austin, United States
plumber, Phoenix, United States
plumber, Las Vegas, United States
HVAC contractor, Houston, United States
HVAC contractor, San Antonio, United States
HVAC contractor, Phoenix, United States
HVAC contractor, Las Vegas, United States
HVAC contractor, Atlanta, United States
roofing contractor, Houston, United States
roofing contractor, San Antonio, United States
roofing contractor, Phoenix, United States
roofing contractor, Las Vegas, United States
roofing contractor, Atlanta, United States
junk removal, Orlando, United States
junk removal, Tampa, United States
junk removal, Houston, United States
junk removal, Phoenix, United States
junk removal, Atlanta, United States"""

    with st.expander("⚡ Batch Search — run multiple keywords at once", expanded=False):
        st.markdown(
            "<p style='font-size:13px;color:#6b7280;margin:0 0 12px;'>"
            "Enter one search per line: <code>keyword, city, country</code>. "
            "Country defaults to United Kingdom if omitted. "
            "Pick a template or write your own combinations.</p>",
            unsafe_allow_html=True,
        )

        # ── USA templates ──────────────────────────────────────────────────────
        st.markdown("<p style='font-size:11px;font-weight:600;color:#3b82f6;"
                    "text-transform:uppercase;letter-spacing:0.5px;margin:0 0 6px;'>"
                    "🇺🇸 USA templates</p>", unsafe_allow_html=True)
        u1, u2, u3, u4 = st.columns(4)
        with u1:
            if st.button("🔧 USA Trades", use_container_width=True, key="tpl_usatrades"):
                st.session_state["batch_text"] = _USA_TRADES
        with u2:
            if st.button("⚖️ USA Attorneys & Pro", use_container_width=True, key="tpl_usapro"):
                st.session_state["batch_text"] = _USA_PROFESSIONALS
        with u3:
            if st.button("🏥 USA Healthcare", use_container_width=True, key="tpl_usahealth"):
                st.session_state["batch_text"] = _USA_HEALTHCARE
        with u4:
            if st.button("💅 USA Beauty & Spas", use_container_width=True, key="tpl_usabeauty"):
                st.session_state["batch_text"] = _USA_BEAUTY

        st.markdown("<p style='font-size:11px;font-weight:600;color:#3b82f6;"
                    "text-transform:uppercase;letter-spacing:0.5px;margin:8px 0 6px;'>"
                    "🏆 USA mega-batches</p>", unsafe_allow_html=True)
        m1, m2 = st.columns(2)
        with m1:
            if st.button("🏆 High-Value USA (50 searches)", use_container_width=True,
                         key="tpl_highvalue_usa"):
                st.session_state["batch_text"] = _HIGH_VALUE_USA
        with m2:
            if st.button("☀️ Sun Belt + Florida (50 searches)", use_container_width=True,
                         key="tpl_sunbelt"):
                st.session_state["batch_text"] = _SUNBELT_USA

        st.divider()

        # ── UK templates ───────────────────────────────────────────────────────
        st.markdown("<p style='font-size:11px;font-weight:600;color:#9ca3af;"
                    "text-transform:uppercase;letter-spacing:0.5px;margin:0 0 6px;'>"
                    "🇬🇧 UK-wide templates</p>", unsafe_allow_html=True)
        t1, t2, t3, t4 = st.columns(4)
        with t1:
            if st.button("🔧 UK Trades", use_container_width=True, key="tpl_uktrades"):
                st.session_state["batch_text"] = _UK_TRADES
        with t2:
            if st.button("💼 UK Professionals", use_container_width=True, key="tpl_ukpro"):
                st.session_state["batch_text"] = _UK_PROFESSIONALS
        with t3:
            if st.button("🏥 UK Healthcare", use_container_width=True, key="tpl_ukhealth"):
                st.session_state["batch_text"] = _UK_HEALTHCARE
        with t4:
            if st.button("💅 UK Beauty", use_container_width=True, key="tpl_ukbeauty"):
                st.session_state["batch_text"] = _UK_BEAUTY

        st.markdown("<p style='font-size:11px;font-weight:600;color:#9ca3af;"
                    "text-transform:uppercase;letter-spacing:0.5px;margin:8px 0 6px;'>"
                    "London postcodes</p>", unsafe_allow_html=True)
        l1, l2, l3, l4 = st.columns(4)
        with l1:
            if st.button("🔧 London Trades", use_container_width=True, key="tpl_lontrades"):
                st.session_state["batch_text"] = _LONDON_TRADES
        with l2:
            if st.button("💼 London Professionals", use_container_width=True, key="tpl_lonpro"):
                st.session_state["batch_text"] = _LONDON_PROFESSIONALS
        with l3:
            if st.button("🏥 London Healthcare", use_container_width=True, key="tpl_lonhealth"):
                st.session_state["batch_text"] = _UK_HEALTHCARE.replace(", United Kingdom",
                    ", London, United Kingdom")
        with l4:
            if st.button("💅 London Beauty", use_container_width=True, key="tpl_lonbeauty"):
                st.session_state["batch_text"] = _LONDON_BEAUTY

        st.markdown("<p style='font-size:11px;font-weight:600;color:#9ca3af;"
                    "text-transform:uppercase;letter-spacing:0.5px;margin:8px 0 6px;'>"
                    "High-value local services</p>", unsafe_allow_html=True)
        h1, h2 = st.columns(2)
        with h1:
            if st.button("🏆 High-Value UK (50 searches)", use_container_width=True,
                         key="tpl_highvalue_uk"):
                st.session_state["batch_text"] = _HIGH_VALUE_UK
        with h2:
            if st.button("🏆 High-Value London (45 searches)", use_container_width=True,
                         key="tpl_highvalue_lon"):
                st.session_state["batch_text"] = _HIGH_VALUE_LONDON

        batch_text = st.text_area(
            "Search list",
            value=st.session_state.get("batch_text", ""),
            height=220,
            placeholder="emergency plumber, Birmingham, United Kingdom\nmortgage broker, Leeds, United Kingdom\nprivate dentist, Bristol, United Kingdom",
            label_visibility="collapsed",
            key="batch_textarea",
        )

        _bp1, _bp2, _bp3 = st.columns([3, 1, 1])
        with _bp1:
            batch_pages = st.slider("Pages per search", 1, 10, 2, key="batch_pages",
                                    help="2 pages ≈ 40 businesses per keyword. "
                                         "Lower = faster batch, higher = more leads per search.")
        with _bp2:
            batch_email = st.toggle("Auto-extract emails", value=True, key="batch_email",
                                    help="ON = visits each website to find email (slower but ready to send).\n"
                                         "OFF = saves businesses only, no email lookup (much faster).")
        with _bp3:
            _threads = st.select_slider("Threads", options=[4, 8, 12, 16, 24, 32], value=16, key="batch_threads",
                                        help="Parallel email extractions. Higher = faster. 16–32 recommended for batch.")

        _b_lines = [l.strip() for l in batch_text.strip().splitlines() if l.strip() and not l.startswith("#")]
        _est_mins = max(1, int(len(_b_lines) * batch_pages * 20 * 6 / ((_threads if batch_email else 300) * 60)))
        st.caption(
            f"{'📋 ' + str(len(_b_lines)) + ' searches queued' if _b_lines else '⬆ Fill in the list above or pick a template'}"
            + (f" · ⏱ ~{_est_mins} min estimated" if _b_lines else "")
        )

        if st.button("🚀 Run Batch Search", type="primary",
                     use_container_width=True, key="btn_batch"):
            if not _b_lines:
                st.error("Add at least one search to the list.")
            else:
                _serper_key  = st.session_state.get("s_serper","") or st.secrets.get("serper_key","")
                _fsq_key     = st.session_state.get("s_fsq","")    or st.secrets.get("foursquare_key","")
                _gplaces_key = st.session_state.get("s_gplaces","")or st.secrets.get("google_places_key","")
                _yelp_key    = st.session_state.get("s_yelp","")   or st.secrets.get("yelp_api_key","")

                _b_total     = len(_b_lines)
                _b_total_new = 0
                _b_total_biz = 0

                # ── Live status banner (replaces itself each search) ──────
                _status_box = st.empty()
                _batch_prog = st.progress(0)
                _batch_log  = st.empty()
                _batch_lines: list = []

                def _blog(msg):
                    _batch_lines.append(msg)
                    _batch_log.markdown(
                        "<div style='background:#0f172a;border-radius:10px;"
                        "padding:14px 18px;font-family:monospace;font-size:12px;"
                        "color:#94a3b8;max-height:220px;overflow-y:auto;line-height:1.7;'>"
                        + "<br>".join(_batch_lines[-30:])
                        + "</div>",
                        unsafe_allow_html=True,
                    )

                def _update_status(num, kw="", loc="", done=False):
                    if done:
                        _status_box.markdown(
                            f"<div style='background:#052e16;border:2px solid #166534;"
                            f"border-radius:12px;padding:18px 22px;margin-bottom:8px;'>"
                            f"<div style='font-size:12px;font-weight:700;color:#4ade80;"
                            f"letter-spacing:1px;text-transform:uppercase;margin-bottom:10px;'>"
                            f"✅ Batch Complete</div>"
                            f"<div style='display:flex;gap:32px;flex-wrap:wrap;'>"
                            f"<div><div style='font-size:28px;font-weight:800;color:#4ade80;"
                            f"line-height:1;'>{_b_total_new}</div>"
                            f"<div style='font-size:11px;color:#86efac;margin-top:2px;'>New leads saved</div></div>"
                            f"<div><div style='font-size:28px;font-weight:800;color:#4ade80;"
                            f"line-height:1;'>{_b_total_biz}</div>"
                            f"<div style='font-size:11px;color:#86efac;margin-top:2px;'>Businesses scanned</div></div>"
                            f"<div><div style='font-size:28px;font-weight:800;color:#4ade80;"
                            f"line-height:1;'>{_b_total}</div>"
                            f"<div style='font-size:11px;color:#86efac;margin-top:2px;'>Searches done</div></div>"
                            f"</div></div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        pct = int((num - 1) / _b_total * 100)
                        _status_box.markdown(
                            f"<div style='background:#1e1b4b;border:2px solid #4338ca;"
                            f"border-radius:12px;padding:18px 22px;margin-bottom:8px;'>"
                            f"<div style='font-size:12px;font-weight:700;color:#a5b4fc;"
                            f"letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;'>"
                            f"🔄 Running…</div>"
                            f"<div style='font-size:15px;font-weight:600;color:#e0e7ff;"
                            f"margin-bottom:12px;'>Search {num} of {_b_total} — "
                            f"<span style='color:#c7d2fe;font-weight:400;'>{kw} in {loc}</span></div>"
                            f"<div style='display:flex;gap:32px;flex-wrap:wrap;'>"
                            f"<div><div style='font-size:24px;font-weight:800;color:#a5b4fc;"
                            f"line-height:1;'>{_b_total_new}</div>"
                            f"<div style='font-size:11px;color:#818cf8;margin-top:2px;'>Leads so far</div></div>"
                            f"<div><div style='font-size:24px;font-weight:800;color:#a5b4fc;"
                            f"line-height:1;'>{_b_total_biz}</div>"
                            f"<div style='font-size:11px;color:#818cf8;margin-top:2px;'>Businesses scanned</div></div>"
                            f"<div><div style='font-size:24px;font-weight:800;color:#a5b4fc;"
                            f"line-height:1;'>{pct}%</div>"
                            f"<div style='font-size:11px;color:#818cf8;margin-top:2px;'>Complete</div></div>"
                            f"</div></div>",
                            unsafe_allow_html=True,
                        )

                from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

                def _extract_one(biz):
                    """Fetch email for one business — runs in a thread pool."""
                    if not biz.get("website"):
                        biz["email"] = None
                        biz["email_source"] = None
                        biz["status"] = "no_email"
                        return biz
                    em, es = find_email_on_website(biz["website"],
                                                   use_guess_fallback=False,
                                                   fast_mode=True)
                    biz["email"] = em
                    biz["email_source"] = es
                    if not em:
                        biz["status"] = "no_email"
                    return biz

                for _bi, _line in enumerate(_b_lines):
                    parts = [p.strip() for p in _line.split(",")]
                    if len(parts) < 2:
                        _blog(f"⚠️  Skipping: {_line}")
                        continue
                    _b_kw   = parts[0]
                    _b_loc  = parts[1]
                    _b_ctry = parts[2] if len(parts) >= 3 else "United Kingdom"

                    _batch_prog.progress(int(_bi / _b_total * 100))
                    _update_status(_bi + 1, _b_kw, _b_loc)
                    _blog(f"🔍 [{_bi+1}/{_b_total}] {_b_kw} · {_b_loc}, {_b_ctry}")

                    _b_sid = save_search(_b_kw, _b_loc, _b_ctry)
                    _b_bizs = find_businesses(
                        keyword=_b_kw, location=_b_loc, country=_b_ctry,
                        max_pages=batch_pages,
                        skip_top=10,
                        serper_key=_serper_key, foursquare_key=_fsq_key,
                        yelp_api_key=_yelp_key, google_places_key=_gplaces_key,
                        log_cb=_blog,
                    )

                    # Deduplicate before hitting websites
                    _b_unique = [b for b in _b_bizs
                                 if not is_duplicate_lead(
                                     website=b.get("website",""),
                                     phone=b.get("phone",""))]

                    # ── Parallel email extraction ─────────────────────────
                    _n_threads = st.session_state.get("batch_threads", 8)
                    if batch_email and _b_unique:
                        _blog(f"  📧 Extracting emails from {len(_b_unique)} sites ({_n_threads} threads)…")
                        with ThreadPoolExecutor(max_workers=_n_threads) as _pool:
                            _futures = {_pool.submit(_extract_one, b): b for b in _b_unique}
                            for _fut in _as_completed(_futures):
                                try:
                                    _futures[_fut].update(_fut.result())
                                except Exception:
                                    pass
                    else:
                        for b in _b_unique:
                            b["email"] = None
                            b["email_source"] = None
                            b["status"] = "no_email"

                    _b_new = 0
                    for _biz in _b_unique:
                        if insert_lead(_biz):
                            _b_new += 1

                    update_search_result(_b_sid, len(_b_bizs), _b_new)
                    _blog(f"  ✅ {_b_new} new leads saved from {len(_b_bizs)} businesses")
                    _b_total_new += _b_new
                    _b_total_biz += len(_b_bizs)

                _batch_prog.progress(100)
                _update_status(0, done=True)
                st.success(
                    f"✅ Batch done — **{_b_total_new} new leads** saved "
                    f"from {_b_total_biz} businesses across {_b_total} searches. "
                    f"Go to **Send Emails** to start outreach."
                )

    # ── E-commerce Store Finder ───────────────────────────────────────────────
    with st.expander("🛒 E-commerce Store Finder — find online shops to pitch", expanded=False):
        st.markdown(
            "<p style='font-size:13px;color:#6b7280;margin:0 0 14px;'>"
            "Searches Google for <b>independent online stores</b> selling a specific product "
            "or in a specific niche. Filters out Amazon, eBay, Etsy and major retailers "
            "automatically. Uses your Serper API key — same one as Google Maps search.</p>",
            unsafe_allow_html=True,
        )

        # Quick-fill niche templates
        st.markdown("<p style='font-size:11px;font-weight:600;color:#9ca3af;"
                    "text-transform:uppercase;letter-spacing:0.5px;margin:0 0 6px;'>"
                    "Niche ideas</p>", unsafe_allow_html=True)
        _ec1, _ec2, _ec3, _ec4 = st.columns(4)
        _ecomm_niches = {
            "🧴 Skincare": "natural skincare products",
            "🏋️ Fitness": "fitness supplements",
            "🐾 Pet": "pet accessories",
            "👗 Fashion": "women's fashion boutique",
            "🍵 Food": "artisan food gifts",
            "🧸 Baby": "baby products",
            "🪴 Home": "home décor",
            "💍 Jewellery": "handmade jewellery",
        }
        _niche_cols = [_ec1, _ec2, _ec3, _ec4] * 2
        for (_label, _niche_val), _col in zip(_ecomm_niches.items(), _niche_cols):
            with _col:
                if st.button(_label, use_container_width=True, key=f"ec_{_label}"):
                    st.session_state["ec_niche"] = _niche_val

        _ecol1, _ecol2 = st.columns([3, 1])
        with _ecol1:
            ec_niche = st.text_input(
                "Product niche or keyword",
                value=st.session_state.get("ec_niche", ""),
                placeholder="e.g. natural skincare · fitness supplements · artisan coffee",
                key="ec_niche_input",
            )
        with _ecol2:
            ec_country = st.selectbox(
                "Country", ["United Kingdom", "Australia", "USA", "New Zealand", "UAE"],
                key="ec_country",
            )

        _ecol3, _ecol4 = st.columns([2, 1])
        with _ecol3:
            ec_max = st.slider("Max stores to find", 10, 60, 30, key="ec_max",
                               help="Each store website is then visited to extract a contact email.")
        with _ecol4:
            ec_email = st.toggle("Auto-extract emails", value=True, key="ec_email")

        _serper_for_ec = st.session_state.get("s_serper","") or st.secrets.get("serper_key","")

        if not _serper_for_ec:
            st.warning("⚠️ Add your Serper API key in the sidebar to use e-commerce search.")
        elif st.button("🛒 Find E-commerce Stores", type="primary",
                       use_container_width=True, key="btn_ecomm"):
            if not ec_niche.strip():
                st.error("Enter a product niche or keyword.")
            else:
                _ec_prog = st.progress(0, text="Searching Google for online stores…")
                _ec_log  = st.empty()
                _ec_lines: list = []

                def _eclog(msg):
                    _ec_lines.append(msg)
                    _ec_log.markdown("```\n" + "\n".join(_ec_lines[-20:]) + "\n```")

                _ec_sid   = save_search(ec_niche, "web", ec_country)
                _ec_stores = scrape_ecommerce(
                    niche=ec_niche.strip(),
                    country=ec_country,
                    api_key=_serper_for_ec,
                    max_results=ec_max,
                    log_cb=_eclog,
                )
                _ec_prog.progress(50, text=f"Found {len(_ec_stores)} stores — extracting emails…")

                _ec_new = 0
                for _idx, _store in enumerate(_ec_stores):
                    if is_duplicate_lead(website=_store.get("website", "")):
                        continue
                    if ec_email and _store.get("website"):
                        _em, _es = find_email_on_website(
                            _store["website"], use_guess_fallback=False
                        )
                        _store["email"]        = _em
                        _store["email_source"] = _es
                        if not _em:
                            _store["status"] = "no_email"
                        _eclog(
                            f"{'📧' if _em else '—'}  {_store['business_name'][:40]}  "
                            f"{'→ ' + _em if _em else '(no email)'}"
                        )
                    else:
                        _store["email"]        = None
                        _store["email_source"] = None
                        _store["status"]       = "no_email"

                    if insert_lead(_store):
                        _ec_new += 1

                    _ec_prog.progress(
                        50 + int((_idx + 1) / max(len(_ec_stores), 1) * 50),
                        text=f"Processing {_idx+1}/{len(_ec_stores)} stores…",
                    )

                update_search_result(_ec_sid, len(_ec_stores), _ec_new)
                _ec_prog.progress(100, text="Done!")
                st.success(
                    f"✅ **{_ec_new} new e-commerce leads** saved from {len(_ec_stores)} "
                    f"stores found. Use the **E-commerce** email template when sending — "
                    f"it references your +238% revenue case study directly."
                )

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    with st.container():
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            keyword = st.text_input("Business type", placeholder="plumber · HVAC · cake shop · roofer")
        with c2:
            location = st.text_input("City / location", placeholder="Melbourne · Dubai · London")
        with c3:
            country = st.selectbox("Country", list(COUNTRY_SCRAPERS.keys()))

        zipcodes_raw = st.text_input(
            "ZIP / Pin codes  *(optional)*",
            placeholder="e.g. 3000, 3001, 3002  or  560001  — comma-separate multiple codes",
            help="Each postcode is searched separately for hyper-local results.",
        )
        if zipcodes_raw.strip():
            _zc_list = [z.strip() for z in zipcodes_raw.split(",") if z.strip()]
            st.caption(f"📍 Will run {len(_zc_list)} targeted search(es): {', '.join(_zc_list[:8])}{'…' if len(_zc_list) > 8 else ''}")
        else:
            st.caption("💡 Leave blank to search by city only, or enter postcodes for hyper-local results.")

        # Parse zip codes into a list for use during search
        zipcodes = [z.strip() for z in zipcodes_raw.split(",") if z.strip()] if zipcodes_raw.strip() else []

        c4, c5 = st.columns(2)
        with c4:
            max_pages = st.slider(
                "Pages to scrape", 1, 10, 5,
                help="Each page returns ~10 businesses from Google Maps. "
                     "5 pages ≈ 50 businesses — ideal daily batch. "
                     "10 pages ≈ 100 businesses — max recommended."
            )
            st.caption(f"≈ {max_pages * 10} businesses will be scraped"
                       + (" · ✅ recommended" if max_pages == 5 else
                          " · ⚠️ may be slow" if max_pages >= 8 else ""))
        with c5:
            auto_email = st.toggle("Auto-extract emails from websites", value=True)

        # Ranking target selector
        target_option = st.selectbox(
            "Which ranking positions to target?",
            options=["page2", "page3", "page1", "page2_3"],
            format_func=lambda x: {
                "page2":   "📍 Page 2  (ranks #11–30) — recommended",
                "page3":   "📍 Page 3  (ranks #21–40) — very cold, low competition",
                "page2_3": "📍 Page 2 & 3  (ranks #11–40) — widest net",
                "page1":   "📍 Page 1  (ranks #1–10) — already ranking, not ideal",
            }[x],
            help="Page 1 businesses already rank well and may not need SEO. "
                 "Page 2–3 businesses are visible but struggling — perfect SEO prospects.",
        )
        skip_top = {"page1": 0, "page2": 10, "page3": 20, "page2_3": 10}[target_option]
        extra_pages = {"page1": 0, "page2": 0, "page3": 1, "page2_3": 1}[target_option]

        st.markdown(
            f"<div style='background:#eff6ff;border-radius:8px;padding:10px 14px;"
            f"font-size:13px;color:#1d4ed8;margin-top:4px;'>"
            f"<b>Targeting strategy:</b> "
            f"{'Skipping top ' + str(skip_top) + ' results — ' if skip_top else 'Including all results — '}"
            + {
                "page2":   "businesses ranked #11–30. Have web presence but not ranking → ideal SEO clients.",
                "page3":   "businesses ranked #21–40. Barely visible online → high need, less competition.",
                "page2_3": "businesses ranked #11–40. Widest prospect pool across pages 2 & 3.",
                "page1":   "top-ranked businesses. They may already have good SEO — lower conversion expected.",
            }[target_option]
            + "</div>",
            unsafe_allow_html=True,
        )

    if st.button("Find Leads", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("Please enter a business keyword (e.g. 'plumber').")
        elif not location.strip():
            st.error("Please enter a location (e.g. 'Melbourne').")
        else:
            log_box  = st.empty()
            prog_bar = st.progress(0, text="Starting …")
            log_lines: list = []

            def log(msg: str):
                log_lines.append(msg)
                log_box.markdown("```\n" + "\n".join(log_lines[-30:]) + "\n```")

            _serper_key  = st.session_state.get("s_serper", "") or st.secrets.get("serper_key", "")
            _fsq_key     = st.session_state.get("s_fsq",    "") or st.secrets.get("foursquare_key", "")
            _gplaces_key = st.session_state.get("s_gplaces","") or st.secrets.get("google_places_key", "")
            _yelp_key    = st.session_state.get("s_yelp",   "") or st.secrets.get("yelp_api_key", "")

            # ── Build search locations: city only, or city + each zip code ────
            if zipcodes:
                _search_locations = [f"{location.strip()} {zc}" for zc in zipcodes]
                log(f"🔎 Searching: '{keyword}' across {len(zipcodes)} postcode(s) in {location}, {country}")
            else:
                _search_locations = [location.strip()]
                log(f"🔎 Searching: '{keyword}' in {location}, {country}")

            businesses = []
            for _loc in _search_locations:
                _search_id = save_search(keyword.strip(), _loc, country)
                log(f"  📍 Location: {_loc}")
                _biz_batch = find_businesses(
                    keyword=keyword.strip(), location=_loc, country=country,
                    max_pages=max_pages + extra_pages,
                    skip_top=skip_top,
                    serper_key=_serper_key, foursquare_key=_fsq_key,
                    yelp_api_key=_yelp_key, google_places_key=_gplaces_key, log_cb=log,
                )
                businesses.extend(_biz_batch)
                log(f"  ✅ {len(_biz_batch)} found for {_loc}")

            if not businesses:
                st.warning("No businesses found. Try a different keyword, location, or add a Serper API key.")
                st.stop()

            # ── Dedup within this batch by website + phone ────────────────────
            seen_ws: set = set()
            seen_ph: set = set()
            unique_biz   = []
            batch_dups   = 0
            for biz in businesses:
                ws = (biz.get("website") or "").strip()
                ph = (biz.get("phone")   or "").strip()
                if (ws and ws in seen_ws) or (ph and ph in seen_ph):
                    batch_dups += 1
                    continue
                if ws: seen_ws.add(ws)
                if ph: seen_ph.add(ph)
                unique_biz.append(biz)

            businesses = unique_biz
            if batch_dups:
                log(f"🔁 Removed {batch_dups} duplicates within this batch.")

            new_count    = 0
            no_email_ct  = 0
            skipped_ct   = 0
            email_dup_ct = 0
            preview_rows = []   # collect processed rows for the results preview

            for idx, biz in enumerate(businesses):
                pct = int((idx + 1) / len(businesses) * 100)
                prog_bar.progress(pct, text=f"Processing {idx+1}/{len(businesses)}: {biz.get('business_name','')}")

                # ── Skip if website/phone already in DB ───────────────────────
                if is_duplicate_lead(website=biz.get("website",""), phone=biz.get("phone","")):
                    log(f"⏭️  Already in DB — skipping: {biz.get('business_name','')}")
                    skipped_ct += 1
                    continue

                # ── Email discovery ───────────────────────────────────────────
                if auto_email and biz.get("website"):
                    log(f"🔗  {biz['business_name']} → {biz['website']}")
                    email, email_source = find_email_on_website(
                        biz["website"], use_guess_fallback=False
                    )
                    if email:
                        # ── Email-level dedup: has this address been sent before? ──

                        existing = get_lead_by_email(email)
                        if existing:
                            _est = existing.get("status", "unknown")
                            _ebn = existing.get("business_name", "another lead")
                            _status_label = {
                                "sent":     "already emailed",
                                "new":      "already in queue",
                                "failed":   "previous send failed",
                                "no_email": "stored without email",
                            }.get(_est, _est)
                            log(f"    ⏭️  {email} already in DB ({_status_label} · {_ebn})")
                            email_dup_ct += 1
                            skipped_ct   += 1
                            continue

                        biz["email"]        = email
                        biz["email_source"] = email_source
                        _src_icons = {
                            "found":    ("📧", "✅ extracted from site"),
                            "inferred": ("🔮", "name-based (inferred)"),
                            "guessed":  ("🤔", "pattern guess"),
                        }
                        icon, note = _src_icons.get(email_source or "", ("📧", ""))
                        log(f"    {icon}  {email}  {note}")
                    else:
                        biz["email"]        = None
                        biz["email_source"] = None
                        biz["status"]       = "no_email"
                        log("    ⚠️  No email found")
                elif not auto_email:
                    biz["email"]        = None
                    biz["email_source"] = None
                    biz["status"]       = "no_email"

                if not biz.get("email"):
                    no_email_ct += 1

                inserted = insert_lead(biz)
                if inserted:
                    new_count += 1
                    preview_rows.append(biz)

            # Update search history with final counts
            if _search_id:
                update_search_result(_search_id, len(businesses) + batch_dups, new_count)

            prog_bar.progress(100, text="Done!")

            st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
            r1, r2, r3, r4, r5 = st.columns(5)
            with r1:
                _stat_card("Found",          len(businesses) + batch_dups, "#2563eb")
            with r2:
                _stat_card("New leads saved", new_count,  "#16a34a",
                           "ready to email" if new_count else "")
            with r3:
                _stat_card("Already in DB",  skipped_ct, "#7c3aed",
                           "quota saved" if skipped_ct else "")
            with r4:
                _stat_card("Email already sent", email_dup_ct, "#f59e0b",
                           "protected" if email_dup_ct else "")
            with r5:
                _stat_card("No email found", no_email_ct, "#ef4444")

            if new_count > 0:
                st.success(f"✅ {new_count} new leads saved — go to **Send Emails** to reach out.")
            elif skipped_ct == len(businesses):
                st.info("All businesses from this search are already in your database. "
                        "Try a different keyword or location to find fresh leads.")

            if preview_rows:
                st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;"
                            "margin:16px 0 8px;'>Newly saved leads preview</p>",
                            unsafe_allow_html=True)
                df_prev = pd.DataFrame(preview_rows)
                if "email_source" in df_prev.columns:
                    _src_labels = {
                        "found":    "✅ Found",
                        "inferred": "🔮 Inferred",
                        "guessed":  "🤔 Guessed",
                    }
                    df_prev["Email status"] = df_prev["email_source"].map(
                        lambda s: _src_labels.get(s, "—")
                    )
                show_cols = [c for c in
                    ["business_name","email","Email status","phone","website","address","city","source"]
                    if c in df_prev.columns]
                st.dataframe(df_prev[show_cols], use_container_width=True)


with tab_find:
    _render_find()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 – Leads Database
# ═════════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_db():
    _section("Leads Database", "All businesses found so far. Sent leads are never emailed again.")

    f1, f2 = st.columns([3, 1])
    with f1:
        status_filter = st.selectbox("Filter", ["all", "new", "sent", "failed", "no_email"],
            format_func=lambda x: {"all":"All leads","new":"New — not yet emailed",
                "sent":"Sent","failed":"Failed","no_email":"No email found"}.get(x, x))
    with f2:
        st.markdown("<div style='height:27px'></div>", unsafe_allow_html=True)
        if st.button("🔄 Refresh", use_container_width=True): st.rerun(scope="app")

    df_db = get_leads(None if status_filter == "all" else status_filter)

    if df_db.empty:
        st.markdown(
            "<div style='background:white;border-radius:12px;padding:48px 24px;"
            "text-align:center;border:1px solid #e9ecef;'>"
            "<p style='font-size:32px;margin:0 0 12px;'>📭</p>"
            "<p style='font-size:16px;font-weight:600;color:#111827;margin:0 0 6px;'>No leads yet</p>"
            "<p style='font-size:13px;color:#9ca3af;margin:0;'>Go to Find Leads to get started.</p>"
            "</div>", unsafe_allow_html=True)
    else:
        has_email = df_db["email"].notna() & (df_db["email"] != "")
        st.markdown(
            f"<div style='display:flex;gap:20px;flex-wrap:wrap;margin-bottom:14px;"
            f"font-size:13px;color:#6b7280;'>"
            f"<span><b style='color:#111827;'>{len(df_db)}</b> leads shown</span>"
            f"<span>·</span>"
            f"<span><b style='color:#16a34a;'>{has_email.sum()}</b> with email</span>"
            f"<span>·</span>"
            f"<span><b style='color:#9ca3af;'>{(~has_email).sum()}</b> without email</span>"
            f"</div>", unsafe_allow_html=True)

        if "email_source" in df_db.columns:
            _db_src_labels = {"found": "Found", "inferred": "Inferred", "guessed": "Guessed"}
            df_db["Email source"] = df_db["email_source"].map(
                lambda s: _db_src_labels.get(s, "—"))
        show = [c for c in ["id","business_name","email","Email source","phone",
                             "city","country","keyword","source","status",
                             "email_sent_at","created_at"] if c in df_db.columns]
        st.dataframe(df_db[show], use_container_width=True, height=420)

        col_dl, col_del = st.columns([1, 3])
        with col_dl:
            st.download_button("Export CSV",
                data=df_db.to_csv(index=False).encode(),
                file_name=f"leads_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv", use_container_width=True)
        with st.expander("Delete leads by ID"):
            ids_input = st.text_input("IDs to delete (comma-separated)", placeholder="1, 5, 12")
            if st.button("Delete", type="primary"):
                try:
                    ids = [int(x.strip()) for x in ids_input.split(",") if x.strip()]
                    delete_leads(ids); st.success(f"Deleted {len(ids)} lead(s)."); st.rerun(scope="app")
                except ValueError:
                    st.error("Use numbers separated by commas.")


with tab_db:
    _render_db()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 – Send Emails
# ═════════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_send():
    _section("Send Emails",
             "Only 'New' leads appear here. Once sent, a lead is marked Sent and never emailed again.")

    # Refresh button — fragments don't auto-update when DB changes in another tab
    if st.button("🔄 Refresh queue", key="send_refresh", help="Reload leads from database"):
        st.rerun(scope="app")

    if not _smtp_ready():
        st.markdown(
            "<div style='background:white;border-radius:12px;padding:32px;text-align:center;"
            "border:1px solid #e9ecef;'>"
            "<p style='font-size:28px;margin:0 0 10px;'>⚙️</p>"
            "<p style='font-size:15px;font-weight:600;color:#111827;margin:0 0 6px;'>"
            "Gmail not configured</p>"
            "<p style='font-size:13px;color:#9ca3af;margin:0;'>"
            "Fill in From address, App password and Display name in the sidebar.</p>"
            "</div>", unsafe_allow_html=True)
        st.caption("How to get an App Password: Google account → Security → 2-Step Verification → App Passwords")
    else:
        with st.expander("Preview email template"):
            st.markdown(
                build_html("short", "ABC Plumbing",
                           sender_name or "Your Name",
                           sender_email or "you@gmail.com"),
                unsafe_allow_html=True,
            )

        # ── Read background state (thread-safe snapshot) ──────────────────────
        with bg_state.LOCK:
            _bg = dict(bg_state.STATE)
            _bg["errors"] = list(bg_state.STATE["errors"])   # copy the list too
        _thread_alive = (
            bg_state._thread is not None and bg_state._thread.is_alive()
        )
        _is_running = _bg["running"] or _thread_alive

        # ── PANEL A — Background progress (shown when send is active) ─────────
        if _is_running or _bg["finished_at"]:
            _pct = (int(_bg["done"] / _bg["total"] * 100)
                    if _bg["total"] > 0 else 0)

            if _is_running:
                # Running header
                st.markdown(
                    "<div style='background:#f0fdf4;border:1px solid #bbf7d0;"
                    "border-radius:10px;padding:16px 20px;margin-bottom:16px;'>"
                    "<div style='font-size:14px;font-weight:700;color:#15803d;"
                    "margin-bottom:4px;'>📤 Sending emails in the background</div>"
                    "<div style='font-size:13px;color:#166534;'>"
                    "You can switch tabs, minimise this window, or leave it open — "
                    "emails keep going. Come back here and click "
                    "<b>Refresh status</b> to see progress.</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    "<div style='background:#f0fdf4;border:1px solid #bbf7d0;"
                    "border-radius:10px;padding:14px 20px;margin-bottom:16px;'>"
                    "<div style='font-size:14px;font-weight:700;color:#15803d;'>"
                    f"✅ Background send finished — {_bg['sent']} sent, "
                    f"{_bg['failed']} failed</div>"
                    f"<div style='font-size:12px;color:#166534;margin-top:2px;'>"
                    f"Started {_bg['started_at']} · Finished {_bg['finished_at']}</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

            # Progress stats
            pa1, pa2, pa3, pa4 = st.columns(4)
            with pa1: _stat_card("Sent",      _bg["sent"],                    "#16a34a")
            with pa2: _stat_card("Failed",    _bg["failed"],                  "#ef4444")
            with pa3: _stat_card("Remaining", max(0, _bg["total"]-_bg["done"]), "#6b7280")
            with pa4: _stat_card("Total",     _bg["total"],                   "#2563eb")

            # Progress bar
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            st.progress(_pct / 100,
                        text=(f"Processing {_bg['done']}/{_bg['total']} — "
                              f"currently: {_bg['current_biz'] or 'waiting…'}")
                        if _is_running else
                        f"Complete ({_bg['done']}/{_bg['total']})")

            if _bg.get("current_email") and _is_running:
                st.caption(f"Sending to: {_bg['current_email']}")

            # Controls
            ctrl1, ctrl2 = st.columns([1, 3])
            with ctrl1:
                if st.button("🔄 Refresh status", use_container_width=True):
                    st.rerun(scope="app")
            with ctrl2:
                if _is_running and not _bg["cancel_requested"]:
                    if st.button("⏹ Stop after current email",
                                 use_container_width=True):
                        with bg_state.LOCK:
                            bg_state.STATE["cancel_requested"] = True
                        st.warning("Stop requested — finishing the current email "
                                   "then halting. Remaining leads stay as 'queued'.")
                elif _bg["cancel_requested"] and _is_running:
                    st.info("⏳ Stopping after this email…")

            # Errors
            if _bg["errors"]:
                with st.expander(f"⚠️ {len(_bg['errors'])} delivery error(s)"):
                    for err in _bg["errors"]:
                        st.text(err)

            st.divider()

        # ── PANEL B — Queue panel (new confirmed leads waiting to be sent) ─────
        df_ready = get_leads_with_email(status="new")

        # Split confirmed vs inferred vs guessed
        if "email_source" in df_ready.columns and not df_ready.empty:
            df_confirmed = df_ready[df_ready["email_source"] == "found"].copy()
            df_inferred  = df_ready[df_ready["email_source"] == "inferred"].copy()
            df_guessed   = df_ready[df_ready["email_source"] == "guessed"].copy()
        else:
            df_confirmed = df_ready.copy()
            df_inferred  = pd.DataFrame()
            df_guessed   = pd.DataFrame()

        confirmed_ct = len(df_confirmed)
        inferred_ct  = len(df_inferred)
        guessed_ct   = len(df_guessed)

        # Inferred-email info banner
        if inferred_ct > 0:
            st.markdown(
                f"<div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;"
                f"padding:14px 18px;margin-bottom:12px;'>"
                f"<p style='font-size:13px;font-weight:700;color:#0369a1;margin:0 0 4px;'>"
                f"🔮  {inferred_ct} name-inferred email address"
                f"{'es' if inferred_ct > 1 else ''} in queue</p>"
                f"<p style='font-size:12px;color:#075985;margin:0;line-height:1.6;'>"
                f"These emails were <b>derived from person names found on the website</b> "
                f"(e.g. <code>john@businessdomain.co.uk</code>). Better than a blind guess — "
                f"safe to send but expect a slightly higher bounce rate than 'found' emails.</p>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # Guessed-email warning
        if guessed_ct > 0:
            st.markdown(
                f"<div style='background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;"
                f"padding:14px 18px;margin-bottom:16px;'>"
                f"<p style='font-size:13px;font-weight:700;color:#92400e;margin:0 0 4px;'>"
                f"⚠️  {guessed_ct} guessed email address"
                f"{'es' if guessed_ct > 1 else ''} in queue</p>"
                f"<p style='font-size:12px;color:#78350f;margin:0;line-height:1.6;'>"
                f"These are <b>pattern guesses</b> (e.g. <code>info@domain.com</code>) and "
                f"will likely bounce. Delete them and re-scrape with the improved finder.</p>"
                f"</div>",
                unsafe_allow_html=True,
            )
            with st.expander(f"🗑️ Review & delete {guessed_ct} guessed-email lead(s)"):
                _g_disp = df_guessed.copy()
                _g_disp["Source"] = "🤔 Guessed"
                _gc = [c for c in ["id","business_name","email","Source","city","keyword"]
                       if c in _g_disp.columns]
                st.dataframe(_g_disp[_gc], use_container_width=True,
                             hide_index=True, height=180)
                if st.button("🗑️ Delete all guessed-email leads", type="primary",
                             key="del_guessed"):
                    delete_leads(df_guessed["id"].astype(int).tolist())
                    st.success(f"Deleted {guessed_ct} leads.")
                    st.rerun(scope="app")

        # ── Sending method banner ─────────────────────────────────────────
        _brevo_k = st.session_state.get("s_brevo","") or st.secrets.get("brevo_key","")
        if _brevo_k:
            st.markdown(
                "<div style='background:#f0fdf4;border:1px solid #bbf7d0;"
                "border-radius:8px;padding:10px 16px;margin-bottom:12px;"
                "font-size:13px;color:#15803d;'>"
                "📡 <b>Brevo</b> will be used for sending — open & click tracking enabled.</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='background:#fefce8;border:1px solid #fde68a;"
                "border-radius:8px;padding:10px 16px;margin-bottom:12px;"
                "font-size:13px;color:#92400e;'>"
                "📧 Sending via <b>Gmail SMTP</b> — add a Brevo API key in the sidebar "
                "for open/click tracking and better deliverability.</div>",
                unsafe_allow_html=True,
            )

        # Sendable pool = confirmed ("found") + inferred — both are real addresses.
        # Guessed (info@…) stays separate and is never auto-queued.
        import pandas as _pd2
        _send_frames = [df for df in [df_confirmed, df_inferred] if not df.empty]
        df_sendable  = _pd2.concat(_send_frames, ignore_index=True) if _send_frames else _pd2.DataFrame()
        sendable_ct  = len(df_sendable)

        if sendable_ct == 0:
            if not _is_running:
                st.markdown(
                    "<div style='background:white;border-radius:12px;padding:48px 24px;"
                    "text-align:center;border:1px solid #e9ecef;'>"
                    "<p style='font-size:32px;margin:0 0 12px;'>📭</p>"
                    "<p style='font-size:16px;font-weight:600;color:#111827;margin:0 0 6px;'>"
                    "No leads ready to send</p>"
                    "<p style='font-size:13px;color:#9ca3af;margin:0;'>"
                    "Just ran a batch search? Click <b>Refresh queue</b> above. "
                    "Otherwise go to Find Leads to scrape more businesses.</p>"
                    "</div>", unsafe_allow_html=True)
        else:
            # Summary stats
            s1, s2, s3 = st.columns(3)
            with s1:
                _stat_card(
                    "Ready to send", sendable_ct, "#16a34a",
                    f"{confirmed_ct} confirmed · {inferred_ct} inferred",
                )
            with s2:
                est_mins = max(1, (min(sendable_ct, 20) * delay_sec) // 60)
                _stat_card("Est. for 20 emails", f"~{est_mins} min", "#6b7280")
            with s3:
                _stat_card("Delay between sends", f"{delay_sec}s", "#7c3aed")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

            # Combined leads table
            _c_disp = df_sendable.copy()
            def _email_label(row):
                src = row.get("email_source", "found")
                if src == "inferred":
                    return "🔮 " + str(row.get("email", ""))
                return "✅ " + str(row.get("email", ""))
            _c_disp["Email"] = _c_disp.apply(_email_label, axis=1)
            _cc = [c for c in ["id","business_name","Email","city","country","keyword"]
                   if c in _c_disp.columns]
            st.dataframe(_c_disp[_cc], use_container_width=True,
                         hide_index=True, height=220)

            # ── Template picker ───────────────────────────────────────────
            st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;"
                        "margin:14px 0 6px;'>Email template</p>",
                        unsafe_allow_html=True)
            tpl_choice = st.radio(
                "template", list(TEMPLATE_OPTIONS.keys()),
                format_func=lambda k: (
                    f"{TEMPLATE_OPTIONS[k][0]}  —  {TEMPLATE_OPTIONS[k][1]}"
                ),
                label_visibility="collapsed",
                key="tpl_choice",
            )
            with st.expander("👁 Preview selected template"):
                st.markdown(
                    build_html(tpl_choice,
                               "Example Business Co.",
                               sender_name or "Your Name",
                               sender_email or "you@gmail.com"),
                    unsafe_allow_html=True,
                )

            _slider_max = max(1, min(200, sendable_ct))
            _slider_val = max(1, min(20, sendable_ct))
            sc1, sc2 = st.columns([3, 1])
            with sc1:
                max_send = st.slider(
                    "How many emails to queue?",
                    1, _slider_max, _slider_val,
                )
            with sc2:
                est_total = max(1, (max_send * delay_sec) // 60)
                st.metric("Est. total time", f"~{est_total} min")

            _brevo_key_send = (st.session_state.get("s_brevo","")
                               or st.secrets.get("brevo_key",""))
            method_label = "via Brevo" if _brevo_key_send else "via Gmail SMTP"

            # ── Plain text mode toggle ─────────────────────────────────────
            if _brevo_key_send:
                _plain_mode = st.toggle(
                    "📨 Plain text mode — targets Primary inbox (not Promotions)",
                    value=True,
                    key="plain_text_toggle",
                    help=(
                        "ON  → sends plain text only. No tracking pixel. "
                        "Gmail routes these to Primary inbox.\n\n"
                        "OFF → sends full HTML with open/click tracking. "
                        "More likely to land in Promotions tab."
                    ),
                )
                if _plain_mode:
                    st.caption("📨 Plain text · No open/click tracking · Primary inbox targeting")
                else:
                    st.caption("🎨 HTML email · Open & click tracking active · May land in Promotions")
            else:
                _plain_mode = False

            if _is_running:
                st.warning("⏳ A send is already running. Wait or stop it first.")
            else:
                if st.button(
                    f"📤 Queue {max_send} email{'s' if max_send > 1 else ''} "
                    f"& send in background {method_label}",
                    type="primary", use_container_width=True,
                ):
                    ids = df_sendable.head(max_send)["id"].astype(int).tolist()
                    _eff_name = (st.session_state.get("s_brevo_name","") or "Aniruddh Gohil") \
                                if _brevo_key_send else sender_name
                    _port = st.session_state.get("s_portfolio", DEFAULT_PORTFOLIO_URL)
                    _cs   = st.session_state.get("s_case_study", DEFAULT_CASE_STUDY)
                    started = _queue_and_send(
                        ids, sender_email, app_password, _eff_name,
                        delay_sec, tpl_choice, _brevo_key_send,
                        portfolio_url=_port, case_study=_cs,
                        plain_text_mode=_plain_mode,
                    )
                    if started:
                        st.success(
                            f"✅ {max_send} email(s) queued! Sending in background "
                            f"{method_label}. Navigate away — check progress anytime."
                        )
                        time.sleep(1)
                        st.rerun(scope="app")
                    else:
                        st.error("Could not start — another send is already running.")


with tab_send:
    _render_send()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 – Follow-ups
# ═════════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_followup():
    _section("Follow-up Sequences",
             "Send automated follow-ups to leads that haven't replied. "
             "Day 3 nudge + Day 7 close — proven to 2-3× reply rates.")

    _brevo_fu = st.session_state.get("s_brevo","") or st.secrets.get("brevo_key","")

    fu1 = get_leads_for_followup(touch=1, days_after=3)
    fu2 = get_leads_for_followup(touch=2, days_after=7)

    f1, f2, f3 = st.columns(3)
    with f1: _stat_card("Due Follow-up 1", len(fu1), "#4f46e5", "sent 3+ days ago")
    with f2: _stat_card("Due Follow-up 2", len(fu2), "#0ea5e9", "follow-up 1 sent 7+ days ago")
    with f3: _stat_card("Total sent",      stats.get("sent",0), "#10b981")

    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    # ── Follow-up 1 ──────────────────────────────────────────────────────────
    st.markdown("### Follow-up 1 · Day 3", unsafe_allow_html=False)
    st.caption("A brief nudge referencing the original email. Sent 3 days after first touch.")
    if fu1.empty:
        st.info("No leads are due for Follow-up 1 right now. Check back in a few days.")
    else:
        _fu1_cols = [c for c in ["id","business_name","email","city","keyword","email_sent_at"]
                     if c in fu1.columns]
        st.dataframe(fu1[_fu1_cols], use_container_width=True, hide_index=True, height=200)
        with st.expander("👁 Preview Follow-up 1 template"):
            st.markdown(
                build_html("followup1", "Example Business Co.",
                           sender_name or "Your Name",
                           sender_email or "you@gmail.com"),
                unsafe_allow_html=True,
            )

        _fu1_max = st.slider("How many to send?", 1, min(50, len(fu1)),
                             min(20, len(fu1)), key="fu1_max")
        _fu_method = "via Brevo" if _brevo_fu else "via Gmail SMTP"
        _fu_ready = _smtp_ready() or bool(_brevo_fu)
        if not _fu_ready:
            st.warning("Configure Gmail or Brevo credentials in the sidebar first.")
        elif _is_running if "_is_running" in dir() else False:
            st.warning("Wait for the current send to finish first.")
        else:
            if st.button(f"📤 Send {_fu1_max} Follow-up 1 emails {_fu_method}",
                         type="primary", use_container_width=True, key="btn_fu1"):
                ids = fu1.head(_fu1_max)["id"].astype(int).tolist()
                _eff_name_fu1 = (st.session_state.get("s_brevo_name","") or "Aniruddh Gohil") \
                                if _brevo_fu else sender_name
                started = _queue_and_send(
                    ids, sender_email, app_password, _eff_name_fu1,
                    delay_sec, "followup1", _brevo_fu, is_followup=1,
                    portfolio_url=st.session_state.get("s_portfolio", DEFAULT_PORTFOLIO_URL),
                    case_study=st.session_state.get("s_case_study", DEFAULT_CASE_STUDY),
                )
                if started:
                    st.success(f"✅ {_fu1_max} Follow-up 1 emails queued!")
                    time.sleep(1); st.rerun(scope="app")

    st.divider()

    # ── Follow-up 2 ──────────────────────────────────────────────────────────
    st.markdown("### Follow-up 2 · Day 7 (Final)", unsafe_allow_html=False)
    st.caption("Polite last touch. Leaves the door open without being pushy.")
    if fu2.empty:
        st.info("No leads are due for Follow-up 2 right now.")
    else:
        _fu2_cols = [c for c in ["id","business_name","email","city","keyword",
                                  "followup1_sent_at"] if c in fu2.columns]
        st.dataframe(fu2[_fu2_cols], use_container_width=True, hide_index=True, height=200)
        with st.expander("👁 Preview Follow-up 2 template"):
            st.markdown(
                build_html("followup2", "Example Business Co.",
                           sender_name or "Your Name",
                           sender_email or "you@gmail.com"),
                unsafe_allow_html=True,
            )

        _fu2_max = st.slider("How many to send?", 1, min(50, len(fu2)),
                             min(20, len(fu2)), key="fu2_max")
        if not _fu_ready:
            st.warning("Configure credentials in the sidebar first.")
        else:
            if st.button(f"📤 Send {_fu2_max} Follow-up 2 emails {_fu_method}",
                         type="primary", use_container_width=True, key="btn_fu2"):
                ids = fu2.head(_fu2_max)["id"].astype(int).tolist()
                _eff_name_fu2 = (st.session_state.get("s_brevo_name","") or "Aniruddh Gohil") \
                                if _brevo_fu else sender_name
                started = _queue_and_send(
                    ids, sender_email, app_password, _eff_name_fu2,
                    delay_sec, "followup2", _brevo_fu, is_followup=2,
                    portfolio_url=st.session_state.get("s_portfolio", DEFAULT_PORTFOLIO_URL),
                    case_study=st.session_state.get("s_case_study", DEFAULT_CASE_STUDY),
                )
                if started:
                    st.success(f"✅ {_fu2_max} Follow-up 2 emails queued!")
                    time.sleep(1); st.rerun(scope="app")


with tab_followup:
    _render_followup()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 5 – Analytics
# ═════════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_analytics():
    _section("Analytics")

    a1, a2, a3, a4, a5, a6 = st.columns(6)
    with a1: _stat_card("Total Leads",     stats.get("total",   0), "#4f46e5")
    with a2: _stat_card("Ready to Send",   stats.get("new",     0), "#f59e0b")
    with a3: _stat_card("Queued / Active", stats.get("queued",  0), "#0ea5e9")
    with a4: _stat_card("Emails Sent",     stats.get("sent",    0), "#10b981")
    with a5: _stat_card("Failed",          stats.get("failed",  0), "#ef4444")
    with a6: _stat_card("No Email Found",  stats.get("no_email",0), "#94a3b8")

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    df_all = get_leads()
    if df_all.empty:
        st.info("No data yet. Find some leads first.")
    else:
        has_email = df_all["email"].notna() & (df_all["email"] != "")
        found_pct = round(has_email.sum() / len(df_all) * 100, 1)
        confirmed = int((df_all["email_source"] == "found").sum()) \
            if "email_source" in df_all.columns else 0
        inferred_n = int((df_all["email_source"] == "inferred").sum()) \
            if "email_source" in df_all.columns else 0
        guessed = int((df_all["email_source"] == "guessed").sum()) \
            if "email_source" in df_all.columns else 0

        # Email coverage bar
        st.markdown(
            f"<div style='background:white;border-radius:12px;padding:20px 24px;"
            f"border:1px solid #e9ecef;margin-bottom:16px;'>"
            f"<p style='font-size:12px;font-weight:700;color:#9ca3af;text-transform:uppercase;"
            f"letter-spacing:0.7px;margin:0 0 12px;'>Email coverage</p>"
            f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:12px;'>"
            f"<div style='flex:1;background:#f3f4f6;border-radius:99px;height:8px;overflow:hidden;'>"
            f"<div style='width:{found_pct}%;height:100%;background:#2563eb;border-radius:99px;'>"
            f"</div></div>"
            f"<span style='font-size:14px;font-weight:700;color:#111827;white-space:nowrap;'>"
            f"{found_pct}%</span></div>"
            f"<div style='display:flex;gap:16px;font-size:13px;color:#6b7280;'>"
            f"<span>{_badge('Found', '#15803d', '#f0fdf4')} {confirmed} confirmed on site</span>"
            f"<span>{_badge('Inferred', '#0369a1', '#f0f9ff')} {inferred_n} name-based</span>"
            f"<span>{_badge('Guessed', '#92400e', '#fffbeb')} {guessed} pattern fallback</span>"
            f"</div></div>",
            unsafe_allow_html=True,
        )

        _CHART_CFG = {"displayModeBar": False, "responsive": True}

        def _chart_layout(fig, height=280, xgrid=True):
            fig.update_layout(
                height=height,
                margin=dict(l=4, r=16, t=16, b=4),
                paper_bgcolor="white",
                plot_bgcolor="white",
                font=dict(family="Inter, Segoe UI, sans-serif", size=12, color="#374151"),
                xaxis=dict(
                    showgrid=xgrid, gridcolor="#f1f5f9", gridwidth=1,
                    zeroline=False, tickfont=dict(size=11, color="#94a3b8"),
                    showline=False,
                ),
                yaxis=dict(
                    showgrid=False, tickfont=dict(size=11, color="#374151"),
                    showline=False,
                ),
                showlegend=False,
                hoverlabel=dict(
                    bgcolor="white", bordercolor="#e2e8f0",
                    font=dict(family="Inter, sans-serif", size=12, color="#0f172a"),
                ),
            )
            return fig

        def _card(title):
            st.markdown(
                f"<div style='background:white;border-radius:12px;padding:16px 20px 4px;"
                f"border:1px solid #e9ecef;margin-bottom:4px;'>"
                f"<p style='font-size:13px;font-weight:600;color:#374151;margin:0;'>"
                f"{title}</p></div>",
                unsafe_allow_html=True,
            )

        ch1, ch2 = st.columns(2)

        # ── Leads by Country ──────────────────────────────────────────────────
        with ch1:
            _card("🌍 Leads by Country")
            cc = df_all.groupby("country").size().reset_index(name="count")
            cc = cc.sort_values("count", ascending=False).head(12)
            cc = cc.sort_values("count", ascending=True)  # flip for horizontal bar
            _n = max(1, len(cc))
            _cc_colors = [
                f"rgba(79,70,229,{0.40 + 0.60 * i / _n})" for i in range(_n)
            ]
            # Scale height tightly — never pad a 1-bar chart to 200px
            _cc_h = _n * 58 + 24
            _cc_bw = max(0.40, 0.82 - _n * 0.04)  # thicker bar when fewer rows
            fig_cc = go.Figure(go.Bar(
                x=cc["count"],
                y=cc["country"],
                orientation="h",
                marker=dict(
                    color=_cc_colors,
                    line=dict(width=0),
                    cornerradius=6,
                ),
                text=cc["count"],
                textposition="inside",
                insidetextanchor="end",
                textfont=dict(size=12, color="white", family="Inter, sans-serif"),
                hovertemplate="<b>%{y}</b><br>%{x} leads<extra></extra>",
                width=[_cc_bw] * _n,
            ))
            fig_cc.update_layout(
                height=_cc_h,
                margin=dict(l=8, r=8, t=8, b=8),
                paper_bgcolor="white",
                plot_bgcolor="white",
                bargap=0.18,
                font=dict(family="Inter, sans-serif", size=12, color="#374151"),
                hoverlabel=dict(
                    bgcolor="white", bordercolor="#e2e8f0",
                    font=dict(family="Inter, sans-serif", size=12),
                ),
            )
            fig_cc.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
            fig_cc.update_yaxes(
                tickfont=dict(size=12, color="#374151"),
                showgrid=False, zeroline=False,
            )
            st.plotly_chart(fig_cc, use_container_width=True, config=_CHART_CFG)

        # ── Leads by Status — horizontal bar (cleaner than donut in narrow col) ──
        with ch2:
            _card("📊 Leads by Status")
            sc_df = df_all.groupby("status").size().reset_index(name="count")
            sc_df = sc_df.sort_values("count", ascending=True)
            _sc_colors = {
                "new":      "#6366f1",
                "sent":     "#10b981",
                "no_email": "#94a3b8",
                "failed":   "#ef4444",
                "queued":   "#f59e0b",
                "bounced":  "#f87171",
                "replied":  "#0ea5e9",
                "skipped":  "#9ca3af",
            }
            _sc_labels = {
                "new": "New", "sent": "Sent", "no_email": "No Email",
                "failed": "Failed", "queued": "Queued", "bounced": "Bounced",
                "replied": "Replied", "skipped": "Skipped",
            }
            _ns = max(1, len(sc_df))
            _sc_h = _ns * 58 + 24
            _sc_bw = max(0.40, 0.82 - _ns * 0.04)
            fig_sc = go.Figure(go.Bar(
                x=sc_df["count"],
                y=[_sc_labels.get(s, s) for s in sc_df["status"]],
                orientation="h",
                marker=dict(
                    color=[_sc_colors.get(s, "#6366f1") for s in sc_df["status"]],
                    line=dict(width=0),
                    cornerradius=6,
                ),
                text=sc_df["count"],
                textposition="inside",
                insidetextanchor="end",
                textfont=dict(size=12, color="white", family="Inter, sans-serif"),
                hovertemplate="<b>%{y}</b><br>%{x} leads<extra></extra>",
                width=[_sc_bw] * _ns,
            ))
            fig_sc.update_layout(
                height=_sc_h,
                margin=dict(l=8, r=8, t=8, b=8),
                paper_bgcolor="white",
                plot_bgcolor="white",
                bargap=0.18,
                font=dict(family="Inter, sans-serif", size=12, color="#374151"),
                hoverlabel=dict(
                    bgcolor="white", bordercolor="#e2e8f0",
                    font=dict(family="Inter, sans-serif", size=12),
                ),
            )
            fig_sc.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
            fig_sc.update_yaxes(
                tickfont=dict(size=12, color="#374151"),
                showgrid=False, zeroline=False,
            )
            st.plotly_chart(fig_sc, use_container_width=True, config=_CHART_CFG)

        # ── Daily Lead Volume ─────────────────────────────────────────────────
        _card("📈 Daily Lead Volume")
        df_all["date"] = pd.to_datetime(df_all["created_at"]).dt.date
        daily = df_all.groupby("date").size().reset_index(name="count")
        daily["date"] = pd.to_datetime(daily["date"])

        if len(daily) == 1:
            fig_daily = go.Figure(go.Bar(
                x=daily["date"], y=daily["count"],
                marker=dict(color="#4f46e5", line=dict(width=0)),
                text=daily["count"], textposition="outside",
                hovertemplate="<b>%{x|%b %d}</b><br>%{y} leads<extra></extra>",
            ))
        else:
            fig_daily = go.Figure()
            fig_daily.add_trace(go.Scatter(
                x=daily["date"], y=daily["count"],
                mode="lines+markers",
                line=dict(color="#4f46e5", width=2.5, shape="spline", smoothing=0.8),
                fill="tozeroy",
                fillcolor="rgba(79,70,229,0.08)",
                marker=dict(size=6, color="#4f46e5",
                            line=dict(color="white", width=2)),
                hovertemplate="<b>%{x|%b %d}</b><br>%{y} leads<extra></extra>",
            ))
        _chart_layout(fig_daily, height=240)
        fig_daily.update_xaxes(tickformat="%b %d", tickfont=dict(size=11, color="#94a3b8"))
        fig_daily.update_yaxes(tickfont=dict(size=11, color="#94a3b8"))
        st.plotly_chart(fig_daily, use_container_width=True, config=_CHART_CFG)

        # ── Top Keywords ──────────────────────────────────────────────────────
        _card("🔑 Top Keywords")
        kc = df_all.groupby("keyword").size().reset_index(name="count")
        kc = kc.sort_values("count", ascending=False).head(15)
        kc = kc.sort_values("count", ascending=True)
        _nk = max(1, len(kc))
        _kc_colors = [
            f"rgba(16,185,129,{0.40 + 0.60 * i / _nk})" for i in range(_nk)
        ]
        fig_kc = go.Figure(go.Bar(
            x=kc["count"],
            y=kc["keyword"],
            orientation="h",
            marker=dict(
                color=_kc_colors,
                line=dict(width=0),
                cornerradius=4,
            ),
            text=kc["count"],
            textposition="inside",
            insidetextanchor="end",
            textfont=dict(size=12, color="white", family="Inter, sans-serif"),
            hovertemplate="<b>%{y}</b><br>%{x} leads<extra></extra>",
            width=[0.55] * _nk,
        ))
        fig_kc.update_layout(
            height=_nk * 58 + 24,
            margin=dict(l=8, r=8, t=8, b=8),
            paper_bgcolor="white",
            plot_bgcolor="white",
            bargap=0.18,
            font=dict(family="Inter, sans-serif", size=12, color="#374151"),
            hoverlabel=dict(
                bgcolor="white", bordercolor="#e2e8f0",
                font=dict(family="Inter, sans-serif", size=12),
            ),
        )
        fig_kc.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
        fig_kc.update_yaxes(
            tickfont=dict(size=11, color="#374151"),
            showgrid=False, zeroline=False,
        )
        st.plotly_chart(fig_kc, use_container_width=True, config=_CHART_CFG)

    # ── Brevo email performance ───────────────────────────────────────────────
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _brevo_analytics = st.session_state.get("s_brevo","") or st.secrets.get("brevo_key","")
    if _brevo_analytics:
        _section("Brevo Email Performance",
                 "Live open & click stats from your Brevo account (last 30 days).")
        with st.spinner("Loading Brevo stats…"):
            _agg = brevo_sender.get_aggregate_stats(_brevo_analytics, days=30)
        if _agg:
            _delivered  = int(_agg.get("delivered",  0))
            _opens      = int(_agg.get("uniqueOpens", _agg.get("opens", 0)))
            _clicks     = int(_agg.get("uniqueClicks", _agg.get("clicks", 0)))
            _hard_b     = int(_agg.get("hardBounces", 0))
            _soft_b     = int(_agg.get("softBounces", 0))
            _bounces    = _hard_b + _soft_b
            _open_rate  = round(_opens  / _delivered * 100, 1) if _delivered else 0.0
            _click_rate = round(_clicks / _delivered * 100, 1) if _delivered else 0.0

            ba1, ba2, ba3, ba4, ba5 = st.columns(5)
            with ba1: _stat_card("Delivered (30d)",  _delivered,           "#10b981")
            with ba2: _stat_card("Unique Opens",     _opens,               "#4f46e5")
            with ba3: _stat_card("Open Rate",        f"{_open_rate}%",     "#7c3aed")
            with ba4: _stat_card("Click Rate",       f"{_click_rate}%",    "#0ea5e9")
            with ba5: _stat_card("Bounced",          _bounces,             "#ef4444")

            # Open-rate progress bar
            st.markdown(
                f"<div style='background:white;border-radius:12px;padding:20px 24px;"
                f"border:1px solid #e9ecef;margin:12px 0;'>"
                f"<p style='font-size:12px;font-weight:700;color:#9ca3af;text-transform:uppercase;"
                f"letter-spacing:0.7px;margin:0 0 12px;'>Email performance benchmarks</p>"
                f"<div style='margin-bottom:10px;'>"
                f"<div style='display:flex;justify-content:space-between;font-size:12px;"
                f"color:#6b7280;margin-bottom:5px;'>"
                f"<span>Open rate &nbsp;<b style='color:#0f172a;'>{_open_rate}%</b></span>"
                f"<span style='color:#9ca3af;'>Industry avg: 20–25%</span></div>"
                f"<div style='background:#f3f4f6;border-radius:99px;height:7px;overflow:hidden;'>"
                f"<div style='width:{min(_open_rate,100)}%;height:100%;"
                f"background:#4f46e5;border-radius:99px;'></div></div></div>"
                f"<div>"
                f"<div style='display:flex;justify-content:space-between;font-size:12px;"
                f"color:#6b7280;margin-bottom:5px;'>"
                f"<span>Click rate &nbsp;<b style='color:#0f172a;'>{_click_rate}%</b></span>"
                f"<span style='color:#9ca3af;'>Industry avg: 2–5%</span></div>"
                f"<div style='background:#f3f4f6;border-radius:99px;height:7px;overflow:hidden;'>"
                f"<div style='width:{min(_click_rate*4,100)}%;height:100%;"
                f"background:#0ea5e9;border-radius:99px;'></div></div></div>"
                f"</div>",
                unsafe_allow_html=True,
            )

            # Sync open/click events back to local DB
            if st.button("🔄 Sync open/click events from Brevo → local DB", key="sync_brevo"):
                with st.spinner("Fetching events from Brevo…"):
                    _events = brevo_sender.get_email_events(_brevo_analytics, limit=500)
                _synced = 0
                for evt in _events:
                    _evt_email = evt.get("email", "")
                    _evt_type  = evt.get("event", "")
                    _evt_date  = evt.get("date",  "")
                    if not _evt_email or not _evt_type:
                        continue
                    _existing_lead = get_lead_by_email(_evt_email)
                    if _existing_lead:
                        _lid = _existing_lead["id"]
                        if _evt_type == "opened" and not _existing_lead.get("opened_at"):
                            update_tracking(_lid, opened_at=_evt_date)
                            _synced += 1
                        elif _evt_type in ("clicks", "clicked") and not _existing_lead.get("clicked_at"):
                            update_tracking(_lid, clicked_at=_evt_date)
                            _synced += 1
                st.success(f"✅ Synced {_synced} tracking event(s) to your leads database.")

            # Leads that opened / clicked
            _df_opened  = (df_all[df_all["opened_at"].notna()]
                           if "opened_at"  in df_all.columns else pd.DataFrame())
            _df_clicked = (df_all[df_all["clicked_at"].notna()]
                           if "clicked_at" in df_all.columns else pd.DataFrame())
            if not _df_opened.empty or not _df_clicked.empty:
                oa, ca = st.columns(2)
                with oa:
                    st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;"
                                "margin-bottom:6px;'>Leads who opened your email</p>",
                                unsafe_allow_html=True)
                    _oc = [c for c in ["business_name","email","city","opened_at"]
                           if c in _df_opened.columns]
                    if not _df_opened.empty:
                        st.dataframe(_df_opened[_oc], use_container_width=True,
                                     hide_index=True, height=200)
                    else:
                        st.caption("None yet — check back after syncing.")
                with ca:
                    st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;"
                                "margin-bottom:6px;'>Leads who clicked a link</p>",
                                unsafe_allow_html=True)
                    _cc2 = [c for c in ["business_name","email","city","clicked_at"]
                            if c in _df_clicked.columns]
                    if not _df_clicked.empty:
                        st.dataframe(_df_clicked[_cc2], use_container_width=True,
                                     hide_index=True, height=200)
                    else:
                        st.caption("None yet — check back after syncing.")
        else:
            st.info("No Brevo stats yet. Stats appear after your first Brevo send (may take a few minutes).")
    else:
        st.markdown(
            "<div style='background:#fefce8;border:1px solid #fde68a;"
            "border-radius:10px;padding:16px 20px;margin-bottom:8px;'>"
            "<p style='font-size:13px;font-weight:700;color:#92400e;margin:0 0 4px;'>"
            "📡 Add Brevo for open &amp; click tracking</p>"
            "<p style='font-size:12px;color:#78350f;margin:0;line-height:1.6;'>"
            "Connect your free Brevo account (sidebar → Brevo API key) to see open rates, "
            "click rates and bounce stats here. Free tier: 300 emails/day.</p>"
            "</div>",
            unsafe_allow_html=True,
        )

    # ── Deliverability Health ─────────────────────────────────────────────────
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _section("Deliverability Health",
             "Check that your emails are actually reaching inboxes, not spam folders.")

    _brevo_deliv = st.session_state.get("s_brevo","") or st.secrets.get("brevo_key","")
    _from_email  = st.session_state.get("s_email","") or st.secrets.get("smtp_email","")
    _from_domain = _from_email.split("@")[1] if "@" in _from_email else ""

    dv1, dv2, dv3 = st.columns(3)

    # ── Card 1: SPF check ─────────────────────────────────────────────────────
    with dv1:
        spf_ok = False
        spf_record = ""
        if _from_domain:
            try:
                _spf_r = requests.get(
                    f"https://dns.google/resolve?name={_from_domain}&type=TXT",
                    timeout=8,
                )
                if _spf_r.status_code == 200:
                    for ans in _spf_r.json().get("Answer", []):
                        d = ans.get("data", "")
                        if "v=spf1" in d:
                            spf_ok = True
                            spf_record = d[:60]
            except Exception:
                pass
        _spf_color = "#10b981" if spf_ok else "#ef4444"
        _spf_icon  = "✅" if spf_ok else "❌"
        _spf_label = "SPF record found" if spf_ok else "No SPF record"
        st.markdown(
            f"<div style='background:white;border-radius:12px;padding:18px 20px;"
            f"border:1px solid #e9ecef;height:100%;'>"
            f"<p style='font-size:11px;font-weight:700;color:#9ca3af;"
            f"text-transform:uppercase;letter-spacing:0.7px;margin:0 0 8px;'>SPF Record</p>"
            f"<p style='font-size:22px;margin:0 0 4px;'>{_spf_icon}</p>"
            f"<p style='font-size:13px;font-weight:600;color:{_spf_color};margin:0 0 4px;'>"
            f"{_spf_label}</p>"
            f"<p style='font-size:11px;color:#9ca3af;margin:0;'>"
            f"{'Authenticated ✓' if spf_ok else 'May land in spam'}</p>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Card 2: DMARC check ───────────────────────────────────────────────────
    with dv2:
        dmarc_ok = False
        if _from_domain:
            try:
                _dm_r = requests.get(
                    f"https://dns.google/resolve?name=_dmarc.{_from_domain}&type=TXT",
                    timeout=8,
                )
                if _dm_r.status_code == 200:
                    for ans in _dm_r.json().get("Answer", []):
                        if "v=DMARC1" in ans.get("data", ""):
                            dmarc_ok = True
            except Exception:
                pass
        _dm_color = "#10b981" if dmarc_ok else "#f59e0b"
        _dm_icon  = "✅" if dmarc_ok else "⚠️"
        _dm_label = "DMARC record found" if dmarc_ok else "No DMARC record"
        st.markdown(
            f"<div style='background:white;border-radius:12px;padding:18px 20px;"
            f"border:1px solid #e9ecef;height:100%;'>"
            f"<p style='font-size:11px;font-weight:700;color:#9ca3af;"
            f"text-transform:uppercase;letter-spacing:0.7px;margin:0 0 8px;'>DMARC Record</p>"
            f"<p style='font-size:22px;margin:0 0 4px;'>{_dm_icon}</p>"
            f"<p style='font-size:13px;font-weight:600;color:{_dm_color};margin:0 0 4px;'>"
            f"{_dm_label}</p>"
            f"<p style='font-size:11px;color:#9ca3af;margin:0;'>"
            f"{'Policy enforced ✓' if dmarc_ok else 'Recommended for trust'}</p>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Card 3: Bounce rate health ────────────────────────────────────────────
    with dv3:
        _b_delivered = 0
        _b_bounces   = 0
        _bounce_rate = 0.0
        if _brevo_deliv:
            try:
                _b_agg = brevo_sender.get_aggregate_stats(_brevo_deliv, days=30)
                _b_delivered = int(_b_agg.get("delivered", 0))
                _b_bounces   = (int(_b_agg.get("hardBounces", 0)) +
                                int(_b_agg.get("softBounces", 0)))
                _bounce_rate = round(_b_bounces / _b_delivered * 100, 1) \
                               if _b_delivered else 0.0
            except Exception:
                pass
        _br_color = ("#10b981" if _bounce_rate < 2
                     else "#f59e0b" if _bounce_rate < 5 else "#ef4444")
        _br_icon  = "✅" if _bounce_rate < 2 else ("⚠️" if _bounce_rate < 5 else "🚨")
        _br_label = ("Healthy" if _bounce_rate < 2
                     else "Needs attention" if _bounce_rate < 5 else "Critical — fix now")
        st.markdown(
            f"<div style='background:white;border-radius:12px;padding:18px 20px;"
            f"border:1px solid #e9ecef;height:100%;'>"
            f"<p style='font-size:11px;font-weight:700;color:#9ca3af;"
            f"text-transform:uppercase;letter-spacing:0.7px;margin:0 0 8px;'>Bounce Rate</p>"
            f"<p style='font-size:22px;margin:0 0 4px;'>{_br_icon}</p>"
            f"<p style='font-size:13px;font-weight:600;color:{_br_color};margin:0 0 4px;'>"
            f"{_bounce_rate}% — {_br_label}</p>"
            f"<p style='font-size:11px;color:#9ca3af;margin:0;'>"
            f"Keep below 2% to protect sender reputation</p>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    # ── Test email sender ─────────────────────────────────────────────────────
    st.markdown(
        "<div style='background:white;border-radius:12px;padding:20px 24px;"
        "border:1px solid #e9ecef;'>"
        "<p style='font-size:13px;font-weight:700;color:#111827;margin:0 0 4px;'>"
        "📬 Send a test email to yourself</p>"
        "<p style='font-size:12px;color:#6b7280;margin:0;'>"
        "Send via Brevo to any inbox and check whether it lands in inbox or spam.</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _te1, _te2 = st.columns([3, 1])
    with _te1:
        _test_recipient = st.text_input(
            "Your email address",
            placeholder="yourname@gmail.com",
            key="test_email_recipient",
            label_visibility="collapsed",
        )
    with _te2:
        _send_test = st.button("Send Test Email", use_container_width=True,
                               key="btn_send_test")

    if _send_test:
        if not _brevo_deliv:
            st.error("Add your Brevo API key in the sidebar first.")
        elif not _from_email:
            st.error("Add your From address in the sidebar first.")
        elif not _test_recipient or "@" not in _test_recipient:
            st.error("Enter a valid email address.")
        else:
            _brevo_name = (st.session_state.get("s_brevo_name", "") or "Aaron Pearson")
            _test_html  = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px;padding:24px;
                        border:1px solid #e5e7eb;border-radius:8px;">
              <p style="font-size:15px;color:#111827;">Hi there,</p>
              <p style="font-size:14px;color:#374151;line-height:1.7;">
                This is a deliverability test email sent from your
                <b>SEO Outreach Engine</b>.<br><br>
                If you're reading this in your <b>inbox</b> — great, your
                emails are landing correctly!<br><br>
                If this arrived in <b>spam</b>, go to
                <a href="https://mail-tester.com">mail-tester.com</a> for
                a detailed score and fix suggestions.
              </p>
              <p style="font-size:13px;color:#9ca3af;margin-top:24px;">
                Sent by {_brevo_name} · {_from_email}
              </p>
            </div>"""
            _test_text = (
                f"Hi,\n\nThis is a deliverability test from your SEO Outreach Engine.\n\n"
                f"If you see this in your inbox — your emails are landing correctly.\n"
                f"If it's in spam — visit mail-tester.com for a detailed fix.\n\n"
                f"— {_brevo_name}"
            )
            with st.spinner("Sending test email…"):
                _tok, _tres = brevo_sender.send_email(
                    api_key=_brevo_deliv,
                    sender_email=_from_email,
                    sender_name=_brevo_name,
                    recipient_email=_test_recipient,
                    recipient_name="Test",
                    subject="✉️ Deliverability test — did this land in inbox?",
                    html_content=_test_html,
                    text_content=_test_text,
                    tags=["deliverability-test"],
                )
            if _tok:
                st.success(
                    f"✅ Sent! Check **{_test_recipient}** — "
                    f"is it in inbox or spam? "
                    f"For a detailed score visit [mail-tester.com](https://mail-tester.com)."
                )
            else:
                st.error(f"Failed: {_tres}")

    # ── Search history table ──────────────────────────────────────────────────
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _section("Search History", "Every keyword × location combination you have ever searched.")
    _sh = get_search_history(limit=50)
    if _sh.empty:
        st.info("No searches recorded yet.")
    else:
        _sh_disp = _sh[["keyword", "location", "country",
                         "results_count", "new_leads", "created_at"]].copy()
        _sh_disp.columns = ["Keyword", "Location", "Country",
                            "Businesses found", "New leads", "Searched at"]
        _sh_disp["Searched at"] = pd.to_datetime(
            _sh_disp["Searched at"]).dt.strftime("%d %b %Y  %H:%M")
        st.dataframe(_sh_disp, use_container_width=True, hide_index=True, height=320)

        # Summary by unique combo
        _combos = (
            _sh.groupby(["keyword", "location", "country"])
            .agg(searches=("id", "count"), total_found=("results_count", "sum"),
                 total_new=("new_leads", "sum"), last_run=("created_at", "max"))
            .reset_index().sort_values("last_run", ascending=False)
        )
        st.markdown(
            "<p style='font-size:13px;font-weight:600;color:#374151;margin:16px 0 6px;'>"
            "Unique keyword × location combinations</p>", unsafe_allow_html=True)
        st.dataframe(_combos.rename(columns={
            "keyword": "Keyword", "location": "Location", "country": "Country",
            "searches": "Times run", "total_found": "Total businesses",
            "total_new": "Total new leads", "last_run": "Last run",
        }), use_container_width=True, hide_index=True)


with tab_analytics:
    _render_analytics()
