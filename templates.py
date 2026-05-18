"""
templates.py – Email copy for SEO / GEO / AEO outreach.

Three battle-tested cold email variants:
  1. SHORT   – under 100 words, conversational, highest reply rate
  2. STORY   – brief pain-point narrative + one clear CTA, ~150 words
  3. DETAILED – full service breakdown with pricing, best for warm-ish leads

Subject lines rotate randomly per send to reduce spam-filter pattern matching.
Follow-up templates fire on day 3 and day 7 after the first touch.

Deliverability notes:
  - "FREE" is a top-3 spam trigger — replaced with "complimentary" / dropped
  - Dollar signs removed (UK audience → pound, but even £ should be buried in body)
  - Each template has a matching plain-text part for proper multipart/alternative
  - List-Unsubscribe header is injected by brevo_sender.py at send time
"""
import random

# ─────────────────────────────────────────────────────────────────────────────
# Subject lines  (rotate randomly — no "FREE", no $ signs, no all-caps words)
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


def get_random_subject(business_name: str) -> str:
    return random.choice(SUBJECT_LINES).format(business_name=business_name)

def get_followup_subject(business_name: str, touch: int = 1) -> str:
    pool = FOLLOWUP_SUBJECT_LINES if touch == 1 else FOLLOWUP2_SUBJECT_LINES
    return random.choice(pool).format(business_name=business_name)


# ─────────────────────────────────────────────────────────────────────────────
# Shared CSS (used by all HTML templates)
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
  .cta     { display:inline-block; background:#4f46e5; color:#fff !important;
             border-radius:8px; padding:11px 22px; font-size:14px;
             font-weight:600; margin:8px 0 20px; letter-spacing:-.1px; }
  .highlight { background:#f0fdf4; border-left:3px solid #10b981;
               border-radius:0 6px 6px 0; padding:12px 16px;
               margin:16px 0; font-size:14px; color:#065f46; }
  .card    { background:#f8fafc; border:1px solid #e2e8f0;
             border-radius:8px; padding:14px 16px; margin:10px 0; }
  .card .ct { font-weight:700; color:#0f172a; margin:0 0 4px;
              font-size:14px; }
  .card .cd { margin:0; font-size:13px; color:#64748b; }
  .price   { text-align:center; background:#fafafa; border:1px solid #e2e8f0;
             border-radius:10px; padding:20px; margin:22px 0; }
  .price .amt { font-size:28px; font-weight:800; color:#4f46e5; }
  .price .note { font-size:13px; color:#94a3b8; margin-top:4px; }
"""

# Plain-text footer — simple, no HTML
_FOOTER_TEXT = (
    "\n\n---\n"
    "You are receiving this because {business_name} is publicly listed online.\n"
    "To stop receiving messages, simply reply with the word Unsubscribe.\n"
)

# HTML footer
_FOOTER_HTML = """
  <div class="footer">
    You are receiving this because <strong>{business_name}</strong> is publicly
    listed online. To stop receiving messages from us, simply reply with the
    word <strong>Unsubscribe</strong> and we will remove you straight away.
  </div>
"""


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 1 — SHORT  (best open-to-reply rate for cold email)
# ─────────────────────────────────────────────────────────────────────────────

def build_short_html(business_name: str, sender_name: str, sender_email: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<p>Hi {business_name} team,</p>

<p>I was searching for services in your area and noticed
<strong>{business_name}</strong> isn't showing up on page 1 of Google
— or in AI tools like ChatGPT and Gemini, where more and more customers
are now searching for local businesses.</p>

<p>I help local businesses improve exactly that. Would you be open to a
quick 10-minute call to see what's holding your rankings back?</p>

<p>No obligation — just an honest look at where things stand.</p>

<div class="sig">
  <strong>{sender_name}</strong><br>
  <a href="mailto:{sender_email}">{sender_email}</a><br>
  <span style="font-size:12px;color:#94a3b8;">SEO &amp; Local Visibility Specialist</span>
</div>

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


SHORT_TEXT = """\
Hi {business_name} team,

I was searching for services in your area and noticed {business_name} isn't
showing up on page 1 of Google — or in AI tools like ChatGPT and Gemini,
where more customers now search for local businesses.

I help local businesses improve exactly that. Would you be open to a quick
10-minute call to see what's holding your rankings back?

No obligation — just an honest look at where things stand.

{sender_name}
{sender_email}
SEO & Local Visibility Specialist
""" + _FOOTER_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 2 — STORY  (narrative + single CTA, ~150 words)
# ─────────────────────────────────────────────────────────────────────────────

def build_story_html(business_name: str, sender_name: str, sender_email: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<span class="tag">SEO · GEO · AEO</span>

<p>Hi {business_name} team,</p>

<p>Something I see all the time: a great local business with a solid
reputation and happy customers — but barely visible online. Competitors
with half the quality are ranking above them simply because they have
invested in the right digital strategy.</p>

<p>I had a look and <strong>{business_name}</strong> has real potential
to rank significantly higher on Google, and to start appearing in AI
search tools like ChatGPT, Gemini and Perplexity — where customers
increasingly discover local services.</p>

<div class="highlight">
  Most businesses I work with see a measurable increase in enquiries
  within 60–90 days. No lock-in contracts.
</div>

<p>I would love to put together a complimentary visibility audit for
{business_name} — a clear picture of where you stand and exactly what
can be improved. Takes about 10 minutes of your time.</p>

<p>Interested?</p>

<div class="sig">
  <strong>{sender_name}</strong><br>
  <a href="mailto:{sender_email}">{sender_email}</a><br>
  <span style="font-size:12px;color:#94a3b8;">SEO &amp; Local Visibility Specialist</span>
</div>

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


STORY_TEXT = """\
Hi {business_name} team,

Something I see all the time: a great local business with a solid reputation
and happy customers — but barely visible online. Competitors with half the
quality rank above them simply because they have invested in the right digital
strategy.

{business_name} has real potential to rank higher on Google, and to start
appearing in AI tools like ChatGPT and Gemini where customers increasingly
discover local services.

Most businesses I work with see measurable growth in enquiries within 60-90
days. No lock-in contracts.

I would love to put together a complimentary visibility audit for
{business_name} — a clear picture of where you stand and what can be improved.

Interested?

{sender_name}
{sender_email}
SEO & Local Visibility Specialist
""" + _FOOTER_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 3 — DETAILED  (full service breakdown + pricing, warm leads)
# ─────────────────────────────────────────────────────────────────────────────

def build_detailed_html(business_name: str, sender_name: str, sender_email: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<span class="tag">SEO · GEO · AEO Services</span>

<p>Hi {business_name} team,</p>

<p>I came across <strong>{business_name}</strong> and wanted to share
something that could genuinely help more customers find you — especially
with how fast search is changing in 2025.</p>

<p>We help local businesses grow through three complementary services:</p>

<div class="card">
  <p class="ct">🔍 SEO – Search Engine Optimisation</p>
  <p class="cd">Rank on page 1 of Google for the exact searches your
  customers make. More visibility, more calls, more bookings.</p>
</div>

<div class="card">
  <p class="ct">🤖 GEO – Generative Engine Optimisation</p>
  <p class="cd">Get recommended inside ChatGPT, Google Gemini and
  Perplexity — where millions now discover local businesses. Most of
  your competitors are not there yet.</p>
</div>

<div class="card">
  <p class="ct">🎯 AEO – Answer Engine Optimisation</p>
  <p class="cd">Appear in Google Featured Snippets and voice search
  results (Siri, Alexa) so customers choose you before clicking anywhere.</p>
</div>

<div class="price">
  <div class="amt">From £500<span style="font-size:16px;font-weight:400;
       color:#94a3b8">&nbsp;/ month</span></div>
  <p class="note">Tailored packages · No lock-in contracts · Results in 60–90 days</p>
</div>

<p>I would like to offer <strong>{business_name}</strong> a complimentary
online visibility audit — an honest look at where you stand today and
exactly what we can improve. No commitment required.</p>

<p>Would you be open to a quick 15-minute call this week?</p>

<div class="sig">
  <strong>{sender_name}</strong><br>
  <a href="mailto:{sender_email}">{sender_email}</a><br>
  <span style="font-size:12px;color:#94a3b8;">SEO &amp; Local Visibility Specialist</span>
</div>

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


DETAILED_TEXT = """\
Hi {business_name} team,

I came across {business_name} and wanted to share something that could
genuinely help more customers find you — especially with how fast search
is changing in 2025.

We help local businesses grow through three services:

SEO – Rank on page 1 of Google for the searches your customers make.

GEO (Generative Engine Optimisation) – Get recommended inside ChatGPT,
Google Gemini and Perplexity. Most competitors are not there yet.

AEO (Answer Engine Optimisation) – Appear in Featured Snippets and
voice search so customers choose you first.

Packages from £500 per month — tailored to your goals, no lock-in contracts.

I would like to offer {business_name} a complimentary visibility audit — an
honest look at where you stand and what we can improve. No commitment needed.

Would you be open to a quick 15-minute call this week?

{sender_name}
{sender_email}
SEO & Local Visibility Specialist
""" + _FOOTER_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# FOLLOW-UP TEMPLATE  (day 3 after first touch)
# ─────────────────────────────────────────────────────────────────────────────

def build_followup_html(business_name: str, sender_name: str, sender_email: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="body">

<p>Hi {business_name} team,</p>

<p>Just following up on my note from a few days ago in case it got
buried in your inbox.</p>

<p>I genuinely think there is a quick win available for
<strong>{business_name}</strong> in local search — and I would hate for a
competitor to get there first.</p>

<p>Happy to keep it to 10 minutes. Would any time this week work for you?</p>

<div class="sig">
  <strong>{sender_name}</strong><br>
  <a href="mailto:{sender_email}">{sender_email}</a>
</div>

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


FOLLOWUP_TEXT = """\
Hi {business_name} team,

Just following up on my note from a few days ago in case it got buried.

I genuinely think there is a quick win available for {business_name} in local
search — and I would hate for a competitor to get there first.

Happy to keep it to 10 minutes. Would any time this week work?

{sender_name}
{sender_email}
""" + _FOOTER_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# FOLLOW-UP 2  (day 7 — final touch)
# ─────────────────────────────────────────────────────────────────────────────

def build_followup2_html(business_name: str, sender_name: str, sender_email: str) -> str:
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
to this email anytime.</p>

<p>Wishing you a great week.</p>

<div class="sig">
  <strong>{sender_name}</strong><br>
  <a href="mailto:{sender_email}">{sender_email}</a>
</div>

</div>
{_FOOTER_HTML.format(business_name=business_name)}
</div></body></html>"""


FOLLOWUP2_TEXT = """\
Hi {business_name} team,

I will keep this brief — I do not want to keep filling your inbox.

If the timing is not right, no worries. But if you ever want to explore
how {business_name} could show up better online, my offer for a complimentary
audit stands — just reply anytime.

Wishing you a great week.

{sender_name}
{sender_email}
""" + _FOOTER_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATE_OPTIONS = {
    "short":    ("Short & punchy",    "~80 words · highest reply rate · best for cold first touch"),
    "story":    ("Narrative",         "~150 words · pain-point story · great open→reply conversion"),
    "detailed": ("Full breakdown",    "~200 words · services + pricing · best for warm or interested leads"),
}

FOLLOWUP_OPTIONS = {
    "followup1": ("Follow-up 1 (Day 3)", "Short nudge referencing the original email"),
    "followup2": ("Follow-up 2 (Day 7)", "Final polite close — leaves door open"),
}


def build_html(template: str, business_name: str,
               sender_name: str, sender_email: str) -> str:
    builders = {
        "short":     build_short_html,
        "story":     build_story_html,
        "detailed":  build_detailed_html,
        "followup1": build_followup_html,
        "followup2": build_followup2_html,
    }
    fn = builders.get(template, build_short_html)
    return fn(business_name, sender_name, sender_email)


def build_text(template: str, business_name: str,
               sender_name: str, sender_email: str) -> str:
    texts = {
        "short":     SHORT_TEXT,
        "story":     STORY_TEXT,
        "detailed":  DETAILED_TEXT,
        "followup1": FOLLOWUP_TEXT,
        "followup2": FOLLOWUP2_TEXT,
    }
    tmpl = texts.get(template, SHORT_TEXT)
    return tmpl.format(
        business_name=business_name,
        sender_name=sender_name,
        sender_email=sender_email,
    )


# Legacy aliases kept so existing import `EMAIL_TEMPLATE_HTML` still works
EMAIL_TEMPLATE_HTML = build_detailed_html(
    "{business_name}", "{sender_name}", "{sender_email}"
)
EMAIL_TEMPLATE_TEXT = DETAILED_TEXT
SUBJECT_LINES = SUBJECT_LINES          # re-export
