#!/usr/bin/env python3
"""Generate sample invoices for exercising BillWatch end-to-end.

Produces three PDFs, always dated relative to *today* so they stay in the future
every time you run it:

  - dutch    : clear Dutch invoice with a labelled `Vervaldatum` (due in 7 days)
               -> parses as source 'label', fires "New invoice" + "Due in 7d".
  - english  : clear English invoice, "Please pay by <date>" (due in 3 days)
               -> source 'label', fires "New invoice" + "Due in 3d".
  - unclear  : no due date and no payment term at all
               -> parser falls back to received + DEFAULT_TERM_DAYS (source
                  'fallback'), so BillWatch tags it `Needs review`.

By default it writes the PDFs to ./test-invoices/ and prints what BillWatch's own
parser extracts from each. With --send it emails each as a separate message (so
each becomes its own Paperless document), reading SMTP settings from the
environment — no secrets live in this file.

Examples:
  python tools/gen_test_invoices.py
  python tools/gen_test_invoices.py --out /tmp/inv --only dutch,unclear
  SMTP_USER=you@icloud.com SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx \\
    python tools/gen_test_invoices.py --send --to accounting@bubbleform.xyz

Requires:  pip install -r tools/requirements.txt
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage

from fpdf import FPDF
from fpdf.enums import XPos, YPos

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_EN_MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]


def _en_date(d: date) -> str:
    return f"{d.day} {_EN_MONTHS[d.month]} {d.year}"


def _nl_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


@dataclass
class Sample:
    key: str
    vendor: str
    subject: str
    filename: str
    render: "callable"
    expected: str  # human note on what BillWatch should detect


def _pdf() -> FPDF:
    p = FPDF()
    p.add_page()
    return p


def _line(p: FPDF, txt: str, size: int = 11, bold: bool = False, h: int = 7):
    p.set_font("Helvetica", "B" if bold else "", size)
    p.cell(0, h, txt, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _row(p: FPDF, left: str, right: str, bold: bool = False, border: str = ""):
    p.set_font("Helvetica", "B" if bold else "", 11)
    p.cell(120, 8, left, border=border)
    p.cell(0, 8, right, border=border, align="R",
           new_x=XPos.LMARGIN, new_y=YPos.NEXT)


# --- the three documents ------------------------------------------------------
# NB: use "EUR" (not the euro glyph) — the core PDF font is Latin-1; BillWatch's
# amount parser normalises "EUR"/"€" to "€" either way.

def _dutch(today: date, inv_no: str) -> bytes:
    due = today + timedelta(days=7)
    p = _pdf()
    _line(p, "Atelier Noord - Studioverhuur", size=18, bold=True, h=12)
    _line(p, "Distelweg 45, 1031 HA Amsterdam", size=10, h=6)
    _line(p, "KvK 68123456 | BTW NL861234567B01 | info@ateliernoord.nl", size=10, h=6)
    p.ln(6)
    _line(p, "FACTUUR", size=14, bold=True, h=10)
    _line(p, f"Factuurnummer: {inv_no}")
    _line(p, f"Factuurdatum: {_nl_date(today)}")
    _line(p, f"Vervaldatum: {_nl_date(due)}")
    p.ln(4)
    _line(p, "Aan: Bubbleform")
    p.ln(4)
    _row(p, "Omschrijving", "Bedrag", bold=True, border="B")
    _row(p, "Huur studioruimte", "EUR 675,00")
    _row(p, "BTW 21%", "EUR 141,75")
    _row(p, "Totaal te betalen", "EUR 816,75", bold=True, border="T")
    p.ln(8)
    p.set_font("Helvetica", "", 10)
    p.multi_cell(0, 6, "Gelieve het totaalbedrag te betalen voor de vervaldatum.\n"
                       f"IBAN NL00 BANK 0123 4567 89 o.v.v. factuurnummer {inv_no}.")
    return bytes(p.output())


def _english(today: date, inv_no: str) -> bytes:
    due = today + timedelta(days=3)
    p = _pdf()
    _line(p, "Northside Studio Rentals Ltd", size=18, bold=True, h=12)
    _line(p, "12 Canal Street, Manchester M1 1AA", size=10, h=6)
    _line(p, "VAT GB123456789 | billing@northsidestudios.co.uk", size=10, h=6)
    p.ln(6)
    _line(p, "INVOICE", size=14, bold=True, h=10)
    _line(p, f"Invoice number: {inv_no}")
    _line(p, f"Invoice date: {_en_date(today)}")
    _line(p, f"Please pay by {_en_date(due)}")
    p.ln(4)
    _line(p, "Bill to: Bubbleform")
    p.ln(4)
    _row(p, "Description", "Amount", bold=True, border="B")
    _row(p, "Studio hire", "EUR 1,000.00")
    _row(p, "VAT 20%", "EUR 250.00")
    _row(p, "Amount due", "EUR 1,250.00", bold=True, border="T")
    p.ln(8)
    p.set_font("Helvetica", "", 10)
    p.multi_cell(0, 6, "Payment is due by the date shown above.\n"
                       f"Please quote invoice {inv_no} with your payment.")
    return bytes(p.output())


def _unclear(today: date, inv_no: str) -> bytes:
    # No due-date label and no payment term -> parser must fall back (+30 days).
    p = _pdf()
    _line(p, "Corner Supplies", size=18, bold=True, h=12)
    _line(p, "Handwritten-style receipt", size=10, h=6)
    p.ln(6)
    _line(p, "Bonnummer / ref: " + inv_no)
    _line(p, f"Datum: {_nl_date(today)}")
    p.ln(4)
    _row(p, "Diverse materialen", "EUR 45,50", bold=True, border="B")
    p.ln(8)
    p.set_font("Helvetica", "", 10)
    p.multi_cell(0, 6, "Bedankt voor uw aankoop. Betaling wordt gewaardeerd.")
    return bytes(p.output())


def build_samples(today: date) -> list[Sample]:
    yr = today.year
    return [
        Sample("dutch", "Atelier Noord - Studioverhuur",
               f"Factuur {yr}-0788 - Atelier Noord Studioverhuur",
               f"factuur-{yr}-0788-nl.pdf",
               lambda: _dutch(today, f"{yr}-0788"),
               f"due {today + timedelta(days=7)} (label), EUR 816,75"),
        Sample("english", "Northside Studio Rentals Ltd",
               f"Invoice {yr}-INV-0451 - Northside Studio Rentals",
               f"invoice-{yr}-0451-en.pdf",
               lambda: _english(today, f"{yr}-INV-0451"),
               f"due {today + timedelta(days=3)} (label), EUR 1,250.00"),
        Sample("unclear", "Corner Supplies",
               f"Bon {yr}-C-207 - Corner Supplies",
               f"receipt-{yr}-C-207-unclear.pdf",
               lambda: _unclear(today, f"{yr}-C-207"),
               "no due date -> fallback (+30d), tagged Needs review"),
    ]


def _validate(pdf_bytes: bytes, today: date) -> str:
    """Print what BillWatch's own parser detects (needs the runtime deps)."""
    sys.path.insert(0, REPO_ROOT)
    try:
        from billwatch.extract import pdf_to_text, parse_invoice
    except Exception as e:  # pragma: no cover
        return f"(parser import failed: {e})"
    text = pdf_to_text(pdf_bytes)
    if not text.strip():
        return "(no text extracted — install tools/requirements.txt for PyMuPDF)"
    inv = parse_invoice(text, received=today, default_term_days=30)
    return f"due={inv.due} ({inv.due_source})  amount={inv.amount}  no={inv.invoice_no}"


def _send(sample: Sample, pdf_bytes: bytes, to: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    if not user or not password:
        raise SystemExit("SMTP_USER and SMTP_PASSWORD must be set to --send.")

    msg = EmailMessage()
    msg["From"] = f"{sample.vendor} <{user}>"
    msg["To"] = to
    msg["Subject"] = sample.subject
    msg.set_content(f"Test invoice from BillWatch's generator ({sample.key}).\n"
                    f"See attached: {sample.filename}\n")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=sample.filename)

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
        server.login(user, password)
        server.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate BillWatch test invoices.")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "test-invoices"),
                    help="directory to write PDFs to (default ./test-invoices)")
    ap.add_argument("--only", default="",
                    help="comma-separated subset: dutch,english,unclear")
    ap.add_argument("--send", action="store_true",
                    help="email each invoice (SMTP_* env vars required)")
    ap.add_argument("--to", default=os.environ.get("TEST_INVOICE_TO", ""),
                    help="recipient for --send (or set TEST_INVOICE_TO)")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip parsing the PDFs back through billwatch.extract")
    args = ap.parse_args()

    today = date.today()
    samples = build_samples(today)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        samples = [s for s in samples if s.key in wanted]
        if not samples:
            raise SystemExit(f"--only matched nothing; pick from dutch,english,unclear")

    if args.send and not args.to:
        raise SystemExit("--send needs --to (or TEST_INVOICE_TO).")

    os.makedirs(args.out, exist_ok=True)
    for s in samples:
        pdf_bytes = s.render()
        path = os.path.join(args.out, s.filename)
        with open(path, "wb") as f:
            f.write(pdf_bytes)
        line = f"[{s.key:8}] {path}"
        if not args.no_validate:
            line += f"\n           parsed: {_validate(pdf_bytes, today)}"
            line += f"\n           expect: {s.expected}"
        print(line)
        if args.send:
            _send(s, pdf_bytes, args.to)
            print(f"           sent -> {args.to}")


if __name__ == "__main__":
    main()
