"""
email_sender.py – Send outreach emails via Gmail SMTP (SSL, port 465).

Uses a Gmail App Password (NOT your main Gmail password).
Generate one at: myaccount.google.com → Security → App Passwords
"""
import random
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

from templates import EMAIL_TEMPLATE_HTML, EMAIL_TEMPLATE_TEXT, get_random_subject


# ---------------------------------------------------------------------------
# Single email
# ---------------------------------------------------------------------------

def send_email(
    sender_email: str,
    app_password: str,
    recipient_email: str,
    business_name: str,
    sender_name: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> tuple:
    """
    Send one email.
    Returns (success: bool, message: str).
    """
    try:
        subject = get_random_subject(business_name)

        html_body = EMAIL_TEMPLATE_HTML.format(
            business_name=business_name,
            sender_name=sender_name,
            sender_email=sender_email,
        )
        text_body = EMAIL_TEMPLATE_TEXT.format(
            business_name=business_name,
            sender_name=sender_name,
            sender_email=sender_email,
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"]  = subject
        msg["From"]     = f"{sender_name} <{sender_email}>"
        msg["To"]       = recipient_email
        msg["Reply-To"] = sender_email

        # Plain text first, HTML second (preferred by clients)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html",  "utf-8"))

        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, [recipient_email], msg.as_string())

        return True, "Sent successfully"

    except smtplib.SMTPAuthenticationError:
        return False, ("Authentication failed. Check your Gmail address and "
                       "App Password (not your main password).")
    except smtplib.SMTPRecipientsRefused:
        return False, f"Recipient refused: {recipient_email}"
    except smtplib.SMTPException as exc:
        return False, f"SMTP error: {exc}"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Batch sender
# ---------------------------------------------------------------------------

def send_batch(
    leads_df,
    sender_email: str,
    app_password: str,
    sender_name: str,
    delay_seconds: int = 45,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
    log_cb: Optional[Callable] = None,
    update_status_cb: Optional[Callable] = None,
) -> dict:
    """
    Send emails to all rows in leads_df.
    Adds a randomised delay between sends to improve deliverability.

    Returns stats dict: {"sent": int, "failed": int, "errors": list[str]}
    """
    stats: dict = {"sent": 0, "failed": 0, "errors": []}
    rows = list(leads_df.iterrows())
    total = len(rows)

    for i, (_, row) in enumerate(rows):
        biz_name = row.get("business_name", "your business")
        recipient = row.get("email", "")

        ok, msg = send_email(
            sender_email=sender_email,
            app_password=app_password,
            recipient_email=recipient,
            business_name=biz_name,
            sender_name=sender_name,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )

        lead_id = int(row["id"])

        if ok:
            stats["sent"] += 1
            if update_status_cb:
                update_status_cb(lead_id, "sent")
            if log_cb:
                log_cb(f"✅ [{i + 1}/{total}] Sent → {biz_name} <{recipient}>")
        else:
            stats["failed"] += 1
            stats["errors"].append(f"{recipient}: {msg}")
            if update_status_cb:
                update_status_cb(lead_id, "failed")
            if log_cb:
                log_cb(f"❌ [{i + 1}/{total}] Failed → {biz_name}: {msg}")

        # Wait between emails (skip after the last one)
        if i < total - 1:
            jitter = random.randint(-8, 8)
            wait = max(10, delay_seconds + jitter)
            if log_cb:
                log_cb(f"   ⏱️  Waiting {wait}s …")
            time.sleep(wait)

    return stats
