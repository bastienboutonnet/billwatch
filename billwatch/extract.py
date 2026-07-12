"""Extract structured invoice data (due date, amount, invoice number) from PDF text.

The senders vary, so we never rely on a template. We read the PDF text and look
for labelled fields, in Dutch first (factuur/vervaldatum/...) then English.
Everything degrades gracefully: if a field is missing we return None and the
caller falls back (e.g. received_date + DEFAULT_TERM_DAYS for the due date).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def pdf_to_text(data: bytes) -> str:
    """Return the text content of a PDF given as raw bytes. Empty string on failure."""
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception:
        text = ""
    if text.strip():
        return text
    # Fallback engine for PDFs pdfplumber chokes on.
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_NL_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
    # common abbreviations
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dec": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "august": 8, "october": 10, "december": 12,
    "mar": 3, "oct": 10,
}
_MONTHS = {**_EN_MONTHS, **_NL_MONTHS}

# Labels that introduce a due date, most specific first.
_DUE_LABELS = [
    "uiterste betaaldatum", "uiterste betaal datum", "vervaldatum", "verval datum",
    "vervaldag", "te betalen voor", "te betalen vóór", "gelieve te betalen voor",
    "betalen voor", "betaal voor", "betaaldatum", "vervalt op", "expiratiedatum",
    "due date", "payment due", "amount due by", "pay before", "please pay by",
    "due by", "date due",
]
_INVOICE_DATE_LABELS = [
    "factuurdatum", "factuur datum", "datum factuur", "invoice date",
    "date of invoice", "datum",
]

_D = r"(\d{1,2})"
_M = r"(\d{1,2})"
_Y = r"(\d{2,4})"
# 15-08-2026 / 15/08/2026 / 15.08.2026
_NUM_DATE = re.compile(rf"\b{_D}[\-/.]{_M}[\-/.]{_Y}\b")
# 2026-08-15
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# 15 augustus 2026  /  15th August 2026
_TXT_DATE = re.compile(
    r"\b(\d{1,2})(?:e|st|nd|rd|th)?\.?\s+([A-Za-zÀ-ÿ]{3,10})\.?\s+(\d{4})\b",
    re.IGNORECASE,
)


def _mk_date(y: int, m: int, d: int) -> Optional[date]:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _first_date_in(snippet: str) -> Optional[date]:
    """Find the first parseable date in a short text snippet (European day-first)."""
    m = _ISO_DATE.search(snippet)
    if m:
        return _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _TXT_DATE.search(snippet)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return _mk_date(int(m.group(3)), month, int(m.group(1)))
    m = _NUM_DATE.search(snippet)
    if m:
        return _mk_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def _labelled_date(text: str, labels: list[str], window: int = 45) -> Optional[date]:
    low = text.lower()
    for label in labels:
        start = 0
        while True:
            idx = low.find(label, start)
            if idx == -1:
                break
            snippet = text[idx: idx + len(label) + window]
            found = _first_date_in(snippet)
            if found:
                return found
            start = idx + len(label)
    return None


_TERM_RE = re.compile(
    r"(?:betalingstermijn|betaaltermijn|binnen|payment\s+term|term\s+of\s+payment|net)\D{0,12}?(\d{1,3})\s*(?:dagen|days|day)?",
    re.IGNORECASE,
)


@dataclass
class DueResult:
    due: Optional[date]
    source: str  # 'label' | 'term' | 'fallback' | 'none'


def parse_due_date(text: str, received: date, default_term_days: int = 30) -> DueResult:
    """Determine the payment due date from invoice text.

    Order of preference:
      1. An explicitly labelled due date (vervaldatum / due date / ...).
      2. Invoice date + payment term ("betalingstermijn 30 dagen").
      3. received + default_term_days.
    """
    d = _labelled_date(text, _DUE_LABELS)
    if d:
        return DueResult(d, "label")

    term_m = _TERM_RE.search(text)
    if term_m:
        term = int(term_m.group(1))
        inv_date = _labelled_date(text, _INVOICE_DATE_LABELS) or received
        return DueResult(inv_date + timedelta(days=term), "term")

    return DueResult(received + timedelta(days=default_term_days), "fallback")


# ---------------------------------------------------------------------------
# Amount + invoice number
# ---------------------------------------------------------------------------

_AMOUNT_LABELS = [
    "totaal te betalen", "totaalbedrag", "te betalen", "totaal incl", "totaal",
    "amount due", "total due", "balance due", "grand total", "total",
]
# €1.234,56  |  € 1,234.56  |  1234,56 EUR
# Capture the whole number token (either thousands convention) ending in a 2-digit
# cents group, so "1,250.00" isn't truncated to "1,25".
_AMOUNT_RE = re.compile(
    r"(?:€|eur)\s*(\d[\d.,\s]{0,12}[.,]\d{2})|(\d[\d.,\s]{0,12}[.,]\d{2})\s*(?:€|eur)",
    re.IGNORECASE,
)


def parse_amount(text: str) -> Optional[str]:
    low = text.lower()
    for label in _AMOUNT_LABELS:
        idx = low.find(label)
        if idx != -1:
            m = _AMOUNT_RE.search(text[idx: idx + len(label) + 40])
            if m:
                return "€" + (m.group(1) or m.group(2)).strip()
    m = _AMOUNT_RE.search(text)  # any currency amount as a last resort
    if m:
        return "€" + (m.group(1) or m.group(2)).strip()
    return None


_INV_NO_RE = re.compile(
    r"(?:factuurnummer|factuurnr\.?|factuur\s*#|invoice\s*(?:number|no\.?|#))\s*[:.#]?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{2,19})",
    re.IGNORECASE,
)


def parse_invoice_no(text: str) -> Optional[str]:
    m = _INV_NO_RE.search(text)
    return m.group(1).strip() if m else None


@dataclass
class Invoice:
    due: date
    due_source: str
    amount: Optional[str]
    invoice_no: Optional[str]


def parse_invoice(text: str, received: date, default_term_days: int = 30) -> Invoice:
    due = parse_due_date(text, received, default_term_days)
    return Invoice(
        due=due.due,
        due_source=due.source,
        amount=parse_amount(text),
        invoice_no=parse_invoice_no(text),
    )
