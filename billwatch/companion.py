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
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

from html import escape

from . import config
from .extract import parse_invoice, parse_amount, parse_invoice_no
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
        # Only require the ninja-id field to exist when the sync is on.
        ninja_id_field=(config.PAPERLESS_NINJA_ID_FIELD
                        if config.INVOICE_NINJA_ENABLED else ""),
        public_base=config.PAPERLESS_PUBLIC_URL,
    )


# ---------------------------------------------------------------------------
# Notification fan-out (each channel is a no-op unless configured)
# ---------------------------------------------------------------------------

def _notify(title: str, push_body: str, *, priority: str = "default",
            tags=None, click: str | None = None,
            email_text: str | None = None, email_html: str | None = None) -> None:
    """Fan out one alert. Pushes stay short (push_body); email gets the richer
    text/HTML when provided, else falls back to push_body."""
    from . import remind
    remind.ntfy(title, push_body, priority=priority, tags=tags or [], click=click)
    remind.pushover(title, push_body, priority=priority, click=click)
    text = email_text if email_text is not None else (
        f"{push_body}\n{click}" if click else push_body)
    remind.send_email(title, text, html_body=email_html)


_DUE_SOURCE_TEXT = {
    "label": "labelled date in the document",
    "term": "invoice date + payment term",
    "fallback": "guessed — no due date found",
}


def _render_email(headline: str, accent: str, subtitle: str,
                  rows: list[tuple[str, str]], note: str, url: str,
                  url_label: str = "Open in Paperless") -> tuple[str, str]:
    """Build (plaintext, html) bodies. Plaintext is the always-works fallback."""
    shown = [(lbl, val) for lbl, val in rows if val]

    text_lines = [headline]
    if subtitle:
        text_lines += ["", subtitle]
    text_lines.append("")
    text_lines += [f"{lbl}: {val}" for lbl, val in shown]
    if note:
        text_lines += ["", note]
    if url:
        text_lines += ["", url]
    text = "\n".join(text_lines)

    row_html = "".join(
        f'<tr><td style="padding:6px 20px;color:#6b7280;font-size:13px;'
        f'white-space:nowrap;vertical-align:top;">{escape(lbl)}</td>'
        f'<td style="padding:6px 20px;color:#111827;font-size:14px;'
        f'font-weight:600;text-align:right;">{escape(val)}</td></tr>'
        for lbl, val in shown
    )
    sub_html = (f'<div style="color:#6b7280;font-size:14px;margin-top:4px;">'
                f'{escape(subtitle)}</div>') if subtitle else ""
    button = (f'<a href="{escape(url, quote=True)}" style="display:inline-block;'
              f'background:{accent};color:#ffffff;text-decoration:none;'
              f'padding:11px 20px;border-radius:8px;font-size:14px;'
              f'font-weight:600;">{escape(url_label)}</a>') if url else ""
    note_html = (f'<div style="color:#9ca3af;font-size:12px;margin-top:16px;'
                 f'line-height:1.5;">{escape(note)}</div>') if note else ""
    html = f"""\
<div style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
    <div style="height:6px;background:{accent};"></div>
    <div style="padding:20px 20px 4px 20px;">
      <div style="font-size:18px;font-weight:700;color:#111827;">{escape(headline)}</div>
      {sub_html}
    </div>
    <table style="width:100%;border-collapse:collapse;margin:12px 0 4px 0;">{row_html}</table>
    <div style="padding:12px 20px 22px 20px;">
      {button}
      {note_html}
    </div>
  </div>
  <div style="max-width:480px;margin:10px auto 0 auto;color:#9ca3af;font-size:11px;text-align:center;">Sent by BillWatch</div>
</div>"""
    return text, html


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
        rows = [
            ("From", doc.title),
            ("Amount", inv.amount or ""),
            ("Invoice no", inv.invoice_no or ""),
            ("Invoice date", doc.created.isoformat() if doc.created else ""),
            ("Due date", inv.due.isoformat()),
            ("Due date via", _DUE_SOURCE_TEXT.get(inv.due_source, inv.due_source)),
        ]
        if low_confidence:
            client.add_tag(doc, "review_tag")
            subject = "New invoice — please check the due date"
            push_body = (f"{doc.title}\nGuessed due {inv.due.isoformat()} "
                         f"(no date found, {config.DEFAULT_TERM_DAYS}d fallback).")
            subtitle = f"No due date found — guessed +{config.DEFAULT_TERM_DAYS} days."
            note = "Tagged 'Needs review'. Open it in Paperless to correct the Due date."
            text, html = _render_email(subject, "#d97706", subtitle, rows, note, click)
            _notify(subject, push_body, priority="high", tags=["mag"], click=click,
                    email_text=text, email_html=html)
        else:
            subject = "New invoice scheduled"
            push_body = (f"{doc.title}\nDue {inv.due.isoformat()} ({inv.due_source})\n"
                         f"Amount {inv.amount or '?'}")
            subtitle = "Due date read from the document."
            note = ("You'll be reminded as the due date approaches. "
                    "Add the 'Paid' tag in Paperless once paid.")
            text, html = _render_email(subject, "#16a34a", subtitle, rows, note, click)
            _notify(subject, push_body, priority="default", tags=["money_with_wings"],
                    click=click, email_text=text, email_html=html)
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
        if r.overdue:
            when, accent = f"Overdue by {abs(r.days)} day(s)", "#dc2626"
        elif r.days == 0:
            when, accent = "Due today", "#ea580c"
        else:
            when, accent = f"Due in {r.days} day(s)", "#2563eb"
        # Amount / invoice no aren't stored in Paperless; re-read from the OCR text.
        rows = [
            ("From", r.doc.title),
            ("Amount", parse_amount(r.doc.content) or ""),
            ("Invoice no", parse_invoice_no(r.doc.content) or ""),
            ("Due date", r.doc.due.isoformat()),
            ("Status", when),
        ]
        push_body = (f"Due {r.doc.due.isoformat()}\n"
                     f"Add the '{config.PAPERLESS_PAID_TAG}' tag in Paperless once paid "
                     f"to stop reminders.")
        note = (f"Add the '{config.PAPERLESS_PAID_TAG}' tag in Paperless once paid "
                f"to stop these reminders.")
        text, html = _render_email(title, accent, when, rows, note, click)
        _notify(title, push_body, priority=priority, tags=tags, click=click,
                email_text=text, email_html=html)
        client.set_last_reminded(r.doc, today)   # no-op if the field isn't configured
        _reminded_in_process[r.doc.id] = today
        log.info("Reminder sent: %s", title)


# ---------------------------------------------------------------------------
# Step 3 (optional): sync invoices into Invoice Ninja as Expenses
# ---------------------------------------------------------------------------

def _amount_to_float(amount: str | None) -> float | None:
    """'€816,75' / '€1,250.00' -> float. Decimal separator = the rightmost . or ,."""
    if not amount:
        return None
    s = re.sub(r"[^\d.,]", "", amount)
    if not s:
        return None
    dec = max(s.rfind("."), s.rfind(","))
    if dec == -1:
        try:
            return float(s)
        except ValueError:
            return None
    int_part = re.sub(r"[.,]", "", s[:dec])
    frac = re.sub(r"[^\d]", "", s[dec + 1:])
    try:
        return float(f"{int_part or '0'}.{frac or '0'}")
    except ValueError:
        return None


def _ninja_action(pushed: bool, needs_review: bool, paid: bool, has_due: bool) -> str | None:
    """Pure decision for the Invoice Ninja sync.

    - not yet pushed, confident (no Needs-review tag) and due date filled -> create
      (this also covers a low-confidence invoice once the human clears the tag)
    - already pushed and the Paid tag was added -> mark_paid
    """
    if not pushed:
        return "create" if (has_due and not needs_review) else None
    return "mark_paid" if paid else None


def sync_invoice_ninja(client: PaperlessClient, ninja) -> None:
    from .invoiceninja import InvoiceNinjaError
    for doc in client.invoices():
        action = _ninja_action(
            pushed=bool(doc.ninja_id),
            needs_review=client.has_tag(doc, "review_tag"),
            paid=client.has_tag(doc, "paid_tag"),
            has_due=doc.due is not None,
        )
        if action == "create":
            amount = _amount_to_float(parse_amount(doc.content))
            if amount is None:
                log.info("IN: skipping doc %s — no amount parsed", doc.id)
                continue
            vendor = doc.correspondent or doc.title or "Unknown vendor"
            invoice_no = parse_invoice_no(doc.content) or ""
            expense_date = (doc.created or date.today()).isoformat()
            notes = (f"Imported by BillWatch\nInvoice: {invoice_no}\n"
                     f"Due: {doc.due}\n{client.document_url(doc.id)}")
            try:
                vendor_id = ninja.find_or_create_vendor(vendor)
                eid = ninja.create_expense(vendor_id=vendor_id, amount=amount,
                                           date=expense_date, public_notes=notes)
                client.set_ninja_id(doc, eid)
                log.info("IN: created expense %s for doc %s (%s, %.2f)",
                         eid, doc.id, vendor, amount)
            except InvoiceNinjaError as e:
                log.warning("IN: create failed for doc %s: %s", doc.id, e)
        elif action == "mark_paid":
            try:
                if not ninja.is_expense_paid(doc.ninja_id):
                    ninja.mark_expense_paid(doc.ninja_id, date.today().isoformat())
                    log.info("IN: marked expense %s paid (doc %s)", doc.ninja_id, doc.id)
            except InvoiceNinjaError as e:
                log.warning("IN: mark-paid failed for doc %s: %s", doc.id, e)


def _ninja_client():
    from .invoiceninja import InvoiceNinjaClient
    return InvoiceNinjaClient(config.INVOICE_NINJA_URL, config.INVOICE_NINJA_TOKEN)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def cycle(client: PaperlessClient, ninja=None) -> None:
    try:
        fill_due_dates(client)
    except Exception as e:
        log.exception("fill_due_dates error: %s", e)
    try:
        run_reminders(client)
    except Exception as e:
        log.exception("reminder error: %s", e)
    if ninja is not None:
        try:
            sync_invoice_ninja(client, ninja)
        except Exception as e:
            log.exception("invoice ninja sync error: %s", e)


def main() -> None:
    if not config.PAPERLESS_URL or not config.PAPERLESS_TOKEN:
        raise SystemExit("PAPERLESS_URL and PAPERLESS_TOKEN are required for the companion.")
    client = _client()
    ninja = _ninja_client() if config.INVOICE_NINJA_ENABLED else None
    if ninja is not None:
        log.info("Invoice Ninja expense sync enabled.")
    log.info("BillWatch Paperless companion started. Polling every %ds.", config.POLL_INTERVAL)
    while True:
        cycle(client, ninja)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
