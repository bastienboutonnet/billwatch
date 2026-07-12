"""Deliver reminders two ways: an iCloud calendar event (with an alarm) and
escalating ntfy push notifications.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from . import config

log = logging.getLogger("billwatch.remind")


# ---------------------------------------------------------------------------
# ntfy push
# ---------------------------------------------------------------------------

def ntfy(title: str, message: str, priority: str = "default",
         tags: Optional[list[str]] = None, click: Optional[str] = None) -> bool:
    if not config.NTFY_ENABLED or not config.NTFY_TOPIC:
        return False
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    if config.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
    url = f"{config.NTFY_URL}/{config.NTFY_TOPIC}"
    try:
        r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("ntfy send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# SMTP email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> bool:
    """Send a plaintext reminder email via SMTP. No-op (returns False) unless
    EMAIL_ENABLED and a host/recipient are configured."""
    if not config.EMAIL_ENABLED or not config.SMTP_HOST or not config.EMAIL_TO:
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = config.EMAIL_FROM or config.SMTP_USER
    msg["To"] = config.EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if config.SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        with server:
            if config.SMTP_SECURITY == "starttls":
                server.starttls()
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.warning("email send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# iCloud CalDAV
# ---------------------------------------------------------------------------

_calendar = None  # cached principal calendar


def _get_calendar():
    global _calendar
    if _calendar is not None:
        return _calendar
    if not config.CALENDAR_ENABLED:
        return None
    try:
        import caldav
        client = caldav.DAVClient(
            url=config.CALDAV_URL,
            username=config.CALDAV_USER,
            password=config.CALDAV_PASSWORD,
        )
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            log.warning("No CalDAV calendars found for this account")
            return None
        if config.CALENDAR_NAME:
            for c in calendars:
                if (c.name or "").lower() == config.CALENDAR_NAME.lower():
                    _calendar = c
                    break
            else:
                log.warning("Calendar %r not found; using first calendar",
                            config.CALENDAR_NAME)
                _calendar = calendars[0]
        else:
            _calendar = calendars[0]
        log.info("Using calendar: %s", getattr(_calendar, "name", "?"))
    except Exception as e:
        log.warning("CalDAV connect failed: %s", e)
        _calendar = None
    return _calendar


def _ical(uid: str, day: date, summary: str, description: str) -> str:
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dstart = day.strftime("%Y%m%d")
    dend = (day + timedelta(days=1)).strftime("%Y%m%d")
    # Escape ical special chars in text fields.
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//BillWatch//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{dtstamp}\r\n"
        f"DTSTART;VALUE=DATE:{dstart}\r\n"
        f"DTEND;VALUE=DATE:{dend}\r\n"
        f"SUMMARY:{esc(summary)}\r\n"
        f"DESCRIPTION:{esc(description)}\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "TRIGGER:-P1D\r\n"          # alert one day before
        f"DESCRIPTION:{esc(summary)}\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def create_calendar_event(day: date, summary: str, description: str) -> Optional[str]:
    cal = _get_calendar()
    if cal is None:
        return None
    uid = f"billwatch-{uuid.uuid4()}"
    try:
        cal.save_event(_ical(uid, day, summary, description))
        return uid
    except Exception as e:
        log.warning("calendar event failed: %s", e)
        return None
