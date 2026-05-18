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
    update_tracking, delete_search_history,
)
from email_finder import find_email_on_website
import brevo_sender
from email_sender import send_email as _smtp_send_one
from scraper import COUNTRY_SCRAPERS, find_businesses
from templates import (
    EMAIL_TEMPLATE_HTML, SUBJECT_LINES,
    TEMPLATE_OPTIONS, FOLLOWUP_OPTIONS,
    build_html, build_text,
    get_random_subject, get_followup_subject,
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
        html_body = build_html(tpl_key, biz_name, sender_name, sender_email)
        text_body = build_text(tpl_key, biz_name, sender_name, sender_email)
        subject   = (get_followup_subject(biz_name, is_followup)
                     if is_followup else get_random_subject(biz_name))

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
            )
            if ok:
                message_id = result
                # Sync contact to Brevo CRM
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
                    brevo_key: str = "", is_followup: int = 0) -> bool:
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
              delay_secs, template, brevo_key, is_followup),
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


def _login_page() -> bool:
    if st.session_state.get("_authenticated"):
        return True

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

        html, body, .stApp { background: #f8f9fb !important; }
        .stApp > .main, .block-container,
        .block-container > div, .block-container > div > div {
            padding: 0 !important; margin: 0 !important;
            max-width: 100% !important; width: 100% !important;
            background: #f8f9fb !important;
        }

        /* ── Google button — target every possible selector ── */
        [data-testid="stBaseButton-secondary"],
        [data-testid="stLinkButton"] a,
        .stLinkButton a,
        .stButton > button {
            background: white !important;
            border: 1.5px solid #e2e8f0 !important;
            border-radius: 99px !important;
            color: #1e293b !important;
            font-size: 15px !important;
            font-weight: 600 !important;
            height: 50px !important;
            box-shadow: 0 2px 12px rgba(0,0,0,0.10) !important;
            transition: all .15s !important;
        }
        [data-testid="stBaseButton-secondary"]:hover,
        [data-testid="stLinkButton"] a:hover,
        .stLinkButton a:hover,
        .stButton > button:hover {
            box-shadow: 0 6px 24px rgba(0,0,0,0.14) !important;
            transform: translateY(-1px) !important;
            border-color: #a5b4fc !important;
        }
        /* Remove Streamlit's default element spacing in login column */
        [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"],
        [data-testid="stVerticalBlock"] > div { gap: 0 !important; }
    </style>
    """, unsafe_allow_html=True)

    # ── Single centred column ─────────────────────────────────────────────────
    _, centre, _ = st.columns([1, 1.2, 1])

    with centre:
        st.markdown("<div style='height:8vh'></div>", unsafe_allow_html=True)

        # ── Everything in one white card ──────────────────────────────────
        st.markdown("""
        <div style="background:white; border-radius:24px;
                    border:1px solid #e8ecf0;
                    box-shadow:0 8px 40px rgba(0,0,0,0.10);
                    padding:40px 44px 32px; text-align:center;">

          <div style="display:inline-flex; align-items:center; justify-content:center;
                      width:56px; height:56px; background:#f0f4ff;
                      border-radius:16px; font-size:26px; margin-bottom:20px;
                      box-shadow:0 2px 8px rgba(79,70,229,0.15);">
            🚀
          </div>

          <div style="font-size:22px; font-weight:800; color:#0f172a;
                      letter-spacing:-0.5px; margin-bottom:6px;
                      font-family:'Inter','Segoe UI',sans-serif;">
            SEO Outreach Engine
          </div>
          <div style="font-size:13px; color:#94a3b8; margin-bottom:28px;">
            Find leads · Extract emails · Close clients
          </div>

          <div style="height:1px; background:#f1f5f9; margin:0 -44px 28px;"></div>

          <div style="font-size:12px; font-weight:600; color:#64748b;
                      text-transform:uppercase; letter-spacing:0.8px; margin-bottom:14px;">
            Sign in to your workspace
          </div>

        </div>
        """, unsafe_allow_html=True)

        # ── OAuth button — centred with padding on sides ──────────────────
        oauth2 = OAuth2Component(
            CLIENT_ID, CLIENT_SECRET,
            _GOOGLE_AUTH_URL,
            _GOOGLE_TOKEN_URL, _GOOGLE_TOKEN_URL,
            _GOOGLE_REVOKE_URL,
        )
        _b1, _b2, _b3 = st.columns([1, 3, 1])
        with _b2:
            result = oauth2.authorize_button(
                name="Continue with Google",
                redirect_uri=REDIRECT_URI,
                scope="openid email profile",
                icon="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg",
                use_container_width=True,
                key="google_login_btn",
            )

        # ── Trust badges + footer ─────────────────────────────────────────
        st.markdown("""
        <div style="text-align:center; margin-top:24px;">
          <div style="display:flex; justify-content:center; align-items:center;
                      gap:14px; flex-wrap:wrap; margin-bottom:16px;">
            <span style="font-size:11px; color:#cbd5e1;">🔒 OAuth 2.0 secured</span>
            <span style="color:#e2e8f0;">·</span>
            <span style="font-size:11px; color:#cbd5e1;">✅ Google verified</span>
            <span style="color:#e2e8f0;">·</span>
            <span style="font-size:11px; color:#cbd5e1;">🔐 Private access only</span>
          </div>
          <div style="font-size:11px; color:#cbd5e1;">
            SEO Outreach Engine &nbsp;·&nbsp; Built for B2B cold email
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("<div style='height:5vh'></div>", unsafe_allow_html=True)

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
            st.rerun()
        else:
            st.error(f"🚫 Access denied for `{email}`. This account has not been granted access.")

    return False


if not _login_page():
    st.stop()

init_db()

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
    gap: 2px; background: white;
    border: 1px solid #e2e8f0; border-radius: 10px;
    padding: 4px; margin-bottom: 24px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 7px !important; padding: 8px 18px !important;
    font-weight: 600 !important; font-size: 13px !important;
    color: #64748b !important; background: transparent !important;
    border: none !important; transition: all .12s !important;
}
.stTabs [aria-selected="true"] {
    color: #0f172a !important; background: #f1f5f9 !important;
    box-shadow: 0 1px 3px rgba(0,0,0,.08) !important;
}

/* ── Primary button ── */
.stButton > button[kind="primary"] {
    background: #4f46e5 !important; border: none !important;
    border-radius: 8px !important; font-weight: 600 !important;
    font-size: 14px !important; color: white !important;
    padding: 10px 22px !important; letter-spacing: -0.1px !important;
    transition: all .12s !important; box-shadow: 0 1px 3px rgba(79,70,229,.3) !important;
}
.stButton > button[kind="primary"]:hover {
    background: #4338ca !important; box-shadow: 0 4px 12px rgba(79,70,229,.35) !important;
    transform: translateY(-1px) !important;
}
.stButton > button[kind="secondary"] {
    background: white !important; border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important; font-weight: 500 !important;
    font-size: 13px !important; color: #374151 !important;
    transition: all .12s !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: #cbd5e1 !important; background: #f8fafc !important;
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
    border: 1px solid #e2e8f0 !important; border-radius: 10px !important;
    overflow: hidden !important; box-shadow: none !important;
}

/* ── Alerts ── */
.stAlert { border-radius: 10px !important; }
[data-testid="stAlert"] { border-radius: 10px !important; }

/* ── Expander ── */
.stExpander { border: 1px solid #e2e8f0 !important; border-radius: 10px !important; }

/* ── Progress bar ── */
[data-testid="stProgressBar"] > div > div {
    background: #4f46e5 !important; border-radius: 99px !important;
}

/* ── Slider ── */
[data-testid="stSlider"] [role="slider"] { background: #4f46e5 !important; }

/* ── Hide Streamlit chrome ── */
#MainMenu, footer, header,
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"] { display: none !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section(title: str, subtitle: str = ""):
    sub = (f'<p style="font-size:13px;color:#64748b;margin:3px 0 0;font-weight:400;">'
           f'{subtitle}</p>') if subtitle else ""
    st.markdown(
        f'<div style="margin-bottom:22px;">'
        f'<h3 style="font-size:17px;font-weight:700;color:#0f172a;margin:0;'
        f'letter-spacing:-0.3px;">{title}</h3>{sub}</div>',
        unsafe_allow_html=True,
    )


def _card(content_html: str):
    st.markdown(
        f'<div style="background:white;border-radius:12px;padding:24px;'
        f'border:1px solid #e2e8f0;margin-bottom:16px;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.04);">{content_html}</div>',
        unsafe_allow_html=True,
    )


def _stat_card(label: str, value, color: str, sub: str = "", icon: str = ""):
    sub_html = (f'<div style="font-size:11px;color:#94a3b8;margin-top:5px;'
                f'font-weight:500;">{sub}</div>') if sub else ""
    icon_html = (f'<div style="font-size:22px;margin-bottom:10px;line-height:1;">'
                 f'{icon}</div>') if icon else ""
    accent = (f'<div style="width:3px;height:36px;background:{color};'
              f'border-radius:3px;flex-shrink:0;"></div>')
    st.markdown(
        f'<div style="background:white;border-radius:12px;padding:18px 20px;'
        f'border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,.04);'
        f'display:flex;align-items:center;gap:14px;min-height:116px;height:100%;'
        f'box-sizing:border-box;">'
        f'{accent}'
        f'<div style="flex:1;min-width:0;">'
        f'{icon_html}'
        f'<div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:0.8px;margin-bottom:5px;white-space:nowrap;">{label}</div>'
        f'<div style="font-size:28px;font-weight:800;color:#0f172a;line-height:1;'
        f'letter-spacing:-1px;">{value}</div>'
        f'{sub_html}'
        f'</div></div>',
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
        "<div style='padding:20px 0 12px;border-bottom:1px solid #1e293b;margin-bottom:12px;'>"
        "<div style='display:flex;align-items:center;gap:8px;'>"
        "<div style='width:28px;height:28px;background:#4f46e5;border-radius:7px;"
        "display:flex;align-items:center;justify-content:center;font-size:14px;'>🚀</div>"
        "<div>"
        "<div style='font-size:14px;font-weight:700;color:#f1f5f9;letter-spacing:-0.3px;'>"
        "SEO Outreach</div>"
        "<div style='font-size:10px;color:#475569;letter-spacing:0.3px;'>B2B COLD EMAIL ENGINE</div>"
        "</div></div></div>",
        unsafe_allow_html=True,
    )

    # User pill
    user_name  = st.session_state.get("_user_name", "")
    user_email = st.session_state.get("_user_email", "")
    initials   = "".join(w[0].upper() for w in user_name.split()[:2]) if user_name else "?"
    st.markdown(
        f"<div style='background:#1e293b;border-radius:8px;padding:10px 12px;"
        f"margin:8px 0;display:flex;align-items:center;gap:10px;'>"
        f"<div style='width:30px;height:30px;border-radius:50%;background:#2563eb;"
        f"display:flex;align-items:center;justify-content:center;"
        f"font-size:12px;font-weight:700;color:white;flex-shrink:0;'>{initials}</div>"
        f"<div style='min-width:0;'>"
        f"<div style='font-size:12px;font-weight:600;color:#e2e8f0;"
        f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{user_name}</div>"
        f"<div style='font-size:11px;color:#475569;white-space:nowrap;"
        f"overflow:hidden;text-overflow:ellipsis;'>{user_email}</div>"
        f"</div></div>",
        unsafe_allow_html=True,
    )
    if st.button("Sign out", use_container_width=True):
        for key in ["_authenticated", "_user_email", "_user_name", "google_login_btn"]:
            st.session_state.pop(key, None)
        st.rerun()

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
            _today = brevo_sender.get_today_stats(_brevo_sidebar)
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
                f"<span style='font-size:10px;color:#334155;"
                f"background:#1e293b;border-radius:4px;padding:1px 5px;"
                f"font-weight:600;'>Brevo free tier</span>"
                f"</div>"
                f"</div>",
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
        _def_brevo_name = st.secrets.get("brevo_sender_name", "Aaron Pearson")
        brevo_sender_name = st.text_input(
            "Sender name (business emails)",
            value=_def_brevo_name,
            placeholder="Aaron Pearson",
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
with tab_find:
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
                st.rerun()

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

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
                    email, email_source = find_email_on_website(biz["website"])
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
                        icon = "🤔" if email_source == "guessed" else "📧"
                        note = "  (pattern guess)" if email_source == "guessed" else "  ✅"
                        log(f"    {icon}  {email}{note}")
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
                    df_prev["Email status"] = df_prev["email_source"].map(
                        lambda s: "✅ Found" if s == "found" else ("🤔 Guessed" if s == "guessed" else "—")
                    )
                show_cols = [c for c in
                    ["business_name","email","Email status","phone","website","address","city","source"]
                    if c in df_prev.columns]
                st.dataframe(df_prev[show_cols], use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 – Leads Database
# ═════════════════════════════════════════════════════════════════════════════
with tab_db:
    _section("Leads Database", "All businesses found so far. Sent leads are never emailed again.")

    f1, f2 = st.columns([3, 1])
    with f1:
        status_filter = st.selectbox("Filter", ["all", "new", "sent", "failed", "no_email"],
            format_func=lambda x: {"all":"All leads","new":"New — not yet emailed",
                "sent":"Sent","failed":"Failed","no_email":"No email found"}.get(x, x))
    with f2:
        st.markdown("<div style='height:27px'></div>", unsafe_allow_html=True)
        if st.button("🔄 Refresh", use_container_width=True): st.rerun()

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
            df_db["Email source"] = df_db["email_source"].map(
                lambda s: "Found" if s=="found" else ("Guessed" if s=="guessed" else "—"))
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
                    delete_leads(ids); st.success(f"Deleted {len(ids)} lead(s)."); st.rerun()
                except ValueError:
                    st.error("Use numbers separated by commas.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 – Send Emails
# ═════════════════════════════════════════════════════════════════════════════
with tab_send:
    _section("Send Emails",
             "Only 'New' leads appear here. Once sent, a lead is marked Sent and never emailed again.")

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
                    st.rerun()
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

        # Split confirmed vs guessed
        if "email_source" in df_ready.columns and not df_ready.empty:
            df_confirmed = df_ready[df_ready["email_source"] == "found"].copy()
            df_guessed   = df_ready[df_ready["email_source"] != "found"].copy()
        else:
            df_confirmed = df_ready.copy()
            df_guessed   = pd.DataFrame()

        confirmed_ct = len(df_confirmed)
        guessed_ct   = len(df_guessed)

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
                    st.rerun()

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

        if confirmed_ct == 0:
            if not _is_running:
                st.markdown(
                    "<div style='background:white;border-radius:12px;padding:48px 24px;"
                    "text-align:center;border:1px solid #e9ecef;'>"
                    "<p style='font-size:32px;margin:0 0 12px;'>📭</p>"
                    "<p style='font-size:16px;font-weight:600;color:#111827;margin:0 0 6px;'>"
                    "No confirmed leads ready</p>"
                    "<p style='font-size:13px;color:#9ca3af;margin:0;'>"
                    "Go to Find Leads to scrape more businesses.</p>"
                    "</div>", unsafe_allow_html=True)
        else:
            # Summary stats
            s1, s2, s3 = st.columns(3)
            with s1: _stat_card("Confirmed leads ready", confirmed_ct, "#16a34a",
                                 "extracted from website")
            with s2:
                est_mins = max(1, (min(confirmed_ct, 20) * delay_sec) // 60)
                _stat_card("Est. for 20 emails", f"~{est_mins} min", "#6b7280")
            with s3: _stat_card("Delay between sends", f"{delay_sec}s", "#7c3aed")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

            # Confirmed leads table
            _c_disp = df_confirmed.copy()
            _c_disp["Email"] = "✅ " + _c_disp["email"].fillna("")
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

            sc1, sc2 = st.columns([3, 1])
            with sc1:
                max_send = st.slider(
                    "How many emails to queue?",
                    1, min(200, confirmed_ct), min(20, confirmed_ct),
                )
            with sc2:
                est_total = max(1, (max_send * delay_sec) // 60)
                st.metric("Est. total time", f"~{est_total} min")

            _brevo_key_send = (st.session_state.get("s_brevo","")
                               or st.secrets.get("brevo_key",""))
            method_label = "via Brevo" if _brevo_key_send else "via Gmail SMTP"

            if _is_running:
                st.warning("⏳ A send is already running. Wait or stop it first.")
            else:
                if st.button(
                    f"📤 Queue {max_send} email{'s' if max_send > 1 else ''} "
                    f"& send in background {method_label}",
                    type="primary", use_container_width=True,
                ):
                    ids = df_confirmed.head(max_send)["id"].astype(int).tolist()
                    _eff_name = (st.session_state.get("s_brevo_name","") or "Aaron Pearson") \
                                if _brevo_key_send else sender_name
                    started = _queue_and_send(
                        ids, sender_email, app_password, _eff_name,
                        delay_sec, tpl_choice, _brevo_key_send,
                    )
                    if started:
                        st.success(
                            f"✅ {max_send} email(s) queued! Sending in background "
                            f"{method_label}. Navigate away — check progress anytime."
                        )
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error("Could not start — another send is already running.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 – Follow-ups
# ═════════════════════════════════════════════════════════════════════════════
with tab_followup:
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
                _eff_name_fu1 = (st.session_state.get("s_brevo_name","") or "Aaron Pearson") \
                                if _brevo_fu else sender_name
                started = _queue_and_send(
                    ids, sender_email, app_password, _eff_name_fu1,
                    delay_sec, "followup1", _brevo_fu, is_followup=1,
                )
                if started:
                    st.success(f"✅ {_fu1_max} Follow-up 1 emails queued!")
                    time.sleep(1); st.rerun()

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
                _eff_name_fu2 = (st.session_state.get("s_brevo_name","") or "Aaron Pearson") \
                                if _brevo_fu else sender_name
                started = _queue_and_send(
                    ids, sender_email, app_password, _eff_name_fu2,
                    delay_sec, "followup2", _brevo_fu, is_followup=2,
                )
                if started:
                    st.success(f"✅ {_fu2_max} Follow-up 2 emails queued!")
                    time.sleep(1); st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 5 – Analytics
# ═════════════════════════════════════════════════════════════════════════════
with tab_analytics:
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
            f"<span>{_badge('Guessed', '#92400e', '#fffbeb')} {guessed} pattern fallback</span>"
            f"</div></div>",
            unsafe_allow_html=True,
        )

        def _chart_layout(fig, height=260):
            fig.update_layout(
                height=height,
                margin=dict(l=8, r=8, t=8, b=8),
                paper_bgcolor="white",
                plot_bgcolor="white",
                font=dict(family="Inter, sans-serif", size=12, color="#374151"),
                xaxis=dict(showgrid=True, gridcolor="#f0f0f0", zeroline=False,
                           tickfont=dict(size=11)),
                yaxis=dict(showgrid=False, tickfont=dict(size=11)),
                showlegend=False,
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
            _card("Leads by Country")
            cc = df_all.groupby("country").size().reset_index(name="count")
            cc = cc.sort_values("count", ascending=True).tail(12)
            fig_cc = go.Figure(go.Bar(
                x=cc["count"],
                y=cc["country"],
                orientation="h",
                marker_color="#4f46e5",
                marker_line_width=0,
                hovertemplate="%{y}: %{x} leads<extra></extra>",
            ))
            _chart_layout(fig_cc, height=max(260, len(cc) * 32))
            st.plotly_chart(fig_cc, use_container_width=True)

        # ── Leads by Status ───────────────────────────────────────────────────
        with ch2:
            _card("Leads by Status")
            sc_df = df_all.groupby("status").size().reset_index(name="count")
            _sc = {
                "new": "#6366f1", "queued": "#f59e0b", "sent": "#10b981",
                "failed": "#ef4444", "no_email": "#94a3b8",
                "bounced": "#f87171", "replied": "#0ea5e9", "skipped": "#9ca3af",
            }
            sc_df = sc_df.sort_values("count", ascending=True)
            fig_sc = go.Figure(go.Bar(
                x=sc_df["count"],
                y=sc_df["status"],
                orientation="h",
                marker_color=[_sc.get(s, "#6366f1") for s in sc_df["status"]],
                marker_line_width=0,
                hovertemplate="%{y}: %{x} leads<extra></extra>",
            ))
            _chart_layout(fig_sc, height=max(260, len(sc_df) * 40))
            st.plotly_chart(fig_sc, use_container_width=True)

        # ── Daily Lead Volume ─────────────────────────────────────────────────
        _card("Daily Lead Volume")
        df_all["date"] = pd.to_datetime(df_all["created_at"]).dt.date
        daily = df_all.groupby("date").size().reset_index(name="count")
        daily["date"] = pd.to_datetime(daily["date"])

        if len(daily) == 1:
            # Single day — show a bar so something is visible
            fig_daily = go.Figure(go.Bar(
                x=daily["date"], y=daily["count"],
                marker_color="#4f46e5", marker_line_width=0,
                hovertemplate="%{x|%b %d}: %{y} leads<extra></extra>",
            ))
        else:
            fig_daily = go.Figure()
            fig_daily.add_trace(go.Scatter(
                x=daily["date"], y=daily["count"],
                mode="lines+markers",
                line=dict(color="#4f46e5", width=2),
                fill="tozeroy",
                fillcolor="rgba(79,70,229,0.10)",
                marker=dict(size=5, color="#4f46e5"),
                hovertemplate="%{x|%b %d}: %{y} leads<extra></extra>",
            ))
        _chart_layout(fig_daily, height=220)
        st.plotly_chart(fig_daily, use_container_width=True)

        # ── Top Keywords ──────────────────────────────────────────────────────
        _card("Top Keywords")
        kc = df_all.groupby("keyword").size().reset_index(name="count")
        kc = kc.sort_values("count", ascending=True).tail(15)
        fig_kc = go.Figure(go.Bar(
            x=kc["count"],
            y=kc["keyword"],
            orientation="h",
            marker_color="#10b981",
            marker_line_width=0,
            hovertemplate="%{y}: %{x} leads<extra></extra>",
        ))
        _chart_layout(fig_kc, height=max(220, len(kc) * 32))
        st.plotly_chart(fig_kc, use_container_width=True)

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
