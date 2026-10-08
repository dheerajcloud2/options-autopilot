"""
Sends an email alert when a new qualifying contract shows up in the
dashboard. Uses plain SMTP (with STARTTLS) -- works with Gmail, Outlook,
or most providers if you use an app password.
"""
import smtplib
from email.mime.text import MIMEText

import config


def email_configured() -> bool:
    return bool(
        config.EMAIL_ENABLED
        and config.SMTP_HOST
        and config.SMTP_USER
        and config.SMTP_PASSWORD
        and config.EMAIL_TO
    )


def send_email(subject: str, body: str):
    if not config.EMAIL_ENABLED:
        return
    if not email_configured():
        print("EMAIL_ENABLED is true but SMTP settings are incomplete in .env -- skipping email.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM or config.SMTP_USER
    msg["To"] = config.EMAIL_TO

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(msg["From"], [config.EMAIL_TO], msg.as_string())
    except Exception as e:
        print(f"Failed to send email alert: {e}")


def format_entry_alert(tab_label: str, contracts: list) -> tuple[str, str]:
    """Build a subject + body for one or more newly-qualifying contracts."""
    if len(contracts) == 1:
        c = contracts[0]
        subject = f"[Options Scanner] New entry: {c['underlying']} {c['put_call']} ${c['strike']} ({tab_label})"
    else:
        subject = f"[Options Scanner] {len(contracts)} new entries ({tab_label})"

    lines = [f"New qualifying contract(s) on the \"{tab_label}\" tab:\n"]
    for c in contracts:
        lines.append(
            f"- {c['underlying']} {c['put_call']} ${c['strike']} exp {c['expiration']} "
            f"({c['option_symbol']})\n"
            f"  current: ${c['current_price']}  volume: {c['volume']}  "
            f"spread: {c.get('spread_pct')}%"
        )
    body = "\n".join(lines)
    return subject, body


def format_membership_alert(tab_label: str, added: list, removed: list) -> tuple[str, str]:
    """Build an alert for contracts entering or leaving a dashboard table."""
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if removed:
        parts.append(f"{len(removed)} removed")
    subject = f"[Options Scanner] {tab_label}: {', '.join(parts)}"
    lines = [f"Updates to the \"{tab_label}\" table:\n"]
    if added:
        lines.append("Added:")
        for c in added:
            lines.append(
                f"- {c['underlying']} {c['put_call']} ${c['strike']} exp {c['expiration']} "
                f"({c['option_symbol']}) change: {c.get('option_pct_since_crossover')}%"
            )
    if removed:
        lines.append("Removed:")
        for c in removed:
            lines.append(
                f"- {c['underlying']} {c['put_call']} ${c['strike']} exp {c['expiration']} "
                f"({c['option_symbol']})"
            )
    return subject, "\n".join(lines)
