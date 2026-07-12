"""Paperless-ngx companion entry point.

Alternative to the standalone `billwatch.main` pipeline. Paperless does the heavy
lifting (IMAP fetch, OCR, invoice classification, storage, archive); this loop only
adds the two things Paperless can't: the payment *due* date (parsed from the OCR
text, since Paperless only knows the document date) and escalating reminders until
the invoice is marked Paid.

Every POLL_INTERVAL seconds:
  1. fill_due_dates: for each invoice with no Due-date yet, parse it from the OCR
     content, write it to the Due-date custom field, create one iCloud calendar
     event, and flag low-confidence (fallback) parses with the Needs-review tag +
     a clickable push.
  2. run_reminders: for every unpaid invoice, ping via ntfy on the REMIND_DAYS
     before due and every day once overdue, with a clickable link to the document.

"Mark paid" is a human adding the Paid tag in the Paperless UI; the companion sees
it on the next sweep and stops. Reminders are ntfy + calendar only — Paperless is
never told about them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

from . import config
from .extract import parse_invoice
from .paperless import PaperlessClient, PaperlessDoc
# `remind` (and its `requests` dependency) is imported lazily inside the functions
# that send notifications, matching billwatch.main, so the pure selection logic
# stays importable without the HTTP stack.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("billwatch.companion")

# In-process same-day dedupe used when no durable Last-reminded field is configured.
_reminded_in_process: dict[int, date] = {}


@dataclass
class Reminder:
    doc: PaperlessDoc
    days: int      # days until (buffer-adjusted) due; negative = overdue
    overdue: bool


def select_reminders(
    docs: list[PaperlessDoc],
    today: date,
    remind_days,
    buffer_days: int = 0,
) -> list[Reminder]:
    """Pure selection: which invoices should fire a reminder today.

    Mirrors billwatch.main.run_reminders — ping on each configured days-before-due
    and every day once overdue. Docs without a due date are skipped.
    """
    out: list[Reminder] = []
    for doc in docs:
        if doc.due is None:
            continue
        effective = doc.due - timedelta(days=buffer_days)
        days = (effective - today).days
        overdue = days < 0
        if overdue or days in remind_days:
            out.append(Reminder(doc, days, overdue))
    return out


def _client() -> PaperlessClient:
    return PaperlessClient(
        config.PAPERLESS_URL,
        config.PAPERLESS_TOKEN,
        invoice_doc_type=config.PAPERLESS_INVOICE_DOC_TYPE,
        due_field=config.PAPERLESS_DUE_FIELD,
        paid_tag=config.PAPERLESS_PAID_TAG,
        review_tag=config.PAPERLESS_REVIEW_TAG,
        last_reminded_field=config.PAPERLESS_LAST_REMINDED_FIELD,
        public_base=config.PAPERLESS_PUBLIC_URL,
    )


# ---------------------------------------------------------------------------
# Notification fan-out (each channel is a no-op unless configured)
# ---------------------------------------------------------------------------

def _notify(title: str, body: str, *, priority: str = "default",
            tags=None, click: str | None = None) -> None:
    from . import remind
    remind.ntfy(title, body, priority=priority, tags=tags or [], click=click)
    remind.pushover(title, body, priority=priority, click=click)
    # Email has no click action, so put the link in the body.
    remind.send_email(title, f"{body}\n{click}" if click else body)


# ---------------------------------------------------------------------------
# Step 1: fill the Due-date field on new invoices
# ---------------------------------------------------------------------------

def fill_due_dates(client: PaperlessClient) -> None:
    from . import remind
    docs = client.invoices_missing_due()
    if not docs:
        return
    log.info("%d invoice(s) missing a due date", len(docs))
    for doc in docs:
        received = doc.created or date.today()
        inv = parse_invoice(doc.content, received, config.DEFAULT_TERM_DAYS)
        client.set_due_date(doc, inv.due)
        low_confidence = inv.due_source == "fallback"

        summary = f"Pay invoice: {inv.amount or '?'}"
        desc = (f"{doc.title}\n"
                f"Amount: {inv.amount or '?'}\nInvoice no: {inv.invoice_no or '?'}\n"
                f"Due date source: {inv.due_source}\n"
                f"{client.document_url(doc.id)}")
        remind.create_calendar_event(inv.due, summary, desc)

        click = client.document_url(doc.id)
        if low_confidence:
            client.add_tag(doc, "review_tag")
            subject = "New invoice — please check the due date"
            body = (f"{doc.title}\nGuessed due {inv.due.isoformat()} "
                    f"(no date found, {config.DEFAULT_TERM_DAYS}d fallback).\n"
                    f"Open to correct the Due date.")
            _notify(subject, body, priority="high", tags=["mag"], click=click)
        else:
            subject = "New invoice scheduled"
            body = (f"{doc.title}\nDue {inv.due.isoformat()} ({inv.due_source})\n"
                    f"Amount {inv.amount or '?'}")
            _notify(subject, body, priority="default", tags=["money_with_wings"], click=click)
        log.info("Due date set: doc %s -> %s (%s)", doc.id, inv.due, inv.due_source)


# ---------------------------------------------------------------------------
# Step 2: daily escalating reminders
# ---------------------------------------------------------------------------

def _title(doc: PaperlessDoc, days: int, overdue: bool) -> tuple[str, str, list[str]]:
    label = doc.title
    if overdue:
        return f"OVERDUE by {abs(days)}d: {label}", "urgent", ["rotating_light"]
    if days == 0:
        return f"DUE TODAY: {label}", "urgent", ["warning"]
    return f"Due in {days}d: {label}", "high", ["hourglass"]


def _already_reminded_today(doc: PaperlessDoc, today: date) -> bool:
    if doc.last_reminded == today:          # durable custom field
        return True
    return _reminded_in_process.get(doc.id) == today


def run_reminders(client: PaperlessClient, today: date | None = None) -> None:
    today = today or date.today()
    docs = client.invoices(exclude_paid=True)
    for r in select_reminders(docs, today, config.REMIND_DAYS, config.REMIND_BUFFER_DAYS):
        if _already_reminded_today(r.doc, today):
            continue
        title, priority, tags = _title(r.doc, r.days, r.overdue)
        click = client.document_url(r.doc.id)
        body = (f"Due {r.doc.due.isoformat()}\n"
                f"Add the '{config.PAPERLESS_PAID_TAG}' tag in Paperless once paid "
                f"to stop reminders.")
        _notify(title, body, priority=priority, tags=tags, click=click)
        client.set_last_reminded(r.doc, today)   # no-op if the field isn't configured
        _reminded_in_process[r.doc.id] = today
        log.info("Reminder sent: %s", title)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def cycle(client: PaperlessClient) -> None:
    try:
        fill_due_dates(client)
    except Exception as e:
        log.exception("fill_due_dates error: %s", e)
    try:
        run_reminders(client)
    except Exception as e:
        log.exception("reminder error: %s", e)


def main() -> None:
    if not config.PAPERLESS_URL or not config.PAPERLESS_TOKEN:
        raise SystemExit("PAPERLESS_URL and PAPERLESS_TOKEN are required for the companion.")
    client = _client()
    log.info("BillWatch Paperless companion started. Polling every %ds.", config.POLL_INTERVAL)
    while True:
        cycle(client)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
