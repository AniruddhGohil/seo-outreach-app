"""
templates.py – Email copy for SEO / GEO / AEO outreach.

Templates:
  SHORT    – ~80 words, highest reply rate, cold first touch
  STORY    – ~150 words, narrative + pain point
  DETAILED – ~200 words, full services breakdown
  ECOMM    – ~130 words, specifically for e-commerce / online stores
  FOLLOWUP1 / FOLLOWUP2 – day 3 / day 7 follow-ups

All templates accept optional `portfolio_url` and `case_study` parameters
so social proof and a portfolio link appear in every email.

Deliverability rules applied:
  - No "FREE" anywhere (subject or body) — top-3 spam trigger
  - No currency symbols in subject lines
  - List-Unsubscribe header injected at send time by brevo_sender.py
"""
import random

# ─────────────────────────────────────────────────────────────────────────────
# Defaults (overridden via sidebar inputs in app.py)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PORTFOLIO_URL = "https://aniruddhgohil.github.io/aniruddh-profile/"
DEFAULT_CASE_STUDY    = "+238% organic revenue for an e-commerce brand in under 12 months"


# ─────────────────────────────────────────────────────────────────────────────
# Subject lines  (no "FREE", no currency, no all-caps)
# ─────────────────────────────────────────────────────────────────────────────

SUBJECT_LINES = [
    "Quick question about {business_name}",
    "{business_name} – noticed something worth sharing",
    "Are customers finding {business_name} online?",
    "{business_name} – Google rankings",
    "Noticed something about {business_name}",
    "{business_name} – showing up in AI search?",
    "2 mins – could help {business_name} get more calls",
    "{business_name} – visibility check",
    "Something I spotted about {business_name}",
    "{business_name} – a thought on local search",
    "{business_name} – the window is closing",
    "Is {business_name} ready for the AI-search shift?",
    "{business_name} – before your competitors move",
    "Who Google recommends is changing — {business_name}",
]

ECOMM_SUBJECT_LINES = [
    "{business_name} – organic revenue opportunity",
    "Quick question about {business_name}'s Google rankings",
    "{business_name} – product pages missing traffic?",
    "How {business_name} could rank higher on Google",
    "{business_name} – SEO audit thought",
    "Noticed something about {business_name}'s search presence",
]

FOLLOWUP_SUBJECT_LINES = [
    "Re: {business_name} – just checking in",
    "Following up – {business_name}",
    "Still thinking about {business_name}'s visibility",
    "One more thought, {business_name}",
]

FOLLOWUP2_SUBJECT_LINES = [
    "Last note – {business_name}",
    "Closing the loop – {business_name}",
    "{business_name} – one last thought",
]


def get_random_subject(business_name: str, template: str = "short") -> str:
    pool = ECOMM_SUBJECT_LINES if template == "ecomm" else SUBJECT_LINES
    return random.choice(pool).format(business_name=business_name)

def get_followup_subject(business_name: str, touch: int = 1) -> str:
    pool = FOLLOWUP_SUBJECT_LINES if touch == 1 else FOLLOWUP2_SUBJECT_LINES
    return random.choice(pool).format(business_name=business_name)


# ─────────────────────────────────────────────────────────────────────────────
# Shared CSS
# ─────────────────────────────────────────────────────────────────────────────

_BASE_CSS = """
  body  { margin:0; padding:0; background:#f8fafc;
          font-family: -apple-system, 'Segoe UI', Arial, sans-serif; }
  .wrap { max-width:580px; margin:28px auto; background:#ffffff;
          border-radius:12px; overflow:hidden;
          box-shadow: 0 2px 16px rgba(0,0,0,.08); }
  .body { padding:36px 40px 28px; color:#1e293b;
          line-height:1.72; font-size:15px; }
  .body p  { margin:0 0 16px; }
  .body a  { color:#4f46e5; text-decoration:none; }
  .sig     { margin-top:24px; padding-top:18px;
             border-top:1px solid #e2e8f0; font-size:14px; color:#475569; }
  .sig strong { color:#0f172a; }
  .footer  { padding:16px 40px; background:#f8fafc;
             border-top:1px solid #e2e8f0;
             font-size:11px; color:#94a3b8; line-height:1.6; }
  .tag     { display:inline-block; background:#eff6ff; color:#4f46e5;
             border-radius:4px; padding:2px 8px; font-size:12px;
             font-weight:600; margin-bottom:18px; }
  .highlight { background:#f0fdf4; border-left:3px solid #10b981;
               border-radius:0 6px 6px 0; padding:12px 16px;
               margin:16px 0; font-size:14px; color:#065f46; }
  .card    { background:#f8fafc; border:1px solid #e2e8f0;
             border-radius:8px; padding:14px 16px; margin:10px 0; }
  .card .ct { font-weight:700; color:#0f172a; margin:0 0 4px; font-size:14px; }
  .card .cd { margin:0; font-size:13px; color:#64748b; }
  .proof   { background:#fafaf5; border:1px solid #e2e8f0;
             border-radius:8px; padding:12px 16px; margin:14px 0;
             font-size:13px; color:#374151; }
  .proof strong { color:#0f172a; }
"""

_FOOTER_TEXT = (
    "\n\n---\n"
    "You are receiving this because {business_name} is publicly listed online.\n"
    "To stop receiving messages, simply reply with the word Unsubscribe.\n"
)

_FOOTER_HTML = """
  <div class="footer">
    You are receiving this because <strong>{business_name}</strong> is publicly
    listed online. To stop receiving messages from us, reply with the word
    <strong>Unsubscribe</strong> and we will remove you straight away.
  </div>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Signature builder
# ─────────────────────────────────────────────────────────────────────────────

def _sig_html(sender_name: str, sender_email: str,
              portfolio_url: str = "", title: str = "SEO &amp; AI Search Specialist") -> str:
    port_line = ""
    if portfolio_url:
        port_line = (
            f"<br><a href='{portfolio_url}' style='font-size:12px;color:#4f46e5;'>"
            f"View my work →</a>"
        )
    return (
        f"<div class='sig'>"
        f"<strong>{sender_name}</strong><br>"
        f"<a href='mailto:{sender_email}'>{sender_email}</a>{port_line}<br>"
        f"<span style='font-size:12px;color:#94a3b8;'>{title}</span>"
        f"</div>"
    )


def _sig_text(sender_name: str, sender_email: str, portfolio_url: str = "",
              title: str = "SEO & AI Search Specialist") -> str:
    lines = [sender_name, sender_email]
    if portfolio_url:
        lines.append(portfolio_url)
    lines.append(title)
    return "\n".join(lines)


def _proof_html(case_study: str) -> str:
    if not case_study:
        return ""
    return (
        f"<div class='proof'>"
        f"<strong>Recent result:</strong> {case_study}"
        f"</div>"
    )


def _proof_text(case_study: str) -> str:
    if not case_study:
        return ""
    return f"Recent result: {case_study}\n\n"


# ── AI-era insight block — shared across templates ────────────────────────────

_AI_ERA_HTML = """
<div style="background:#f5f3ff;border-left:3px solid #7c3aed;border-radius:0 8px 8px 0;
            padding:14px 18px;margin:18px 0;font-size:14px;color:#3b0764;line-height:1.7;">
  <strong style="display:block;margin-bottom:6px;color:#4c1d95;">
    The businesses that adapt early gain a massive competitive advantage.
  </strong>
  In the AI-search era, visibility is no longer just about appearing in search
  results. It&#39;s about becoming the source Google trusts enough to <em>cite,
  summarise and recommend</em>. That kind of authority may become more valuable
  than rankings themselves.
</div>
"""

_AI_ERA_TEXT = (
    "The businesses that adapt early gain a massive competitive advantage.\n"
    "In the AI-search era, visibility is no longer just about appearing in\n"
    "search results — it's about becoming the source Google trusts enough to\n"
    "cite, summarise and recommend. That may become more valuable than\n"
    "rankings themselves.\n\n"
)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 1 — SHORT
# ─────────────────────────────────────────────────────────────────────────────

def build_short_html(business_name: str, sender_name: str, sender_email: str,
                     portfolio_url: str = "", case_study: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<p>Hi {business_name} team,</p>

<p>I was searching for services in your area and noticed
<strong>{business_name}</strong> isn't showing up on page 1 of Google
— or in AI tools like ChatGPT and Gemini, where more customers now
discover local businesses.</p>

<p style="font-size:14px;color:#4c1d95;background:#f5f3ff;
          border-left:3px solid #7c3aed;border-radius:0 6px 6px 0;
          padding:10px 14px;margin:14px 0;line-height:1.65;">
  The businesses that adapt early gain a massive competitive advantage —
  because in the AI-search era, being the source Google <em>cites and
  recommends</em> may matter more than rankings alone.
</p>

{_proof_html(case_study)}

<p>I help businesses improve exactly that. Would you be open to a
quick 10-minute call to see what's holding you back?</p>

<p>No obligation — just an honest look at where things stand.</p>

{_sig_html(sender_name, sender_email, portfolio_url)}

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


def _build_short_text(business_name: str, sender_name: str, sender_email: str,
                      portfolio_url: str = "", case_study: str = "") -> str:
    return (
        f"Hi {business_name} team,\n\n"
        f"I was searching for services in your area and noticed {business_name} isn't\n"
        f"showing up on page 1 of Google — or in AI tools like ChatGPT and Gemini,\n"
        f"where more customers now discover local businesses.\n\n"
        f"The businesses that adapt early gain a massive competitive advantage.\n"
        f"In the AI-search era, being the source Google cites and recommends\n"
        f"may matter more than rankings alone.\n\n"
        + _proof_text(case_study) +
        f"I help businesses improve exactly that. Would you be open to a quick\n"
        f"10-minute call to see what's holding you back?\n\n"
        f"No obligation — just an honest look at where things stand.\n\n"
        + _sig_text(sender_name, sender_email, portfolio_url)
        + _FOOTER_TEXT.format(business_name=business_name)
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 2 — STORY
# ─────────────────────────────────────────────────────────────────────────────

def build_story_html(business_name: str, sender_name: str, sender_email: str,
                     portfolio_url: str = "", case_study: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<span class="tag">SEO · GEO · AEO</span>

<p>Hi {business_name} team,</p>

<p>Something I see all the time: a great business with a solid reputation
and happy customers — but barely visible online. Competitors with half the
quality are ranking above them simply because they invested in the right
digital strategy.</p>

<p><strong>{business_name}</strong> has real potential to rank significantly
higher on Google and to start appearing in AI tools like ChatGPT, Gemini and
Perplexity — where customers increasingly discover businesses.</p>

{_AI_ERA_HTML}

{_proof_html(case_study)}

<div class="highlight">
  Most businesses I work with see a measurable increase in enquiries
  within 60–90 days. No lock-in contracts.
</div>

<p>I would love to put together a complimentary visibility audit for
{business_name} — a clear picture of where you stand and exactly what
can be improved. Takes about 10 minutes of your time.</p>

<p>Interested?</p>

{_sig_html(sender_name, sender_email, portfolio_url)}

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


def _build_story_text(business_name: str, sender_name: str, sender_email: str,
                      portfolio_url: str = "", case_study: str = "") -> str:
    return (
        f"Hi {business_name} team,\n\n"
        f"Something I see all the time: a great business with a solid reputation\n"
        f"and happy customers — but barely visible online. Competitors with half\n"
        f"the quality rank above them simply because they invested in the right\n"
        f"digital strategy.\n\n"
        f"{business_name} has real potential to rank higher on Google and to start\n"
        f"appearing in AI tools like ChatGPT and Gemini where customers increasingly\n"
        f"discover businesses.\n\n"
        + _AI_ERA_TEXT +
        _proof_text(case_study) +
        f"Most businesses I work with see measurable growth in enquiries within\n"
        f"60-90 days. No lock-in contracts.\n\n"
        f"I would love to put together a complimentary visibility audit for\n"
        f"{business_name} — a clear picture of where you stand and what can\n"
        f"be improved.\n\n"
        f"Interested?\n\n"
        + _sig_text(sender_name, sender_email, portfolio_url)
        + _FOOTER_TEXT.format(business_name=business_name)
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 3 — DETAILED
# ─────────────────────────────────────────────────────────────────────────────

def build_detailed_html(business_name: str, sender_name: str, sender_email: str,
                        portfolio_url: str = "", case_study: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<span class="tag">SEO · GEO · AEO</span>

<p>Hi {business_name} team,</p>

<p>I came across <strong>{business_name}</strong> and wanted to share
something that could genuinely help more customers find you — especially
with how fast search is changing in 2025.</p>

{_proof_html(case_study)}

<p>We help businesses grow through three complementary services:</p>

<div class="card">
  <p class="ct">🔍 SEO – Search Engine Optimisation</p>
  <p class="cd">Rank on page 1 of Google for the exact searches your
  customers make. More visibility, more calls, more revenue.</p>
</div>

<div class="card">
  <p class="ct">🤖 GEO – Generative Engine Optimisation</p>
  <p class="cd">Get recommended inside ChatGPT, Google Gemini and
  Perplexity — where millions now discover businesses. Most of your
  competitors are not there yet.</p>
</div>

<div class="card">
  <p class="ct">🎯 AEO – Answer Engine Optimisation</p>
  <p class="cd">Appear in Google Featured Snippets and voice search
  results so customers choose you before clicking anywhere.</p>
</div>

{_AI_ERA_HTML}

<div class="highlight">
  📦 Tailored packages · No lock-in contracts · Results in 60–90 days
</div>

<p>I would like to offer <strong>{business_name}</strong> a complimentary
visibility audit — an honest look at where you stand today and exactly
what we can improve. No commitment required.</p>

<p>Would you be open to a quick 15-minute call this week?</p>

{_sig_html(sender_name, sender_email, portfolio_url)}

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


def _build_detailed_text(business_name: str, sender_name: str, sender_email: str,
                          portfolio_url: str = "", case_study: str = "") -> str:
    return (
        f"Hi {business_name} team,\n\n"
        f"I came across {business_name} and wanted to share something that could\n"
        f"genuinely help more customers find you — especially with how fast search\n"
        f"is changing in 2025.\n\n"
        + _proof_text(case_study) +
        f"We help businesses grow through three services:\n\n"
        f"SEO – Rank on page 1 of Google for the searches your customers make.\n\n"
        f"GEO – Get recommended inside ChatGPT, Google Gemini and Perplexity.\n"
        f"Most competitors are not there yet.\n\n"
        f"AEO – Appear in Featured Snippets and voice search so customers choose\n"
        f"you first.\n\n"
        + _AI_ERA_TEXT +
        f"Tailored packages, no lock-in contracts — pricing shared after a quick chat.\n\n"
        f"I would like to offer {business_name} a complimentary visibility audit —\n"
        f"an honest look at where you stand and what we can improve. No commitment.\n\n"
        f"Would you be open to a quick 15-minute call this week?\n\n"
        + _sig_text(sender_name, sender_email, portfolio_url)
        + _FOOTER_TEXT.format(business_name=business_name)
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 4 — ECOMMERCE  (online stores, D2C brands)
# ─────────────────────────────────────────────────────────────────────────────

def build_ecomm_html(business_name: str, sender_name: str, sender_email: str,
                     portfolio_url: str = "", case_study: str = "") -> str:
    cs = case_study or DEFAULT_CASE_STUDY
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<span class="tag">E-commerce SEO</span>

<p>Hi {business_name} team,</p>

<p>I came across <strong>{business_name}</strong> and noticed your product
and category pages have room to rank significantly higher on Google —
which for an online store translates directly into more orders without
paying for ads.</p>

<div class="proof">
  <strong>Recent result:</strong> {cs}
</div>

<p>For e-commerce specifically, I focus on:</p>

<div class="card">
  <p class="ct">🛒 Product &amp; Category Page Rankings</p>
  <p class="cd">Getting your products in front of buyers at the exact
  moment they are searching to purchase.</p>
</div>

<div class="card">
  <p class="ct">🤖 AI Search Visibility</p>
  <p class="cd">When someone asks ChatGPT or Google Gemini "where can
  I buy [product]?" — your store should be the answer.</p>
</div>

<div class="card">
  <p class="ct">📊 Technical SEO &amp; Site Architecture</p>
  <p class="cd">Fixing the crawlability, speed and structure issues that
  silently suppress rankings for most online stores.</p>
</div>

<p>I would love to put together a complimentary audit for
<strong>{business_name}</strong> — a clear picture of where organic
revenue is being left on the table.</p>

<p>Open to a quick 15-minute call?</p>

{_sig_html(sender_name, sender_email, portfolio_url, title='E-commerce SEO Specialist')}

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


def _build_ecomm_text(business_name: str, sender_name: str, sender_email: str,
                       portfolio_url: str = "", case_study: str = "") -> str:
    cs = case_study or DEFAULT_CASE_STUDY
    return (
        f"Hi {business_name} team,\n\n"
        f"I came across {business_name} and noticed your product and category pages\n"
        f"have room to rank significantly higher on Google — which for an online\n"
        f"store translates directly into more orders without paying for ads.\n\n"
        f"Recent result: {cs}\n\n"
        f"For e-commerce I focus on:\n"
        f"- Product & category page rankings (buyers searching to purchase)\n"
        f"- AI search visibility (ChatGPT, Gemini recommending your store)\n"
        f"- Technical SEO & site architecture fixes that suppress most stores\n\n"
        f"I would love to put together a complimentary audit for {business_name} —\n"
        f"a clear picture of where organic revenue is being left on the table.\n\n"
        f"Open to a quick 15-minute call?\n\n"
        + _sig_text(sender_name, sender_email, portfolio_url, title="E-commerce SEO Specialist")
        + _FOOTER_TEXT.format(business_name=business_name)
    )


# ─────────────────────────────────────────────────────────────────────────────
# FOLLOW-UP 1  (day 3)
# ─────────────────────────────────────────────────────────────────────────────

def build_followup_html(business_name: str, sender_name: str, sender_email: str,
                        portfolio_url: str = "", case_study: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<p>Hi {business_name} team,</p>

<p>Just following up on my note from a few days ago — I wanted to add
one more thought in case it is helpful.</p>

<div style="background:#f5f3ff;border-left:3px solid #7c3aed;border-radius:0 8px 8px 0;
            padding:14px 18px;margin:16px 0;font-size:14px;color:#3b0764;line-height:1.7;">
  The businesses moving now on AI-search positioning are the ones locking
  in that visibility advantage. Once competitors figure it out, the window
  closes — and it tends to close fast.
</div>

<p>I genuinely think there is a quick win available for
<strong>{business_name}</strong>, and I would hate for a competitor to
get there first. Happy to keep it to 10 minutes — would any time this
week work?</p>

{_sig_html(sender_name, sender_email, portfolio_url)}

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


def _build_followup_text(business_name: str, sender_name: str, sender_email: str,
                          portfolio_url: str = "", case_study: str = "") -> str:
    return (
        f"Hi {business_name} team,\n\n"
        f"Just following up on my note from a few days ago — I wanted to add\n"
        f"one more thought in case it is helpful.\n\n"
        f"The businesses moving now on AI-search positioning are the ones locking\n"
        f"in that visibility advantage. Once competitors figure it out, the window\n"
        f"closes — and it tends to close fast.\n\n"
        f"I genuinely think there is a quick win available for {business_name},\n"
        f"and I would hate for a competitor to get there first.\n"
        f"Happy to keep it to 10 minutes — would any time this week work?\n\n"
        + _sig_text(sender_name, sender_email, portfolio_url)
        + _FOOTER_TEXT.format(business_name=business_name)
    )


# ─────────────────────────────────────────────────────────────────────────────
# FOLLOW-UP 2  (day 7 — final touch)
# ─────────────────────────────────────────────────────────────────────────────

def build_followup2_html(business_name: str, sender_name: str, sender_email: str,
                          portfolio_url: str = "", case_study: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<p>Hi {business_name} team,</p>

<p>I will keep this brief — I do not want to keep filling your inbox.</p>

<p>If the timing is not right, no worries at all. But if you ever
want to explore how <strong>{business_name}</strong> could show up
better online, my offer for a complimentary audit stands — just reply
anytime.</p>

<p>Wishing you a great week.</p>

{_sig_html(sender_name, sender_email, portfolio_url)}

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


def _build_followup2_text(business_name: str, sender_name: str, sender_email: str,
                           portfolio_url: str = "", case_study: str = "") -> str:
    return (
        f"Hi {business_name} team,\n\n"
        f"I will keep this brief — I do not want to keep filling your inbox.\n\n"
        f"If the timing is not right, no worries. But if you ever want to explore\n"
        f"how {business_name} could show up better online, my offer for a\n"
        f"complimentary audit stands — just reply anytime.\n\n"
        f"Wishing you a great week.\n\n"
        + _sig_text(sender_name, sender_email, portfolio_url)
        + _FOOTER_TEXT.format(business_name=business_name)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATE_OPTIONS = {
    "short":    ("Short & punchy",       "~80 words · highest reply rate · cold first touch"),
    "story":    ("Narrative",            "~150 words · pain-point story · great for cold outreach"),
    "detailed": ("Full breakdown",       "~200 words · services listed · best for warm leads"),
    "ecomm":    ("E-commerce",           "~130 words · targets online stores · uses revenue case study"),
}

FOLLOWUP_OPTIONS = {
    "followup1": ("Follow-up 1 (Day 3)", "Short nudge referencing the original email"),
    "followup2": ("Follow-up 2 (Day 7)", "Final polite close — leaves door open"),
}

_HTML_BUILDERS = {
    "short":     build_short_html,
    "story":     build_story_html,
    "detailed":  build_detailed_html,
    "ecomm":     build_ecomm_html,
    "followup1": build_followup_html,
    "followup2": build_followup2_html,
}

_TEXT_BUILDERS = {
    "short":     _build_short_text,
    "story":     _build_story_text,
    "detailed":  _build_detailed_text,
    "ecomm":     _build_ecomm_text,
    "followup1": _build_followup_text,
    "followup2": _build_followup2_text,
}


def build_html(template: str, business_name: str,
               sender_name: str, sender_email: str,
               portfolio_url: str = "", case_study: str = "") -> str:
    fn = _HTML_BUILDERS.get(template, build_short_html)
    return fn(business_name, sender_name, sender_email, portfolio_url, case_study)


def build_text(template: str, business_name: str,
               sender_name: str, sender_email: str,
               portfolio_url: str = "", case_study: str = "") -> str:
    fn = _TEXT_BUILDERS.get(template, _build_short_text)
    return fn(business_name, sender_name, sender_email, portfolio_url, case_study)


# Legacy aliases
EMAIL_TEMPLATE_HTML = build_detailed_html(
    "{business_name}", "{sender_name}", "{sender_email}"
)
EMAIL_TEMPLATE_TEXT = _build_detailed_text(
    "{business_name}", "{sender_name}", "{sender_email}"
)
SUBJECT_LINES = SUBJECT_LINES   # re-export
