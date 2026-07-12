"""Main loop.

Every POLL_INTERVAL seconds:
  1. Read new inbox mail -> detect bills -> store, create a calendar event, and
     send an initial "new bill" push.
  2. Read the 'paid' folder -> mark matching invoices paid (stops reminders).
  3. Run the reminder pass -> push about anything due soon or overdue.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from . import config, mail, store as store_mod
from .classify import classify
from .extract import parse_invoice, parse_due_date, pdf_to_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("billwatch")


def _fmt(inv_row) -> str:
    who = (inv_row["sender"] or "").split("<")[0].strip() or inv_row["sender"]
    amount = inv_row["amount"] or "?"
    no = f" #{inv_row['invoice_no']}" if inv_row["invoice_no"] else ""
    return f"{amount} — {who}{no}"


def process_inbox(store: store_mod.Store) -> None:
    last_uid = int(store.get_meta("last_uid", "0") or "0")
    messages, high = mail.fetch_new(last_uid)
    if not messages:
        return
    log.info("Fetched %d new message(s)", len(messages))

    for m in messages:
        if store.seen(m.message_id):
            continue

        pdf_text = "\n".join(pdf_to_text(p) for p in m.pdfs) if m.pdfs else ""
        has_pdf = bool(m.pdfs)

        # Parse due date up front so a labelled date can boost the score.
        combined = f"{m.subject}\n{m.body}\n{pdf_text}"
        due_probe = parse_due_date(pdf_text or combined, m.received, config.DEFAULT_TERM_DAYS)
        due_found = due_probe.source == "label"

        decision = classify(m.subject, m.body, pdf_text, has_pdf, due_found)
        if not decision.is_candidate:
            continue

        inv = parse_invoice(pdf_text or combined, m.received, config.DEFAULT_TERM_DAYS)
        status = "pending" if decision.is_confident else "review"

        cal_uid = None
        summary = f"Pay invoice: {inv.amount or '?'}"
        who = (m.sender or "").split("<")[0].strip()
        desc = (f"From: {who}\nSubject: {m.subject}\n"
                f"Amount: {inv.amount or '?'}\nInvoice no: {inv.invoice_no or '?'}\n"
                f"Due date source: {inv.due_source}\n"
                f"Detected by BillWatch (score {decision.score}).")
        from . import remind
        cal_uid = remind.create_calendar_event(inv.due, summary, desc)

        store.add(
            message_id=m.message_id, uid=m.uid, sender=m.sender, subject=m.subject,
            invoice_no=inv.invoice_no, amount=inv.amount,
            due_date=inv.due.isoformat(), due_source=inv.due_source,
            received_date=m.received.isoformat(), status=status,
            calendar_uid=cal_uid, last_reminded="",
            created_at=datetime.now().isoformat(timespec="seconds"),
        )

        flag = "" if decision.is_confident else " [please check — low confidence]"
        title = f"New bill detected{flag}"
        body = (f"{inv.amount or 'amount ?'} from {who or 'unknown'}\n"
                f"Due {inv.due.isoformat()} ({inv.due_source})\n"
                f"Subject: {m.subject}")
        remind.ntfy(title, body,
                    priority="default" if decision.is_confident else "high",
                    tags=["money_with_wings"])
        log.info("Bill: %s due %s (%s, score %d, %s)",
                 inv.amount, inv.due, inv.due_source, decision.score, status)

    store.set_meta("last_uid", str(high))


def process_paid(store: store_mod.Store) -> None:
    pending = store.pending_message_ids()
    if not pending:
        return
    try:
        paid_ids = mail.paid_folder_message_ids()
    except Exception as e:
        log.warning("could not read paid folder: %s", e)
        return
    for mid in pending & paid_ids:
        store.mark_paid(mid)
        log.info("Marked paid (found in %s): %s", config.IMAP_PAID_FOLDER, mid)


def run_reminders(store: store_mod.Store) -> None:
    from . import remind
    today = date.today()
    today_iso = today.isoformat()
    for inv in store.unpaid():
        try:
            due = date.fromisoformat(inv["due_date"])
        except Exception:
            continue
        effective = due - timedelta(days=config.REMIND_BUFFER_DAYS)
        days = (effective - today).days
        overdue = days < 0
        should = overdue or days in config.REMIND_DAYS
        if not should:
            continue
        if inv["last_reminded"] == today_iso:
            continue  # already pinged today

        if overdue:
            title = f"OVERDUE by {abs(days)}d: {_fmt(inv)}"
            priority, tags = "urgent", ["rotating_light"]
        elif days == 0:
            title = f"DUE TODAY: {_fmt(inv)}"
            priority, tags = "urgent", ["warning"]
        else:
            title = f"Due in {days}d: {_fmt(inv)}"
            priority, tags = "high", ["hourglass"]

        body = (f"Due {inv['due_date']}\nSubject: {inv['subject']}\n"
                f"Move the email to '{config.IMAP_PAID_FOLDER}' once paid to stop reminders.")
        remind.ntfy(title, body, priority=priority, tags=tags)
        store.set_last_reminded(inv["message_id"], today_iso)
        log.info("Reminder sent: %s", title)


def cycle(store: store_mod.Store) -> None:
    try:
        process_inbox(store)
    except Exception as e:
        log.exception("inbox processing error: %s", e)
    try:
        process_paid(store)
    except Exception as e:
        log.exception("paid processing error: %s", e)
    try:
        run_reminders(store)
    except Exception as e:
        log.exception("reminder error: %s", e)


def main() -> None:
    if not config.IMAP_USER or not config.IMAP_PASSWORD:
        raise SystemExit("IMAP_USER and IMAP_PASSWORD (app-specific) are required.")
    store = store_mod.Store(config.DB_PATH)
    log.info("BillWatch started. Polling every %ds.", config.POLL_INTERVAL)
    while True:
        cycle(store)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
