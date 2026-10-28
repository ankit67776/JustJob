"""Optional delivery of the digest by Telegram and/or email."""
from __future__ import annotations

import html
import logging
import os
import smtplib
from email.message import EmailMessage

from .digest import Digest, to_html
from .net import post_json
from .web import experience_label

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4000   # Telegram's hard limit is 4096 characters per message


def _telegram_lines(d: Digest, n: int) -> list[str]:
    lines = [f"<b>Job digest {d.generated_at[:10]}</b> · {len(d.new)} new, {d.total_open} open"]
    for r in d.new[:n]:
        lines.append(
            f'\n<b>{r["score"]}</b> · <a href="{html.escape(r["url"])}">{html.escape(r["title"])}</a>\n'
            f'{html.escape(r["company"])} · {html.escape((r["location"] or "")[:40])} · {experience_label(r)}'
        )
    if not d.new:
        lines.append("\nNo new matches today.")
    return lines


def send_telegram(d: Digest, n: int) -> str:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return "skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    chunks, current = [], ""
    for line in _telegram_lines(d, n):
        if len(current) + len(line) > TELEGRAM_LIMIT:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for text in chunks:
        post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    return f"sent {len(chunks)} message(s)"


def send_email(d: Digest) -> str:
    env = {k: os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")}
    if not all(env[k] for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")):
        return "skipped: SMTP_* / EMAIL_TO not set"
    msg = EmailMessage()
    msg["Subject"] = f"Job digest {d.generated_at[:10]}: {len(d.new)} new matches"
    msg["From"] = env["SMTP_USER"]
    msg["To"] = env["EMAIL_TO"]
    msg.set_content(f"{len(d.new)} new matches. Open the HTML version of this email to see them.")
    msg.add_alternative(to_html(d), subtype="html")
    port = int(env["SMTP_PORT"] or 587)
    with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as s:
        s.starttls()
        s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        s.send_message(msg)
    return "sent"


def send(cfg: dict, d: Digest) -> dict:
    n = int(cfg["notify"].get("max_items", 15))
    out = {}
    for name, enabled, fn in (
        ("telegram", cfg["notify"].get("telegram"), lambda: send_telegram(d, n)),
        ("email", cfg["notify"].get("email"), lambda: send_email(d)),
    ):
        if not enabled:
            continue
        try:
            out[name] = fn()
        except Exception as e:   # a failed notification shouldn't fail the run
            log.warning("notify %s failed: %s", name, e)
            out[name] = f"failed: {e}"
    return out
