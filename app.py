"""
app.py – SEO Outreach Engine
Streamlit web app: find SMB leads → extract emails → send cold outreach.
"""
import base64
import io
import json
import time

import pandas as pd
import streamlit as st
from streamlit_oauth import OAuth2Component

from database import (
    delete_leads, get_leads, get_leads_with_email,
    get_stats, init_db, insert_lead, is_duplicate_lead, update_status,
)
from email_finder import find_email_on_website
from email_sender import send_batch
from scraper import COUNTRY_SCRAPERS, find_businesses
from templates import EMAIL_TEMPLATE_HTML, EMAIL_TEMPLATE_TEXT, SUBJECT_LINES

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

    # ── Minimalist light-theme CSS ────────────────────────────────────────────
    st.markdown("""
    <style>
        #MainMenu, footer, header,
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] { display: none !important; }

        html, body, .stApp { background: #f8f9fb !important; }

        .stApp > .main,
        .block-container,
        .block-container > div,
        .block-container > div > div {
            padding: 0 !important;
            margin: 0 !important;
            max-width: 100% !important;
            width: 100% !important;
            background: #f8f9fb !important;
        }
    </style>
    """, unsafe_allow_html=True)

    # ── Single centred column ─────────────────────────────────────────────────
    _, centre, _ = st.columns([1, 1.4, 1])

    with centre:
        # Top breathing room
        st.markdown("<div style='height:10vh'></div>", unsafe_allow_html=True)

        # ── Logo mark ──────────────────────────────────────────────────────
        st.markdown("""
        <div style="text-align:center; margin-bottom:32px;">
          <div style="display:inline-flex; align-items:center; justify-content:center;
                      width:64px; height:64px; background:#f0f4ff;
                      border-radius:18px; font-size:30px;
                      box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            🚀
          </div>
        </div>
        """, unsafe_allow_html=True)

        # ── Title + tagline ────────────────────────────────────────────────
        st.markdown("""
        <div style="text-align:center; margin-bottom:40px;">
          <div style="font-size:26px; font-weight:800; color:#0f172a;
                      letter-spacing:-0.6px; margin-bottom:8px;
                      font-family:'Inter','Segoe UI',sans-serif;">
            SEO Outreach Engine
          </div>
          <div style="font-size:15px; color:#94a3b8; font-weight:400; line-height:1.6;">
            Find leads · Extract emails · Close clients
          </div>
        </div>
        """, unsafe_allow_html=True)

        # ── Card container top ─────────────────────────────────────────────
        st.markdown("""
        <div style="background:white; border-radius:20px;
                    border:1px solid #e8ecf0;
                    box-shadow:0 4px 24px rgba(0,0,0,0.06);
                    padding:36px 40px 28px;">
          <div style="font-size:17px; font-weight:700; color:#0f172a;
                      margin-bottom:6px; font-family:'Inter','Segoe UI',sans-serif;">
            Sign in to your workspace
          </div>
          <div style="font-size:13px; color:#94a3b8; margin-bottom:28px; line-height:1.6;">
            Access is restricted to authorised accounts only.
          </div>
          <div style="height:1px; background:#f1f5f9; margin-bottom:24px;"></div>
        </div>
        """, unsafe_allow_html=True)

        # ── OAuth button (Streamlit widget — must be outside HTML div) ────
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

        # ── Card bottom + three trust badges ──────────────────────────────
        st.markdown("""
        <div style="background:white; border-radius:0 0 20px 20px;
                    border:1px solid #e8ecf0; border-top:none;
                    box-shadow:0 4px 24px rgba(0,0,0,0.06);
                    padding:20px 40px 28px;">
          <div style="display:flex; justify-content:center; gap:20px;
                      flex-wrap:wrap; margin-top:4px;">
            <span style="font-size:12px; color:#cbd5e1; display:flex;
                         align-items:center; gap:5px;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                   stroke="#cbd5e1" stroke-width="2">
                <rect x="3" y="11" width="18" height="11" rx="2"/>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
              Secured with OAuth 2.0
            </span>
            <span style="font-size:12px; color:#cbd5e1;">·</span>
            <span style="font-size:12px; color:#cbd5e1; display:flex;
                         align-items:center; gap:5px;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                   stroke="#cbd5e1" stroke-width="2">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                <circle cx="12" cy="7" r="4"/>
              </svg>
              Private access only
            </span>
            <span style="font-size:12px; color:#cbd5e1;">·</span>
            <span style="font-size:12px; color:#cbd5e1; display:flex;
                         align-items:center; gap:5px;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                   stroke="#cbd5e1" stroke-width="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              </svg>
              Google verified
            </span>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # ── Footer ─────────────────────────────────────────────────────────
        st.markdown("""
        <div style="text-align:center; margin-top:28px; font-size:12px; color:#cbd5e1;">
          SEO Outreach Engine &nbsp;·&nbsp; Built for B2B cold email
        </div>
        """, unsafe_allow_html=True)

        # Bottom breathing room
        st.markdown("<div style='height:10vh'></div>", unsafe_allow_html=True)

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
/* ── Base ── */
html, body, [class*="css"] {
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
}
.stApp { background: #f4f6f9; }
.block-container { padding-top: 28px !important; padding-bottom: 40px !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] { background: #0f172a !important; }
[data-testid="stSidebar"] > div { background: #0f172a !important; }
[data-testid="stSidebar"] * { color: #cbd5e1 !important; }
[data-testid="stSidebar"] .stTextInput input {
    background: #1e293b !important;
    border: 1px solid #334155 !important;
    color: #f1f5f9 !important;
    border-radius: 8px !important;
    font-size: 13px !important;
}
[data-testid="stSidebar"] .stTextInput label {
    color: #64748b !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.5px !important;
}
[data-testid="stSidebar"] .stButton button {
    background: #1e293b !important;
    border: 1px solid #334155 !important;
    color: #94a3b8 !important;
    border-radius: 8px !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    width: 100% !important;
    transition: all .15s !important;
}
[data-testid="stSidebar"] .stButton button:hover {
    background: #334155 !important;
    color: #f1f5f9 !important;
}
[data-testid="stSidebar"] hr { border-color: #1e293b !important; margin: 12px 0 !important; }
[data-testid="stSidebar"] .stExpander {
    background: #1e293b !important;
    border: 1px solid #334155 !important;
    border-radius: 10px !important;
}
[data-testid="stSidebar"] .stSlider [data-baseweb="slider"] [role="slider"] {
    background: #3b82f6 !important;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 0;
    background: transparent;
    border-bottom: 2px solid #e2e8f0;
    padding: 0;
    border-radius: 0;
    margin-bottom: 24px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 0 !important;
    padding: 10px 22px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    color: #94a3b8 !important;
    background: transparent !important;
    border-bottom: 2px solid transparent !important;
    margin-bottom: -2px !important;
}
.stTabs [aria-selected="true"] {
    color: #2563eb !important;
    border-bottom: 2px solid #2563eb !important;
    background: transparent !important;
}

/* ── Buttons ── */
.stButton button[kind="primary"] {
    background: #2563eb !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    color: white !important;
    padding: 10px 20px !important;
    transition: background .15s !important;
}
.stButton button[kind="primary"]:hover {
    background: #1d4ed8 !important;
}
.stButton button[kind="secondary"] {
    background: white !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 13px !important;
    color: #374151 !important;
}

/* ── Metric cards ── */
[data-testid="metric-container"] {
    background: white !important;
    border: 1px solid #e9ecef !important;
    border-radius: 12px !important;
    padding: 20px 24px !important;
    box-shadow: none !important;
}
[data-testid="stMetricValue"] {
    font-size: 28px !important;
    font-weight: 700 !important;
    color: #111827 !important;
}
[data-testid="stMetricLabel"] {
    font-size: 12px !important;
    color: #9ca3af !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.6px !important;
}

/* ── Data table ── */
[data-testid="stDataFrame"] {
    border: 1px solid #e9ecef !important;
    border-radius: 10px !important;
    overflow: hidden !important;
    box-shadow: none !important;
}

/* ── Inputs / selects ── */
.stTextInput input, .stSelectbox select, div[data-baseweb="select"] {
    border-radius: 8px !important;
    border-color: #e2e8f0 !important;
    font-size: 14px !important;
}

/* ── Alerts ── */
.stAlert { border-radius: 8px !important; border: none !important; }

/* ── Toggle ── */
.stToggle [data-baseweb="checkbox"] { border-radius: 20px !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section(title: str, subtitle: str = ""):
    sub = f"<p style='font-size:13px;color:#9ca3af;margin:2px 0 0;'>{subtitle}</p>" if subtitle else ""
    st.markdown(
        f"<div style='margin-bottom:20px;'>"
        f"<h3 style='font-size:18px;font-weight:700;color:#111827;margin:0;'>{title}</h3>"
        f"{sub}</div>",
        unsafe_allow_html=True,
    )


def _card(content_html: str):
    st.markdown(
        f'<div style="background:white;border-radius:12px;padding:24px;'
        f'border:1px solid #e9ecef;margin-bottom:16px;">{content_html}</div>',
        unsafe_allow_html=True,
    )


def _stat_card(label: str, value, color: str, sub: str = ""):
    sub_html = (f'<div style="font-size:12px;color:#9ca3af;margin-top:4px;">{sub}</div>'
                if sub else "")
    dot = (f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
           f'background:{color};margin-right:6px;vertical-align:middle;"></span>')
    st.markdown(
        f'<div style="background:white;border-radius:12px;padding:20px 24px;'
        f'border:1px solid #e9ecef;">'
        f'<div style="font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;'
        f'letter-spacing:0.7px;margin-bottom:10px;">{dot}{label}</div>'
        f'<div style="font-size:34px;font-weight:800;color:#111827;line-height:1;">{value}</div>'
        f'{sub_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="background:{bg};color:{color};border-radius:6px;'
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
        "<div style='padding:16px 0 8px;'>"
        "<div style='font-size:15px;font-weight:700;color:#f1f5f9;letter-spacing:-0.3px;'>"
        "🚀 SEO Outreach</div>"
        "<div style='font-size:11px;color:#475569;margin-top:2px;'>B2B cold email engine</div>"
        "</div>",
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

    st.divider()

    # ── Rate limiting ─────────────────────────────────────────────────────
    st.markdown("<div style='font-size:11px;font-weight:700;color:#475569;"
                "text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px;'>"
                "Rate Limiting</div>", unsafe_allow_html=True)
    delay_sec = st.slider("Delay between emails (s)", 30, 180, 120)
    st.caption("120 s recommended. Higher = safer deliverability.")


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
stats = get_stats()
total  = stats.get("total",    0)
sent   = stats.get("sent",     0)
ready  = stats.get("new",      0)
failed = stats.get("failed",   0)

h1, h2, h3, h4 = st.columns([3, 1, 1, 1])
with h1:
    st.markdown(
        "<h1 style='font-size:22px;font-weight:700;color:#111827;margin:0;padding:8px 0;'>"
        "SEO Outreach Engine</h1>"
        "<p style='font-size:13px;color:#9ca3af;margin:0;'>"
        "Find leads · Extract emails · Close clients</p>",
        unsafe_allow_html=True,
    )
with h2:
    st.metric("Total leads", total)
with h3:
    st.metric("Emails sent", sent)
with h4:
    st.metric("Ready to send", ready)

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tab_find, tab_db, tab_send, tab_analytics = st.tabs([
    "🔍 Find Leads",
    "📋 Leads Database",
    "✉️ Send Emails",
    "📊 Analytics",
])

# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 – Find Leads
# ═════════════════════════════════════════════════════════════════════════════
with tab_find:
    _section("Find Leads",
             "Search Google Maps for businesses, then visit each website to extract contact emails.")

    with st.container():
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            keyword = st.text_input("Business type", placeholder="plumber · HVAC · cake shop · roofer")
        with c2:
            location = st.text_input("City / location", placeholder="Melbourne · Dubai · London")
        with c3:
            country = st.selectbox("Country", list(COUNTRY_SCRAPERS.keys()))

        c4, c5 = st.columns(2)
        with c4:
            max_pages = st.slider("Results to fetch", 1, 10, 3, help="~10 businesses per page")
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

            log(f"🔎 Searching: '{keyword}' in {location}, {country}")

            _serper_key  = st.session_state.get("s_serper", "") or st.secrets.get("serper_key", "")
            _fsq_key     = st.session_state.get("s_fsq",    "") or st.secrets.get("foursquare_key", "")
            _gplaces_key = st.session_state.get("s_gplaces","") or st.secrets.get("google_places_key", "")
            _yelp_key    = st.session_state.get("s_yelp",   "") or st.secrets.get("yelp_api_key", "")

            businesses = find_businesses(
                keyword=keyword.strip(), location=location.strip(), country=country,
                max_pages=max_pages + extra_pages,
                skip_top=skip_top,
                serper_key=_serper_key, foursquare_key=_fsq_key,
                yelp_api_key=_yelp_key, google_places_key=_gplaces_key, log_cb=log,
            )

            if not businesses:
                st.warning("No businesses found. Try a different keyword, location, or add a Serper API key.")
                st.stop()

            # ── Dedup within this batch ───────────────────────────────────────
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

            new_count   = 0
            no_email_ct = 0
            skipped_ct  = 0

            for idx, biz in enumerate(businesses):
                pct = int((idx + 1) / len(businesses) * 100)
                prog_bar.progress(pct, text=f"Processing {idx+1}/{len(businesses)}: {biz.get('business_name','')}")

                # Skip if already in database
                if is_duplicate_lead(website=biz.get("website",""), phone=biz.get("phone","")):
                    log(f"⏭️  Already in DB — skipping: {biz.get('business_name','')}")
                    skipped_ct += 1
                    continue

                # Email discovery
                if auto_email and biz.get("website"):
                    log(f"🔗  {biz['business_name']} → {biz['website']}")
                    email, email_source = find_email_on_website(biz["website"])
                    if email:
                        biz["email"]        = email
                        biz["email_source"] = email_source
                        icon = "🤔" if email_source == "guessed" else "📧"
                        note = "  (pattern guess)" if email_source == "guessed" else "  ✅"
                        log(f"    {icon}  {email}{note}")
                    else:
                        biz["email"] = None; biz["email_source"] = None
                        biz["status"] = "no_email"
                        log("    ⚠️  No email found")
                elif not auto_email:
                    biz["email"] = None; biz["email_source"] = None; biz["status"] = "no_email"

                if not biz.get("email"):
                    no_email_ct += 1

                if insert_lead(biz):
                    new_count += 1

            prog_bar.progress(100, text="Done!")

            st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
            r1, r2, r3, r4 = st.columns(4)
            with r1:
                _stat_card("Businesses found",  len(businesses) + batch_dups, "#2563eb")
            with r2:
                _stat_card("New leads saved",   new_count,  "#16a34a",
                           "ready to email" if new_count else "")
            with r3:
                _stat_card("Already in DB",     skipped_ct, "#7c3aed",
                           "API quota saved" if skipped_ct else "")
            with r4:
                _stat_card("No email found",    no_email_ct, "#ef4444")

            if new_count > 0:
                st.success(f"{new_count} new leads saved — go to Send Emails to reach out.")

            df_prev = pd.DataFrame([b for b in businesses if not is_duplicate_lead(
                website=b.get("website",""), phone=b.get("phone","")
            )])
            if not df_prev.empty:
                st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;"
                            "margin:16px 0 8px;'>Results preview</p>", unsafe_allow_html=True)
                if "email_source" in df_prev.columns:
                    df_prev["Email"] = df_prev["email_source"].map(
                        lambda s: "Found" if s == "found" else ("Guessed" if s == "guessed" else "—")
                    )
                show_cols = [c for c in
                    ["business_name","email","Email","phone","website","address","city","source"]
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
        st.write(""); st.write("")
        if st.button("Refresh", use_container_width=True): st.rerun()

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
            st.markdown(EMAIL_TEMPLATE_HTML.format(
                business_name="ABC Plumbing", sender_name=sender_name,
                sender_email=sender_email), unsafe_allow_html=True)

        df_ready = get_leads_with_email(status="new")

        if df_ready.empty:
            st.markdown(
                "<div style='background:white;border-radius:12px;padding:48px 24px;"
                "text-align:center;border:1px solid #e9ecef;'>"
                "<p style='font-size:32px;margin:0 0 12px;'>📭</p>"
                "<p style='font-size:16px;font-weight:600;color:#111827;margin:0 0 6px;'>"
                "No leads ready to send</p>"
                "<p style='font-size:13px;color:#9ca3af;margin:0;'>"
                "All leads have been emailed, or no emails were found yet.<br>"
                "Go to Find Leads with auto-extract enabled to get new leads.</p>"
                "</div>", unsafe_allow_html=True)
        else:
            guessed_count = int((df_ready.get("email_source","") == "guessed").sum()) \
                if "email_source" in df_ready.columns else 0

            # Summary row
            s1, s2, s3 = st.columns(3)
            with s1: _stat_card("Ready to send",  len(df_ready), "#2563eb")
            with s2: _stat_card("Guessed emails",  guessed_count, "#f59e0b",
                                 "lower deliverability" if guessed_count else "")
            with s3:
                est_preview = max(1, (min(20, len(df_ready)) * delay_sec) // 60)
                _stat_card("Est. time (20 emails)", f"~{est_preview} min", "#6b7280")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

            if "email_source" in df_ready.columns:
                df_ready["Email source"] = df_ready["email_source"].map(
                    lambda s: "Found" if s=="found" else ("Guessed" if s=="guessed" else "—"))
            show_ready = [c for c in ["id","business_name","email","Email source",
                                      "city","country","keyword"] if c in df_ready.columns]
            st.dataframe(df_ready[show_ready], use_container_width=True, height=240)

            sc1, sc2 = st.columns([3, 1])
            with sc1:
                max_send = st.slider("Number of emails to send",
                    1, min(200, len(df_ready)), min(20, len(df_ready)))
            with sc2:
                est_mins = max(1, (max_send * delay_sec) // 60)
                st.metric("Est. time", f"~{est_mins} min")

            st.info(f"Sending **{max_send} emails** with **{delay_sec}s** between each. "
                    f"Each lead is marked Sent immediately after delivery.")

            if st.button(f"Send {max_send} emails", type="primary", use_container_width=True):
                batch = df_ready.head(max_send).copy()
                log_area = st.empty()
                send_logs: list = []
                def s_log(msg: str):
                    send_logs.append(msg)
                    log_area.markdown("```\n" + "\n".join(send_logs[-30:]) + "\n```")

                result = send_batch(leads_df=batch, sender_email=sender_email,
                    app_password=app_password, sender_name=sender_name,
                    delay_seconds=delay_sec, log_cb=s_log, update_status_cb=update_status)

                rc1, rc2 = st.columns(2)
                with rc1: _stat_card("Sent",   result["sent"],   "#16a34a")
                with rc2: _stat_card("Failed", result["failed"], "#ef4444")
                if result["errors"]:
                    with st.expander("Error details"):
                        for err in result["errors"]: st.text(err)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 – Analytics
# ═════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    _section("Analytics")

    a1, a2, a3, a4, a5 = st.columns(5)
    with a1: _stat_card("Total leads",  stats.get("total",    0), "#2563eb")
    with a2: _stat_card("New (unsent)", stats.get("new",      0), "#7c3aed", "go to Send Emails")
    with a3: _stat_card("Emails sent",  stats.get("sent",     0), "#16a34a")
    with a4: _stat_card("Failed",       stats.get("failed",   0), "#ef4444")
    with a5: _stat_card("No email",     stats.get("no_email", 0), "#f59e0b")

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

        ch1, ch2 = st.columns(2)
        with ch1:
            st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;margin-bottom:6px;'>"
                        "Leads by country</p>", unsafe_allow_html=True)
            cc = df_all.groupby("country").size().reset_index(name="count")
            st.bar_chart(cc.set_index("country"), color="#2563eb", height=220)

        with ch2:
            st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;margin-bottom:6px;'>"
                        "Leads by status</p>", unsafe_allow_html=True)
            sc_df = df_all.groupby("status").size().reset_index(name="count")
            st.bar_chart(sc_df.set_index("status"), color="#7c3aed", height=220)

        st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;margin:16px 0 6px;'>"
                    "Daily lead volume</p>", unsafe_allow_html=True)
        df_all["date"] = pd.to_datetime(df_all["created_at"]).dt.date
        daily = df_all.groupby("date").size().reset_index(name="leads")
        st.line_chart(daily.set_index("date"), color="#2563eb", height=200)

        st.markdown("<p style='font-size:13px;font-weight:600;color:#374151;margin:16px 0 6px;'>"
                    "Leads by keyword</p>", unsafe_allow_html=True)
        kc = df_all.groupby("keyword").size().reset_index(name="count")
        st.bar_chart(kc.set_index("keyword"), color="#16a34a", height=200)
