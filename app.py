"""
app.py – SEO Outreach Engine
Streamlit web app: find SMB leads → extract emails → send cold outreach.

Deploy free at https://streamlit.io/cloud
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
    get_stats, init_db, insert_lead, update_status,
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
# Access is controlled by the `allowed_emails` list in Streamlit Secrets.
# To grant access to a new user → add their Gmail to that list and save.
# ─────────────────────────────────────────────────────────────────────────────

_GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/auth"
_GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _decode_id_token(id_token: str) -> dict:
    """Decode the JWT payload from Google's id_token (no signature verify needed)."""
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)   # fix padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _login_page() -> bool:
    """
    Render the Google Sign-In page.
    Returns True when the user is authenticated and allowed.
    """
    if st.session_state.get("_authenticated"):
        return True

    # ── Load secrets ────────────────────────────────────────────────────────
    try:
        CLIENT_ID      = st.secrets["google_client_id"]
        CLIENT_SECRET  = st.secrets["google_client_secret"]
        REDIRECT_URI   = st.secrets["redirect_uri"]
        ALLOWED_EMAILS = [e.strip().lower() for e in st.secrets["allowed_emails"]]
    except KeyError as exc:
        st.error(
            f"⚠️ Missing secret key: **{exc}**. "
            "Go to Streamlit Cloud → your app → Settings → Secrets and add all required keys."
        )
        return False

    # ── Centred login card ───────────────────────────────────────────────────
    _, card, _ = st.columns([1, 1.1, 1])
    with card:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown(
            """
            <div style='text-align:center; margin-bottom:28px;'>
                <div style='font-size:58px; margin-bottom:4px;'>🚀</div>
                <div style='font-size:26px; font-weight:800; color:#1e3a8a; letter-spacing:-.5px;'>
                    SEO Outreach Engine
                </div>
                <div style='font-size:13px; color:#6b7280; margin-top:6px;'>
                    Private workspace — authorised accounts only
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        oauth2 = OAuth2Component(
            CLIENT_ID, CLIENT_SECRET,
            _GOOGLE_AUTH_URL,
            _GOOGLE_TOKEN_URL, _GOOGLE_TOKEN_URL,   # token + refresh
            _GOOGLE_REVOKE_URL,
        )

        result = oauth2.authorize_button(
            name="Sign in with Google",
            redirect_uri=REDIRECT_URI,
            scope="openid email profile",
            icon="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg",
            use_container_width=True,
            key="google_login_btn",
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
                st.markdown("<br>", unsafe_allow_html=True)
                st.error(
                    f"🚫 **Access denied** for `{email}`.\n\n"
                    "This Google account has not been granted access. "
                    "Contact the admin to request access."
                )

        st.markdown(
            "<div style='text-align:center; margin-top:24px; font-size:12px; color:#9ca3af;'>"
            "🔒 Secured with Google OAuth 2.0</div>",
            unsafe_allow_html=True,
        )
        return False


# Block everything below until login succeeds
if not _login_page():
    st.stop()

# Bootstrap database
init_db()

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar – global settings
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image(
        "https://img.icons8.com/fluency/96/rocket.png", width=60
    )
    st.title("SEO Outreach Engine")
    st.caption("Find → Extract → Send")

    # ── Logged-in user info + logout ─────────────────────────────────────────
    user_name  = st.session_state.get("_user_name", "")
    user_email = st.session_state.get("_user_email", "")
    st.markdown(
        f"<div style='background:#eff6ff; border-radius:8px; padding:10px 12px; "
        f"font-size:13px; color:#1e40af; margin-bottom:4px;'>"
        f"👤 <b>{user_name}</b><br>"
        f"<span style='color:#6b7280; font-size:12px;'>{user_email}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.button("🚪 Sign out", use_container_width=True):
        for key in ["_authenticated", "_user_email", "_user_name", "google_login_btn"]:
            st.session_state.pop(key, None)
        st.rerun()
    st.divider()

    # ── Gmail SMTP  (pre-filled from secrets, editable any time) ───────────
    st.subheader("📧 Gmail SMTP")

    # Load saved defaults from secrets (if set) – user can still override
    _def_email = st.secrets.get("smtp_email",    "")
    _def_name  = st.secrets.get("smtp_name",     "")
    _def_pass  = st.secrets.get("smtp_password", "")
    _def_yelp  = st.secrets.get("yelp_api_key",  "")

    sender_email = st.text_input(
        "Sending Gmail address",
        value=_def_email,
        placeholder="you@gmail.com",
        key="s_email",
        help="Can be any Gmail — doesn't have to match your login Gmail.",
    )
    app_password = st.text_input(
        "Gmail App Password",
        value=_def_pass,
        type="password",
        key="s_pass",
        help=(
            "Not your main password. Generate at "
            "myaccount.google.com → Security → App Passwords"
        ),
    )
    sender_name = st.text_input(
        "Your name (shown in From:)",
        value=_def_name,
        placeholder="John Smith",
        key="s_name",
    )

    # ── Google Places API key (best data source) ────────────────────────────
    _def_gplaces = st.secrets.get("google_places_key", "")
    with st.expander("🗺️ Google Places API Key  (recommended)", expanded=bool(_def_gplaces)):
        google_places_key = st.text_input(
            "Google Places API Key",
            value=_def_gplaces,
            type="password",
            key="s_gplaces",
            help=(
                "Enable 'Places API' in Google Cloud Console → APIs & Services → Library. "
                "Then create an API key under Credentials. $200 free credit/month."
            ),
        )
        if _def_gplaces:
            st.success("✅ Google Places API active — best data quality")
        else:
            st.info("Add key for best results. Works for AU, NZ, UK, UAE, USA.")

    # ── Optional: Yelp API key ───────────────────────────────────────────────
    with st.expander("🟡 Yelp API Key  (optional, better data)"):
        yelp_key = st.text_input(
            "Yelp Fusion API Key",
            value=_def_yelp,
            type="password",
            key="s_yelp",
            help="Free at yelp.com/developers — 500 calls/day. Covers AU/NZ/UK/USA.",
        )

    st.divider()

    # ── Rate limiting ────────────────────────────────────────────────────────
    st.subheader("⏱️ Rate Limiting")
    delay_sec = st.slider(
        "Seconds between emails", min_value=30, max_value=180, value=60,
        help="45–90 s is safe for Gmail. More delay = better deliverability.",
    )

    # ── Safety notice ────────────────────────────────────────────────────────
    st.divider()
    st.warning(
        """
**⚠️ Gmail Safety**
- **Start with 20–30 emails/day**, then ramp up weekly
- Sending 500/day on day 1 will flag your account
- Use a **Gmail App Password** (never your main password)
- Consider **Google Workspace** ($6/month) for a custom domain and higher limits
- All emails include an unsubscribe notice (CAN-SPAM compliant)
        """
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
    st.header("🔍 Find & Qualify Leads")
    st.caption(
        "Enter a business type and location. The app will scrape public directories, "
        "visit each website, and extract contact emails automatically."
    )

    # ── Input row ─────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        keyword = st.text_input(
            "Business keyword",
            placeholder="e.g.  HVAC  ·  cake shop  ·  plumber  ·  roofer",
        )
    with c2:
        location = st.text_input(
            "City / location",
            placeholder="e.g.  Melbourne  ·  Burlington Vermont  ·  Dubai",
        )
    with c3:
        country = st.selectbox("Country", list(COUNTRY_SCRAPERS.keys()))

    c4, c5 = st.columns([1, 1])
    with c4:
        max_pages = st.slider(
            "Pages to scrape", 1, 10, 3,
            help="Each page yields ~10–25 businesses. More pages = more leads + longer wait.",
        )
    with c5:
        auto_email = st.toggle(
            "Auto-find emails from websites", value=True,
            help="Visits each business's website and extracts their contact email.",
        )

    # ── Run ───────────────────────────────────────────────────────────────
    if st.button("🚀 Find Leads", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("Please enter a business keyword (e.g. 'plumber').")
        elif not location.strip():
            st.error("Please enter a location (e.g. 'Melbourne').")
        else:
            log_box    = st.empty()
            prog_bar   = st.progress(0, text="Starting …")
            log_lines: list = []

            def log(msg: str):
                log_lines.append(msg)
                log_box.markdown(
                    "```\n" + "\n".join(log_lines[-25:]) + "\n```"
                )

            log(f"🔎 Searching: '{keyword}' in {location}, {country}")

            businesses = find_businesses(
                keyword=keyword.strip(),
                location=location.strip(),
                country=country,
                max_pages=max_pages,
                yelp_api_key=st.session_state.get("s_yelp", ""),
                google_places_key=st.session_state.get("s_gplaces", ""),
                log_cb=log,
            )

            if not businesses:
                st.warning(
                    "No businesses found. Try a different keyword or location, "
                    "or increase the page count."
                )
                st.stop()

            st.success(f"Found **{len(businesses)}** businesses — processing …")

            new_count    = 0
            no_email_ct  = 0

            for idx, biz in enumerate(businesses):
                pct = int((idx + 1) / len(businesses) * 100)
                prog_bar.progress(
                    pct,
                    text=f"Processing {idx + 1}/{len(businesses)}: "
                         f"{biz.get('business_name', '')}",
                )

                # Email discovery
                if auto_email and biz.get("website"):
                    log(f"🔗  {biz['business_name']} → {biz['website']}")
                    found = find_email_on_website(biz["website"])
                    if found:
                        biz["email"] = found
                        log(f"    📧  {found}")
                    else:
                        biz["email"]  = None
                        biz["status"] = "no_email"
                        log("    ⚠️  No email found")
                elif not auto_email:
                    biz["email"]  = None
                    biz["status"] = "no_email"

                if not biz.get("email"):
                    no_email_ct += 1

                if insert_lead(biz):
                    new_count += 1

            prog_bar.progress(100, text="Done!")

            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Businesses scraped", len(businesses))
            mc2.metric("New leads saved",    new_count)
            mc3.metric("No email found",     no_email_ct)

            st.subheader("Preview")
            df_prev = pd.DataFrame(businesses)
            show_cols = [
                c for c in
                ["business_name", "email", "phone", "website", "address", "city", "source"]
                if c in df_prev.columns
            ]
            st.dataframe(df_prev[show_cols], use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 – Leads Database
# ═════════════════════════════════════════════════════════════════════════════
with tab_db:
    st.header("📋 Leads Database")

    filter_col, btn_col = st.columns([3, 1])
    with filter_col:
        status_filter = st.selectbox(
            "Filter by status",
            ["all", "new", "sent", "failed", "no_email"],
        )
    with btn_col:
        st.write("")   # vertical spacer
        if st.button("🔄 Refresh"):
            st.rerun()

    df_db = get_leads(None if status_filter == "all" else status_filter)

    if df_db.empty:
        st.info("No leads yet. Head to **Find Leads** to get started.")
    else:
        st.info(f"Showing **{len(df_db)}** leads")

        show = [
            "id", "business_name", "email", "phone",
            "city", "country", "keyword", "source",
            "status", "email_sent_at", "created_at",
        ]
        show = [c for c in show if c in df_db.columns]
        st.dataframe(df_db[show], use_container_width=True, height=480)

        # ── Export ─────────────────────────────────────────────────────────
        csv_bytes = df_db.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Export as CSV",
            data=csv_bytes,
            file_name=f"leads_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )

        # ── Delete ─────────────────────────────────────────────────────────
        with st.expander("🗑️ Delete leads"):
            ids_input = st.text_input(
                "Enter lead IDs to delete (comma-separated)",
                placeholder="1, 5, 12",
            )
            if st.button("Delete", type="primary"):
                try:
                    ids = [int(x.strip()) for x in ids_input.split(",") if x.strip()]
                    if ids:
                        delete_leads(ids)
                        st.success(f"Deleted {len(ids)} lead(s).")
                        st.rerun()
                except ValueError:
                    st.error("Invalid ID format. Use numbers separated by commas.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 – Send Emails
# ═════════════════════════════════════════════════════════════════════════════
with tab_send:
    st.header("✉️ Send Emails")

    # ── SMTP gate ─────────────────────────────────────────────────────────
    if not _smtp_ready():
        st.warning(
            "⚙️ Fill in your **Gmail address**, **App Password**, and **name** "
            "in the left sidebar before sending."
        )
        st.info(
            "📖 How to create a Gmail App Password: "
            "[Google guide](https://support.google.com/accounts/answer/185833)"
        )
    else:
        # ── Email preview ──────────────────────────────────────────────────
        with st.expander("👁️ Preview email template", expanded=False):
            prev_html = EMAIL_TEMPLATE_HTML.format(
                business_name="ABC Plumbing Services",
                sender_name=sender_name,
                sender_email=sender_email,
            )
            st.markdown(prev_html, unsafe_allow_html=True)
            st.divider()
            st.text("Plain-text version:")
            st.text(
                EMAIL_TEMPLATE_TEXT.format(
                    business_name="ABC Plumbing Services",
                    sender_name=sender_name,
                    sender_email=sender_email,
                )
            )

        st.subheader("Ready-to-send leads")
        df_ready = get_leads_with_email(status="new")

        if df_ready.empty:
            st.info(
                "No leads with emails ready to send. "
                "Go to **Find Leads**, enable 'Auto-find emails', and scrape some leads first."
            )
        else:
            st.success(f"📬 **{len(df_ready)}** leads ready")

            show_ready = [
                c for c in
                ["id", "business_name", "email", "city", "country", "keyword"]
                if c in df_ready.columns
            ]
            st.dataframe(df_ready[show_ready], use_container_width=True, height=280)

            sc1, sc2 = st.columns([3, 1])
            with sc1:
                max_send = st.slider(
                    "Emails to send this session",
                    1, min(200, len(df_ready)),
                    min(20, len(df_ready)),
                    help="Start small — increase as your account warms up.",
                )
            with sc2:
                est_mins = max(1, (max_send * delay_sec) // 60)
                st.metric("Est. time", f"~{est_mins} min")

            st.warning(
                f"You are about to send **{max_send} emails** with a **{delay_sec}s delay** "
                f"between each one. Estimated completion: ~{est_mins} minutes."
            )

            if st.button(f"🚀 Send {max_send} emails now", type="primary"):
                batch = df_ready.head(max_send).copy()
                log_area = st.empty()
                send_logs: list = []

                def s_log(msg: str):
                    send_logs.append(msg)
                    log_area.markdown(
                        "```\n" + "\n".join(send_logs[-30:]) + "\n```"
                    )

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
                rc1.metric("✅ Emails sent",  result["sent"])
                rc2.metric("❌ Failed",        result["failed"])

                if result["errors"]:
                    with st.expander("View errors"):
                        for err in result["errors"]:
                            st.text(err)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 – Analytics
# ═════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.header("📊 Analytics")

    stats = get_stats()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Leads",    stats.get("total",    0))
    m2.metric("New (unsent)",   stats.get("new",      0))
    m3.metric("Emails Sent",    stats.get("sent",     0))
    m4.metric("Failed",         stats.get("failed",   0))
    m5.metric("No Email",       stats.get("no_email", 0))

    st.divider()

    df_all = get_leads()
    if df_all.empty:
        st.info("No data yet. Start by finding leads.")
    else:
        ac1, ac2 = st.columns(2)

        with ac1:
            st.subheader("Leads by Country")
            cc = df_all.groupby("country").size().reset_index(name="count")
            st.bar_chart(cc.set_index("country"))

        with ac2:
            st.subheader("Leads by Status")
            sc = df_all.groupby("status").size().reset_index(name="count")
            st.bar_chart(sc.set_index("status"))

        st.subheader("Leads by Keyword")
        kc = df_all.groupby("keyword").size().reset_index(name="count")
        st.bar_chart(kc.set_index("keyword"))

        st.subheader("Email find rate")
        has_email = df_all["email"].notna() & (df_all["email"] != "")
        found_pct  = round(has_email.sum() / len(df_all) * 100, 1) if len(df_all) else 0
        st.progress(
            int(found_pct),
            text=f"{found_pct}% of leads have an email address",
        )

        st.subheader("Daily lead volume")
        df_all["date"] = pd.to_datetime(df_all["created_at"]).dt.date
        daily = df_all.groupby("date").size().reset_index(name="leads")
        st.line_chart(daily.set_index("date"))
