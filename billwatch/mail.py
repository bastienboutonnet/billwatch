"""IMAP access to iCloud: pull new inbox mail, and see what's in the 'paid' folder.

We never mark your mail as read and never delete anything. New messages are
tracked by a UID high-water mark plus their Message-ID, so nothing is processed
twice and your inbox is left untouched.
"""
from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Optional

from . import config


@dataclass
class Message:
    uid: int
    message_id: str
    sender: str
    subject: str
    body: str
    received: date
    pdfs: list[bytes] = field(default_factory=list)


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
    return conn


def _extract(raw: bytes, uid: int) -> Message:
    msg = email.message_from_bytes(raw)
    subject = _decode(msg.get("Subject"))
    sender = _decode(msg.get("From"))
    message_id = (msg.get("Message-ID") or f"uid-{uid}").strip()

    try:
        received = parsedate_to_datetime(msg.get("Date")).date()
    except Exception:
        received = datetime.now().date()

    body_parts: list[str] = []
    pdfs: list[bytes] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        filename = _decode(part.get_filename())
        if ctype == "application/pdf" or (filename or "").lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                pdfs.append(payload)
        elif ctype == "text/plain" and "attachment" not in disp:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_parts.append(payload.decode(charset, errors="replace"))
                except LookupError:
                    body_parts.append(payload.decode("utf-8", errors="replace"))

    return Message(
        uid=uid,
        message_id=message_id,
        sender=sender,
        subject=subject,
        body="\n".join(body_parts),
        received=received,
        pdfs=pdfs,
    )


def fetch_new(last_uid: int) -> tuple[list[Message], int]:
    """Return (new messages, highest UID seen). Messages with UID > last_uid only."""
    conn = _connect()
    messages: list[Message] = []
    high = last_uid
    try:
        conn.select(f'"{config.IMAP_INBOX}"', readonly=True)
        typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return [], last_uid
        uids = [int(x) for x in data[0].split()]
        # IMAP's `n:*` returns the last message even when none are newer — filter it.
        uids = [u for u in uids if u > last_uid]
        for uid in uids:
            typ, fetched = conn.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            messages.append(_extract(raw, uid))
            high = max(high, uid)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return messages, high


def paid_folder_message_ids(limit: int = 300) -> set[str]:
    """Message-IDs currently sitting in the 'paid' mailbox."""
    ids: set[str] = set()
    conn = _connect()
    try:
        typ, _ = conn.select(f'"{config.IMAP_PAID_FOLDER}"', readonly=True)
        if typ != "OK":
            return ids
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return ids
        uids = data[0].split()[-limit:]
        if not uids:
            return ids
        uid_set = b",".join(uids).decode()
        typ, fetched = conn.uid("fetch", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ == "OK":
            for item in fetched:
                if isinstance(item, tuple) and item[1]:
                    header = item[1].decode(errors="replace")
                    for line in header.splitlines():
                        if line.lower().startswith("message-id:"):
                            ids.add(line.split(":", 1)[1].strip())
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return ids
