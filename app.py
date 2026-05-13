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

    # ── Full-page dark styles ────────────────────────────────────────────────
    st.markdown("""
    <style>
        #MainMenu, footer, header { visibility: hidden; }
        .stApp {
            background: #080d1a !important;
            min-height: 100vh;
        }
        .block-container {
            padding-top: 0 !important;
            padding-bottom: 0 !important;
            max-width: 100% !important;
        }
        /* Style the Google OAuth button to match our design */
        [data-testid="stBaseButton-secondary"],
        iframe { border-radius: 12px !important; }
        /* Remove default column gaps on login page */
        [data-testid="stHorizontalBlock"] { gap: 0 !important; }
    </style>
    """, unsafe_allow_html=True)

    # ── Split-screen layout ──────────────────────────────────────────────────
    left_col, right_col = st.columns([1.1, 0.9])

    # ── LEFT: Dark branding panel ────────────────────────────────────────────
    with left_col:
        st.markdown(
            """
            <div style="
                background: linear-gradient(160deg, #0f172a 0%, #0c1a3a 60%, #0a1628 100%);
                min-height: 100vh;
                padding: 72px 64px;
                display: flex;
                flex-direction: column;
                justify-content: center;
                position: relative;
                overflow: hidden;
            ">
              <!-- Decorative blobs -->
              <div style="position:absolute; top:-80px; left:-80px; width:300px; height:300px;
                          background:radial-gradient(circle, rgba(37,99,235,0.2) 0%, transparent 70%);
                          border-radius:50%;"></div>
              <div style="position:absolute; bottom:-100px; right:-60px; width:350px; height:350px;
                          background:radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 70%);
                          border-radius:50%;"></div>

              <!-- Logo + wordmark -->
              <div style="display:flex; align-items:center; gap:14px; margin-bottom:56px;">
                <div style="font-size:40px; line-height:1;">🚀</div>
                <div>
                  <div style="font-size:20px; font-weight:800; color:white; letter-spacing:-0.3px;">
                    SEO Outreach Engine
                  </div>
                  <div style="font-size:12px; color:#4b6cb7; font-weight:500; margin-top:2px;">
                    B2B Cold Email · Powered by Google Maps
                  </div>
                </div>
              </div>

              <!-- Headline -->
              <div style="font-size:42px; font-weight:900; color:white; line-height:1.15;
                          letter-spacing:-1.5px; margin-bottom:20px;">
                Turn searches<br>into
                <span style="background:linear-gradient(90deg,#60a5fa,#818cf8);
                             -webkit-background-clip:text; -webkit-text-fill-color:transparent;">
                  SEO clients
                </span>
              </div>
              <div style="font-size:16px; color:#94a3b8; line-height:1.7; margin-bottom:48px;
                          max-width:400px;">
                Automatically find small businesses, extract their contact emails,
                and send personalised SEO pitch emails — all in one place.
              </div>

              <!-- Feature list -->
              <div style="display:flex; flex-direction:column; gap:18px;">
                <div style="display:flex; align-items:center; gap:14px;">
                  <div style="width:36px; height:36px; background:rgba(37,99,235,0.2);
                              border-radius:10px; display:flex; align-items:center;
                              justify-content:center; font-size:18px; flex-shrink:0;">🗺️</div>
                  <div>
                    <div style="font-size:14px; font-weight:700; color:#e2e8f0;">
                      Google Maps scraping (via Serper)
                    </div>
                    <div style="font-size:12px; color:#64748b;">Free 2,500 searches/month · no credit card</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:14px;">
                  <div style="width:36px; height:36px; background:rgba(16,185,129,0.2);
                              border-radius:10px; display:flex; align-items:center;
                              justify-content:center; font-size:18px; flex-shrink:0;">📧</div>
                  <div>
                    <div style="font-size:14px; font-weight:700; color:#e2e8f0;">
                      Automatic email extraction
                    </div>
                    <div style="font-size:12px; color:#64748b;">7 methods incl. Cloudflare decoder</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:14px;">
                  <div style="width:36px; height:36px; background:rgba(139,92,246,0.2);
                              border-radius:10px; display:flex; align-items:center;
                              justify-content:center; font-size:18px; flex-shrink:0;">🚀</div>
                  <div>
                    <div style="font-size:14px; font-weight:700; color:#e2e8f0;">
                      Gmail cold outreach — $600/month pitch
                    </div>
                    <div style="font-size:12px; color:#64748b;">Rate-limited sending · CAN-SPAM compliant</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:14px;">
                  <div style="width:36px; height:36px; background:rgba(251,191,36,0.2);
                              border-radius:10px; display:flex; align-items:center;
                              justify-content:center; font-size:18px; flex-shrink:0;">📊</div>
                  <div>
                    <div style="font-size:14px; font-weight:700; color:#e2e8f0;">
                      Full leads database & analytics
                    </div>
                    <div style="font-size:12px; color:#64748b;">Track status, export CSV, never email twice</div>
                  </div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── RIGHT: Login panel ───────────────────────────────────────────────────
    with right_col:
        st.markdown(
            """
            <div style="
                background: #0d1424;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                padding: 60px 48px;
            ">
              <div style="width:100%; max-width:360px; text-align:center;">
                <div style="font-size:13px; font-weight:700; color:#3b82f6;
                            text-transform:uppercase; letter-spacing:2px; margin-bottom:12px;">
                  Private Workspace
                </div>
                <div style="font-size:28px; font-weight:800; color:white;
                            letter-spacing:-0.5px; margin-bottom:10px;">
                  Welcome back
                </div>
                <div style="font-size:14px; color:#64748b; margin-bottom:36px; line-height:1.6;">
                  Sign in with your authorised Google account<br>to access your outreach dashboard.
                </div>
                <div style="height:1px; background:rgba(255,255,255,0.07); margin-bottom:28px;"></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # OAuth button — positioned in right column
        # Use nested columns to center it within the right panel
        _, btn_center, _ = st.columns([0.5, 3, 0.5])
        with btn_center:
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

            st.markdown(
                "<div style='text-align:center; margin-top:20px; font-size:12px; color:#334155;'>"
                "🔒 Google OAuth 2.0 &nbsp;·&nbsp; Authorised accounts only"
                "</div>",
                unsafe_allow_html=True,
            )

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
# Global CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
html, body, [class*="css"] { font-family: 'Inter', 'Segoe UI', sans-serif; }
.stApp { background: #f1f5f9; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f172a 0%, #1e3a8a 100%) !important;
}
[data-testid="stSidebar"] * { color: #e2e8f0 !important; }
[data-testid="stSidebar"] .stTextInput input {
    background: rgba(255,255,255,0.08) !important;
    border: 1px solid rgba(255,255,255,0.15) !important;
    color: #f1f5f9 !important;
    border-radius: 8px !important;
}
[data-testid="stSidebar"] .stTextInput label { color: #94a3b8 !important; }
[data-testid="stSidebar"] .stButton button {
    background: rgba(255,255,255,0.1) !important;
    border: 1px solid rgba(255,255,255,0.2) !important;
    color: #f1f5f9 !important;
    border-radius: 8px !important;
    width: 100%;
}
[data-testid="stSidebar"] .stButton button:hover {
    background: rgba(255,255,255,0.18) !important;
}
[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.1) !important; }
[data-testid="stSidebar"] .stExpander {
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: #e2e8f0;
    padding: 4px;
    border-radius: 12px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 9px !important;
    padding: 8px 20px !important;
    font-weight: 600 !important;
    color: #64748b !important;
    background: transparent !important;
}
.stTabs [aria-selected="true"] {
    background: white !important;
    color: #1e3a8a !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.12) !important;
}

/* ── Primary buttons ── */
.stButton button[kind="primary"] {
    background: linear-gradient(135deg, #1d4ed8, #2563eb) !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 700 !important;
    font-size: 15px !important;
    padding: 12px 24px !important;
    box-shadow: 0 4px 14px rgba(37,99,235,0.35) !important;
    color: white !important;
}
.stButton button[kind="primary"]:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(37,99,235,0.45) !important;
}

/* ── Streamlit native metric override ── */
[data-testid="metric-container"] {
    background: white !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 14px !important;
    padding: 18px 22px !important;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06) !important;
}
[data-testid="stMetricValue"] {
    font-size: 30px !important;
    font-weight: 800 !important;
    color: #0f172a !important;
}
[data-testid="stMetricLabel"] {
    font-size: 12px !important;
    color: #64748b !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.5px !important;
}

/* ── Dataframe ── */
[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important;
    overflow: hidden !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.04) !important;
}

/* ── Alerts ── */
.stAlert { border-radius: 10px !important; }

/* ── Slider ── */
[data-baseweb="slider"] [role="slider"] { background: #2563eb !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _card(content_html: str, padding: str = "24px 28px"):
    st.markdown(
        f'<div style="background:white; border-radius:16px; padding:{padding}; '
        f'box-shadow:0 2px 10px rgba(0,0,0,0.06); margin-bottom:16px;">'
        f'{content_html}</div>',
        unsafe_allow_html=True,
    )


def _stat_card(icon: str, label: str, value, color: str, sub: str = ""):
    sub_html = f'<div style="font-size:12px;color:#94a3b8;margin-top:4px;">{sub}</div>' if sub else ""
    st.markdown(
        f"""
        <div style="background:white; border-radius:14px; padding:20px 22px;
                    border-left:5px solid {color};
                    box-shadow:0 2px 10px rgba(0,0,0,0.06);">
          <div style="font-size:22px; margin-bottom:6px;">{icon}</div>
          <div style="font-size:32px; font-weight:800; color:#0f172a; line-height:1;">
            {value}
          </div>
          <div style="font-size:12px; font-weight:700; color:#64748b;
                      text-transform:uppercase; letter-spacing:0.5px; margin-top:6px;">
            {label}
          </div>
          {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="background:{bg}; color:{color}; border-radius:20px; '
        f'padding:3px 10px; font-size:11px; font-weight:700; '
        f'display:inline-block;">{text}</span>'
    )


STATUS_BADGE = {
    "new":      _badge("● NEW",      "#1d4ed8", "#eff6ff"),
    "sent":     _badge("✓ SENT",     "#15803d", "#f0fdf4"),
    "failed":   _badge("✗ FAILED",   "#dc2626", "#fef2f2"),
    "no_email": _badge("— NO EMAIL", "#92400e", "#fffbeb"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        "<div style='text-align:center; padding:8px 0 4px;'>"
        "<span style='font-size:44px;'>🚀</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div style='text-align:center; font-size:18px; font-weight:800; "
        "color:#f1f5f9; margin-bottom:2px;'>SEO Outreach Engine</div>"
        "<div style='text-align:center; font-size:12px; color:#94a3b8; "
        "margin-bottom:16px;'>Find → Extract → Send</div>",
        unsafe_allow_html=True,
    )

    user_name  = st.session_state.get("_user_name", "")
    user_email = st.session_state.get("_user_email", "")
    st.markdown(
        f"<div style='background:rgba(255,255,255,0.08); border-radius:10px; "
        f"padding:10px 14px; margin-bottom:8px;'>"
        f"<div style='font-size:13px; font-weight:700; color:#e2e8f0;'>👤 {user_name}</div>"
        f"<div style='font-size:11px; color:#94a3b8; margin-top:2px;'>{user_email}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.button("🚪 Sign out", use_container_width=True):
        for key in ["_authenticated", "_user_email", "_user_name", "google_login_btn"]:
            st.session_state.pop(key, None)
        st.rerun()

    st.divider()
    st.markdown(
        "<div style='font-size:13px; font-weight:700; color:#94a3b8; "
        "text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;'>"
        "📧 Gmail SMTP</div>",
        unsafe_allow_html=True,
    )

    _def_email = st.secrets.get("smtp_email",    "")
    _def_name  = st.secrets.get("smtp_name",     "")
    _def_pass  = st.secrets.get("smtp_password", "")
    _def_yelp  = st.secrets.get("yelp_api_key",  "")

    sender_email = st.text_input(
        "Sending Gmail address", value=_def_email,
        placeholder="you@gmail.com", key="s_email",
    )
    app_password = st.text_input(
        "Gmail App Password", value=_def_pass,
        type="password", key="s_pass",
        help="myaccount.google.com → Security → App Passwords",
    )
    sender_name = st.text_input(
        "Your name (From:)", value=_def_name,
        placeholder="John Smith", key="s_name",
    )

    _def_serper   = st.secrets.get("serper_key",        "")
    _def_fsq      = st.secrets.get("foursquare_key",    "")
    _def_gplaces  = st.secrets.get("google_places_key", "")

    with st.expander("🗺️ Serper API Key  (FREE – recommended)", expanded=bool(_def_serper)):
        serper_key = st.text_input(
            "Serper.dev API Key", value=_def_serper, type="password", key="s_serper",
            help="Free 2,500 Google Maps searches/month. serper.dev",
        )
        if _def_serper:
            st.success("✅ Serper active!")
        else:
            st.info("Get free key → serper.dev")

    with st.expander("📍 Foursquare API Key  (FREE)", expanded=False):
        foursquare_key = st.text_input(
            "Foursquare API Key", value=_def_fsq, type="password", key="s_fsq",
        )

    with st.expander("🗺️ Google Places API Key  (optional)", expanded=False):
        google_places_key = st.text_input(
            "Google Places API Key", value=_def_gplaces, type="password", key="s_gplaces",
        )

    with st.expander("🟡 Yelp API Key  (optional)", expanded=False):
        yelp_key = st.text_input(
            "Yelp Fusion API Key", value=_def_yelp, type="password", key="s_yelp",
        )

    st.divider()
    st.markdown(
        "<div style='font-size:13px; font-weight:700; color:#94a3b8; "
        "text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;'>"
        "⏱️ Rate Limiting</div>",
        unsafe_allow_html=True,
    )
    delay_sec = st.slider(
        "Seconds between emails", min_value=30, max_value=180, value=120,
        help="120 s is safe. More delay = better deliverability.",
    )

    st.divider()
    st.markdown(
        """
        <div style='background:rgba(251,191,36,0.12); border:1px solid rgba(251,191,36,0.3);
                    border-radius:10px; padding:12px 14px; font-size:12px; color:#fbbf24;'>
        ⚠️ <b>Gmail Safety Tips</b><br><br>
        • Start with <b>20–30 emails/day</b><br>
        • Use a <b>Gmail App Password</b><br>
        • Ramp up slowly each week<br>
        • All emails include unsubscribe notice
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────
def _smtp_ready() -> bool:
    return bool(
        st.session_state.get("s_email")
        and st.session_state.get("s_pass")
        and st.session_state.get("s_name")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
stats = get_stats()

st.markdown(
    f"""
    <div style="background:linear-gradient(135deg,#1e3a8a,#2563eb);
                border-radius:16px; padding:24px 32px; margin-bottom:24px;
                display:flex; align-items:center; gap:24px;">
      <div style="flex:1;">
        <div style="font-size:26px; font-weight:800; color:white; margin-bottom:4px;">
          🚀 SEO Outreach Engine
        </div>
        <div style="font-size:14px; color:#93c5fd;">
          Find small businesses · Extract emails · Land SEO clients at $600/month
        </div>
      </div>
      <div style="display:flex; gap:16px;">
        <div style="background:rgba(255,255,255,0.12); border-radius:12px;
                    padding:14px 20px; text-align:center; min-width:80px;">
          <div style="font-size:26px; font-weight:800; color:white;">
            {stats.get("total", 0)}
          </div>
          <div style="font-size:11px; color:#93c5fd; font-weight:600;">TOTAL LEADS</div>
        </div>
        <div style="background:rgba(255,255,255,0.12); border-radius:12px;
                    padding:14px 20px; text-align:center; min-width:80px;">
          <div style="font-size:26px; font-weight:800; color:#86efac;">
            {stats.get("sent", 0)}
          </div>
          <div style="font-size:11px; color:#93c5fd; font-weight:600;">EMAILS SENT</div>
        </div>
        <div style="background:rgba(255,255,255,0.12); border-radius:12px;
                    padding:14px 20px; text-align:center; min-width:80px;">
          <div style="font-size:26px; font-weight:800; color:#fde68a;">
            {stats.get("new", 0)}
          </div>
          <div style="font-size:11px; color:#93c5fd; font-weight:600;">READY TO SEND</div>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

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
    _card(
        "<div style='font-size:18px; font-weight:700; color:#0f172a; margin-bottom:6px;'>"
        "🔍 Find & Qualify Leads</div>"
        "<div style='font-size:13px; color:#64748b;'>"
        "Enter a business type and location. The app scrapes Google Maps, visits each "
        "website, and automatically extracts contact emails — skipping businesses already "
        "in your database to save API quota."
        "</div>"
    )

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        keyword = st.text_input(
            "Business keyword",
            placeholder="e.g.  HVAC  ·  plumber  ·  cake shop  ·  roofer",
        )
    with c2:
        location = st.text_input(
            "City / location",
            placeholder="e.g.  Melbourne  ·  Dubai  ·  Burlington Vermont",
        )
    with c3:
        country = st.selectbox("Country", list(COUNTRY_SCRAPERS.keys()))

    c4, c5 = st.columns([1, 1])
    with c4:
        max_pages = st.slider(
            "Results to fetch", 1, 10, 3,
            help="Each page ~10 businesses. 3 pages = ~30 leads.",
        )
    with c5:
        auto_email = st.toggle(
            "Auto-find emails", value=True,
            help="Visits each website to extract contact email.",
        )

    if st.button("🚀 Find Leads Now", type="primary", use_container_width=True):
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
                max_pages=max_pages, serper_key=_serper_key, foursquare_key=_fsq_key,
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

            prog_bar.progress(100, text="✅ Done!")

            # Results summary cards
            st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)
            r1, r2, r3, r4 = st.columns(4)
            with r1:
                _stat_card("🏢", "Businesses Found", len(businesses) + batch_dups, "#2563eb")
            with r2:
                _stat_card("💾", "New Leads Saved", new_count, "#16a34a",
                           "→ ready to email" if new_count else "")
            with r3:
                _stat_card("⏭️", "Already in DB", skipped_ct, "#7c3aed",
                           "quota saved" if skipped_ct else "")
            with r4:
                _stat_card("❌", "No Email Found", no_email_ct, "#dc2626")

            if new_count > 0:
                st.success(
                    f"✅ **{new_count} new leads saved!** "
                    f"Go to **✉️ Send Emails** tab to send your outreach."
                )

            # Preview table
            st.markdown("### 📋 Results Preview")
            df_prev = pd.DataFrame([b for b in businesses if not is_duplicate_lead(
                website=b.get("website",""), phone=b.get("phone","")
            )])
            if not df_prev.empty:
                if "email_source" in df_prev.columns:
                    df_prev["Email Source"] = df_prev["email_source"].map(
                        lambda s: "✅ Found" if s == "found" else ("🤔 Guessed" if s == "guessed" else "—")
                    )
                show_cols = [c for c in
                    ["business_name","email","Email Source","phone","website","address","city","source"]
                    if c in df_prev.columns]
                st.dataframe(df_prev[show_cols], use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 – Leads Database
# ═════════════════════════════════════════════════════════════════════════════
with tab_db:
    st.markdown("### 📋 Leads Database")

    # Status badges legend
    st.markdown(
        "<div style='display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px;'>"
        + STATUS_BADGE["new"]    + "&nbsp; New lead, not yet emailed"
        + "&nbsp;&nbsp;&nbsp;"
        + STATUS_BADGE["sent"]   + "&nbsp; Email sent — will never be emailed again"
        + "&nbsp;&nbsp;&nbsp;"
        + STATUS_BADGE["failed"] + "&nbsp; Send failed (check SMTP settings)"
        + "&nbsp;&nbsp;&nbsp;"
        + STATUS_BADGE["no_email"] + "&nbsp; No email address found"
        + "</div>",
        unsafe_allow_html=True,
    )

    f1, f2, f3 = st.columns([2, 1, 1])
    with f1:
        status_filter = st.selectbox(
            "Filter by status",
            ["all", "new", "sent", "failed", "no_email"],
            format_func=lambda x: {
                "all": "📋 All leads",
                "new": "🔵 New (not yet emailed)",
                "sent": "🟢 Sent",
                "failed": "🔴 Failed",
                "no_email": "🟡 No email found",
            }.get(x, x)
        )
    with f2:
        st.write("")
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    with f3:
        st.write("")

    df_db = get_leads(None if status_filter == "all" else status_filter)

    if df_db.empty:
        st.markdown(
            "<div style='background:white; border-radius:14px; padding:48px; "
            "text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.06);'>"
            "<div style='font-size:48px; margin-bottom:12px;'>📭</div>"
            "<div style='font-size:18px; font-weight:700; color:#0f172a; margin-bottom:8px;'>No leads yet</div>"
            "<div style='font-size:14px; color:#64748b;'>Head to <b>🔍 Find Leads</b> to get started.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        total_shown = len(df_db)
        has_email = df_db["email"].notna() & (df_db["email"] != "")
        st.markdown(
            f"<div style='background:white; border-radius:12px; padding:14px 20px; "
            f"margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,0.05); "
            f"display:flex; gap:24px; flex-wrap:wrap;'>"
            f"<span style='color:#0f172a; font-weight:700;'>📊 {total_shown} leads</span>"
            f"<span style='color:#16a34a;'>📧 {has_email.sum()} with email</span>"
            f"<span style='color:#64748b;'>❌ {(~has_email).sum()} without email</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        if "email_source" in df_db.columns:
            df_db["Email ✔"] = df_db["email_source"].map(
                lambda s: "✅ Found" if s == "found" else ("🤔 Guessed" if s == "guessed" else "—")
            )

        show = [c for c in
            ["id","business_name","email","Email ✔","phone","city","country",
             "keyword","source","status","email_sent_at","created_at"]
            if c in df_db.columns]
        st.dataframe(df_db[show], use_container_width=True, height=440)

        dl1, dl2 = st.columns([1, 3])
        with dl1:
            csv_bytes = df_db.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Export CSV", data=csv_bytes,
                file_name=f"leads_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv", use_container_width=True,
            )

        with st.expander("🗑️ Delete leads by ID"):
            ids_input = st.text_input("Lead IDs to delete (comma-separated)", placeholder="1, 5, 12")
            if st.button("Delete selected", type="primary"):
                try:
                    ids = [int(x.strip()) for x in ids_input.split(",") if x.strip()]
                    if ids:
                        delete_leads(ids)
                        st.success(f"Deleted {len(ids)} lead(s).")
                        st.rerun()
                except ValueError:
                    st.error("Invalid format. Use numbers separated by commas.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 – Send Emails
# ═════════════════════════════════════════════════════════════════════════════
with tab_send:
    st.markdown("### ✉️ Send Cold Pitch Emails")

    # How it works explainer
    st.markdown(
        """
        <div style="background:white; border-radius:14px; padding:20px 24px;
                    border-left:5px solid #2563eb; margin-bottom:20px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.06);">
          <div style="font-weight:700; color:#0f172a; margin-bottom:10px; font-size:15px;">
            ℹ️ How sending works
          </div>
          <div style="font-size:13px; color:#475569; line-height:1.8;">
            <b>Only "New" leads are shown here</b> — leads you've already emailed (status = Sent)
            are <b>permanently excluded</b> and will never be emailed again.<br>
            Once an email is sent successfully, the lead is marked <b>Sent</b> with a timestamp.
            This prevents any accidental duplicate outreach.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not _smtp_ready():
        st.markdown(
            """
            <div style="background:#fef2f2; border:1px solid #fecaca; border-radius:14px;
                        padding:24px; text-align:center; margin-bottom:16px;">
              <div style="font-size:36px; margin-bottom:10px;">⚙️</div>
              <div style="font-size:16px; font-weight:700; color:#991b1b; margin-bottom:8px;">
                Gmail SMTP not configured
              </div>
              <div style="font-size:13px; color:#7f1d1d;">
                Fill in your <b>Gmail address</b>, <b>App Password</b>, and <b>your name</b>
                in the left sidebar to unlock sending.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.info(
            "📖 How to create a Gmail App Password: "
            "[Google guide](https://support.google.com/accounts/answer/185833) · "
            "Takes 2 minutes, no cost."
        )
    else:
        with st.expander("👁️ Preview email template", expanded=False):
            prev_html = EMAIL_TEMPLATE_HTML.format(
                business_name="ABC Plumbing Services",
                sender_name=sender_name,
                sender_email=sender_email,
            )
            st.markdown(prev_html, unsafe_allow_html=True)
            st.divider()
            st.text(EMAIL_TEMPLATE_TEXT.format(
                business_name="ABC Plumbing Services",
                sender_name=sender_name,
                sender_email=sender_email,
            ))

        df_ready = get_leads_with_email(status="new")

        if df_ready.empty:
            st.markdown(
                """
                <div style="background:white; border-radius:14px; padding:48px;
                            text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
                  <div style="font-size:48px; margin-bottom:12px;">📭</div>
                  <div style="font-size:18px; font-weight:700; color:#0f172a; margin-bottom:8px;">
                    No leads ready to send
                  </div>
                  <div style="font-size:14px; color:#64748b;">
                    Either all leads have been emailed already, or you haven't found any leads with emails yet.<br>
                    Go to <b>🔍 Find Leads</b> with "Auto-find emails" turned on to get new leads.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            # Ready count banner
            guessed_count = 0
            if "email_source" in df_ready.columns:
                guessed_count = (df_ready["email_source"] == "guessed").sum()

            st.markdown(
                f"""
                <div style="background:white; border-radius:14px; padding:18px 24px;
                            border-left:5px solid #16a34a; margin-bottom:16px;
                            box-shadow:0 2px 8px rgba(0,0,0,0.06); display:flex;
                            align-items:center; gap:16px;">
                  <div style="font-size:36px;">📬</div>
                  <div>
                    <div style="font-size:22px; font-weight:800; color:#0f172a;">
                      {len(df_ready)} leads ready to send
                    </div>
                    <div style="font-size:13px; color:#64748b; margin-top:2px;">
                      {"⚠️ " + str(guessed_count) + " have guessed emails (pattern fallback) — "
                       "lower deliverability expected. " if guessed_count else ""}
                      All have status = New. None will be sent twice.
                    </div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            if "email_source" in df_ready.columns:
                df_ready["Email ✔"] = df_ready["email_source"].map(
                    lambda s: "✅ Found" if s == "found" else ("🤔 Guessed" if s == "guessed" else "—")
                )
            show_ready = [c for c in
                ["id","business_name","email","Email ✔","city","country","keyword"]
                if c in df_ready.columns]
            st.dataframe(df_ready[show_ready], use_container_width=True, height=260)

            sc1, sc2, sc3 = st.columns([3, 1, 1])
            with sc1:
                max_send = st.slider(
                    "Emails to send this session",
                    1, min(200, len(df_ready)), min(20, len(df_ready)),
                    help="Start with 20–30/day and ramp up weekly.",
                )
            with sc2:
                est_mins = max(1, (max_send * delay_sec) // 60)
                st.metric("Est. time", f"~{est_mins} min")
            with sc3:
                st.metric("Delay", f"{delay_sec}s")

            st.markdown(
                f"""
                <div style="background:#fffbeb; border:1px solid #fde68a; border-radius:10px;
                            padding:14px 18px; font-size:13px; color:#92400e; margin-bottom:12px;">
                  ⚡ Sending <b>{max_send} emails</b> with <b>{delay_sec}s gap</b> between each.
                  Est. time: <b>~{est_mins} minutes</b>.
                  Each lead will be marked <b>Sent</b> immediately after delivery.
                </div>
                """,
                unsafe_allow_html=True,
            )

            if st.button(f"🚀 Send {max_send} emails now", type="primary", use_container_width=True):
                batch    = df_ready.head(max_send).copy()
                log_area = st.empty()
                send_logs: list = []

                def s_log(msg: str):
                    send_logs.append(msg)
                    log_area.markdown("```\n" + "\n".join(send_logs[-30:]) + "\n```")

                result = send_batch(
                    leads_df=batch,
                    sender_email=sender_email,
                    app_password=app_password,
                    sender_name=sender_name,
                    delay_seconds=delay_sec,
                    log_cb=s_log,
                    update_status_cb=update_status,
                )

                rc1, rc2 = st.columns(2)
                with rc1:
                    _stat_card("✅", "Emails Sent", result["sent"], "#16a34a")
                with rc2:
                    _stat_card("❌", "Failed", result["failed"], "#dc2626")

                if result["errors"]:
                    with st.expander("View error details"):
                        for err in result["errors"]:
                            st.text(err)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 – Analytics
# ═════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.markdown("### 📊 Analytics Dashboard")

    # Stat cards row
    a1, a2, a3, a4, a5 = st.columns(5)
    with a1:
        _stat_card("🏢", "Total Leads",   stats.get("total",    0), "#2563eb")
    with a2:
        _stat_card("🔵", "New (unsent)",  stats.get("new",      0), "#7c3aed",
                   "→ go to Send Emails")
    with a3:
        _stat_card("✅", "Emails Sent",   stats.get("sent",     0), "#16a34a")
    with a4:
        _stat_card("❌", "Failed",        stats.get("failed",   0), "#dc2626")
    with a5:
        _stat_card("📭", "No Email",      stats.get("no_email", 0), "#d97706")

    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

    df_all = get_leads()
    if df_all.empty:
        st.info("No data yet. Start by finding leads.")
    else:
        # Email find rate
        has_email  = df_all["email"].notna() & (df_all["email"] != "")
        found_pct  = round(has_email.sum() / len(df_all) * 100, 1)

        confirmed = guessed = 0
        if "email_source" in df_all.columns:
            confirmed = int((df_all["email_source"] == "found").sum())
            guessed   = int((df_all["email_source"] == "guessed").sum())

        st.markdown(
            f"""
            <div style="background:white; border-radius:14px; padding:20px 24px;
                        box-shadow:0 2px 8px rgba(0,0,0,0.06); margin-bottom:16px;">
              <div style="font-weight:700; color:#0f172a; margin-bottom:12px; font-size:15px;">
                📧 Email Coverage
              </div>
              <div style="background:#f1f5f9; border-radius:100px; height:12px; overflow:hidden; margin-bottom:12px;">
                <div style="background:linear-gradient(90deg,#16a34a,#22c55e);
                            width:{found_pct}%; height:100%; border-radius:100px;"></div>
              </div>
              <div style="display:flex; gap:24px; flex-wrap:wrap; font-size:13px;">
                <span><b style="color:#0f172a; font-size:22px;">{found_pct}%</b>
                      <span style="color:#64748b;"> of leads have an email</span></span>
                <span style="background:#f0fdf4; color:#16a34a; border-radius:8px;
                             padding:4px 12px; font-weight:700;">
                  ✅ {confirmed} confirmed on site</span>
                <span style="background:#fffbeb; color:#92400e; border-radius:8px;
                             padding:4px 12px; font-weight:700;">
                  🤔 {guessed} guessed (pattern)</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        ac1, ac2 = st.columns(2)
        with ac1:
            st.markdown(
                "<div style='background:white; border-radius:14px; padding:20px 24px; "
                "box-shadow:0 2px 8px rgba(0,0,0,0.06); margin-bottom:16px;'>"
                "<div style='font-weight:700; color:#0f172a; margin-bottom:12px;'>🌍 Leads by Country</div>",
                unsafe_allow_html=True,
            )
            cc = df_all.groupby("country").size().reset_index(name="count")
            st.bar_chart(cc.set_index("country"), color="#2563eb")
            st.markdown("</div>", unsafe_allow_html=True)

        with ac2:
            st.markdown(
                "<div style='background:white; border-radius:14px; padding:20px 24px; "
                "box-shadow:0 2px 8px rgba(0,0,0,0.06); margin-bottom:16px;'>"
                "<div style='font-weight:700; color:#0f172a; margin-bottom:12px;'>📈 Leads by Status</div>",
                unsafe_allow_html=True,
            )
            sc_df = df_all.groupby("status").size().reset_index(name="count")
            st.bar_chart(sc_df.set_index("status"), color="#7c3aed")
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown(
            "<div style='background:white; border-radius:14px; padding:20px 24px; "
            "box-shadow:0 2px 8px rgba(0,0,0,0.06); margin-bottom:16px;'>"
            "<div style='font-weight:700; color:#0f172a; margin-bottom:12px;'>🔑 Leads by Keyword</div>",
            unsafe_allow_html=True,
        )
        kc = df_all.groupby("keyword").size().reset_index(name="count")
        st.bar_chart(kc.set_index("keyword"), color="#16a34a")
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown(
            "<div style='background:white; border-radius:14px; padding:20px 24px; "
            "box-shadow:0 2px 8px rgba(0,0,0,0.06);'>"
            "<div style='font-weight:700; color:#0f172a; margin-bottom:12px;'>📅 Daily Lead Volume</div>",
            unsafe_allow_html=True,
        )
        df_all["date"] = pd.to_datetime(df_all["created_at"]).dt.date
        daily = df_all.groupby("date").size().reset_index(name="leads")
        st.line_chart(daily.set_index("date"), color="#2563eb")
        st.markdown("</div>", unsafe_allow_html=True)
