"""
templates.py – Email copy for SEO / GEO / AEO outreach.

Subject lines rotate randomly to reduce spam-filter pattern matching.
"""
import random

SUBJECT_LINES = [
    "Quick question about {business_name}'s online visibility",
    "{business_name} – Are you showing up in AI search results?",
    "Helping {business_name} attract more local customers",
    "Free visibility check for {business_name}",
    "{business_name} – 3 ways to get found online in 2025",
    "Is {business_name} missing out on AI-powered search traffic?",
]

# ---------------------------------------------------------------------------
# HTML template  (rendered in the email client)
# ---------------------------------------------------------------------------
EMAIL_TEMPLATE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body{{margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;}}
  .wrap{{max-width:600px;margin:28px auto;background:#ffffff;border-radius:10px;
         overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.09);}}
  .header{{background:linear-gradient(135deg,#1e3a8a 0%,#2563eb 100%);
           padding:30px 32px;}}
  .header h1{{color:#fff;margin:0;font-size:21px;font-weight:700;letter-spacing:-.3px;}}
  .header p{{color:#bfdbfe;margin:5px 0 0;font-size:13px;}}
  .body{{padding:32px 32px 24px;color:#374151;line-height:1.68;font-size:15px;}}
  .body p{{margin:0 0 15px;}}
  .card{{background:#eff6ff;border-left:4px solid #2563eb;
         border-radius:0 7px 7px 0;padding:14px 18px;margin:12px 0;}}
  .card .ct{{font-weight:700;color:#1e3a8a;margin:0 0 5px;font-size:15px;}}
  .card .cd{{margin:0;font-size:14px;color:#4b5563;}}
  .pricing{{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;
            padding:18px 20px;margin:22px 0;text-align:center;}}
  .pricing .amt{{font-size:30px;font-weight:800;color:#1e40af;}}
  .pricing .note{{font-size:13px;color:#6b7280;margin:5px 0 0;}}
  .footer{{padding:18px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;
           font-size:12px;color:#9ca3af;line-height:1.55;}}
  a{{color:#2563eb;}}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <h1>Grow Your Business Online</h1>
    <p>Professional SEO &nbsp;·&nbsp; GEO &nbsp;·&nbsp; AEO Services</p>
  </div>

  <div class="body">
    <p>Hi there,</p>

    <p>I came across <strong>{business_name}</strong> and wanted to reach out — I believe
    there is a real opportunity to help more customers find you online.</p>

    <p>We help small and medium businesses like yours grow through three targeted
    digital visibility services:</p>

    <div class="card">
      <p class="ct">🔍 SEO – Search Engine Optimization</p>
      <p class="cd">Rank higher on Google so local customers find <em>you</em> first,
      not your competitors. We focus on the exact keywords your customers search for.</p>
    </div>

    <div class="card">
      <p class="ct">🤖 GEO – Generative Engine Optimization</p>
      <p class="cd">Get your business recommended inside AI tools such as ChatGPT,
      Google Gemini, and Perplexity — where millions of people now discover local
      services. This is the new frontier of search, and most businesses are missing it.</p>
    </div>

    <div class="card">
      <p class="ct">🎯 AEO – Answer Engine Optimization</p>
      <p class="cd">Appear as the direct answer in voice searches (Siri, Alexa,
      Google Assistant) and in Google Featured Snippets — so customers choose you
      before they even click a link.</p>
    </div>

    <div class="pricing">
      <div class="amt">From $600<span style="font-size:16px;font-weight:400;
           color:#6b7280">&nbsp;/ month</span></div>
      <p class="note">Packages tailored to your goals &amp; budget &nbsp;·&nbsp;
      No lock-in contracts</p>
    </div>

    <p>I'd love to offer <strong>{business_name}</strong> a
    <strong>complimentary online visibility audit</strong> — an honest look at where
    you stand right now and exactly what we can improve. No commitment required.</p>

    <p>Would you be open to a quick 15-minute call?</p>

    <p>Best regards,<br>
    <strong>{sender_name}</strong><br>
    <a href="mailto:{sender_email}">{sender_email}</a></p>
  </div>

  <div class="footer">
    You are receiving this message because <strong>{business_name}</strong> is publicly
    listed as a local business. If you would prefer not to hear from us again, simply
    reply with <strong>"Unsubscribe"</strong> and we will remove you immediately —
    no questions asked.
  </div>

</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Plain-text fallback  (shown by clients that block HTML)
# ---------------------------------------------------------------------------
EMAIL_TEMPLATE_TEXT = """\
Hi there,

I came across {business_name} and wanted to reach out about something that could
genuinely help more customers find you online.

We help small and medium businesses grow through three digital visibility services:

--- SEO (Search Engine Optimization) ---
Rank higher on Google so local customers find you first, not your competitors.

--- GEO (Generative Engine Optimization) ---
Get recommended inside AI tools like ChatGPT, Google Gemini, and Perplexity —
where millions of people now search for local services. Most businesses are still
missing this channel.

--- AEO (Answer Engine Optimization) ---
Appear as the direct answer in voice searches (Siri, Alexa) and Google Featured
Snippets — so customers choose you before they ever visit a website.

PRICING
Packages start from $600/month and are fully customised to your goals and budget.
No lock-in contracts.

I'd love to offer {business_name} a complimentary online visibility audit — a clear
picture of where you stand and what we can improve. No commitment required.

Would you be open to a quick 15-minute chat?

Best regards,
{sender_name}
{sender_email}

---
You're receiving this because {business_name} is publicly listed as a local business.
Reply "Unsubscribe" to be removed immediately — no questions asked.
"""


def get_random_subject(business_name: str) -> str:
    return random.choice(SUBJECT_LINES).format(business_name=business_name)
