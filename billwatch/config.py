"""All configuration comes from environment variables (see .env.example)."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _int_list(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return out or default


def _list(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


# --- iCloud IMAP (needs an app-specific password; your normal password won't work) ---
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.mail.me.com")
IMAP_PORT = _int("IMAP_PORT", 993)
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_INBOX = os.environ.get("IMAP_INBOX", "INBOX")
# Move a paid invoice's email into this mailbox and BillWatch stops reminding you.
IMAP_PAID_FOLDER = os.environ.get("IMAP_PAID_FOLDER", "Betaald")

# --- iCloud CalDAV (same Apple ID + app-specific password) ---
CALENDAR_ENABLED = os.environ.get("CALENDAR_ENABLED", "true").lower() in ("1", "true", "yes", "on")
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com")
CALDAV_USER = os.environ.get("CALDAV_USER", IMAP_USER)
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", IMAP_PASSWORD)
CALENDAR_NAME = os.environ.get("CALENDAR_NAME", "")  # empty = first writable calendar

# --- ntfy push (self-host ntfy or use ntfy.sh) ---
NTFY_ENABLED = os.environ.get("NTFY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")  # optional bearer token for protected topics

# --- Pushover push (private by design; alternative/addition to ntfy) ---
PUSHOVER_ENABLED = os.environ.get("PUSHOVER_ENABLED", "false").lower() in ("1", "true", "yes", "on")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")  # application API token
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")    # your user or group key
PUSHOVER_DEVICE = os.environ.get("PUSHOVER_DEVICE", "")  # optional: limit to one device

# --- Email reminders (SMTP) ---
EMAIL_ENABLED = os.environ.get("EMAIL_ENABLED", "false").lower() in ("1", "true", "yes", "on")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
# 'starttls' (587), 'ssl' (465), or 'none'.
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").lower()
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")  # iCloud/Gmail: app-specific password
# From defaults to SMTP_USER; To may be a comma-separated list.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip() or SMTP_USER
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# --- Paperless-ngx companion (alternative to the standalone IMAP pipeline) ---
# When these are set, `python -m billwatch.companion` talks to Paperless instead
# of iCloud IMAP: Paperless owns capture/OCR/classification/storage, the companion
# only fills the Due-date field and escalates reminders. See docs/HANDOFF.md.
PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "").rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
# Phone-reachable base for clickable notification links (Tailscale MagicDNS /
# Cloudflare-tunnel hostname, NOT a LAN IP). Falls back to PAPERLESS_URL.
PAPERLESS_PUBLIC_URL = os.environ.get("PAPERLESS_PUBLIC_URL", "").rstrip("/") or PAPERLESS_URL
# The shared vocabulary configured once in the Paperless UI:
PAPERLESS_INVOICE_DOC_TYPE = os.environ.get("PAPERLESS_INVOICE_DOC_TYPE", "Invoice")
PAPERLESS_DUE_FIELD = os.environ.get("PAPERLESS_DUE_FIELD", "Due date")  # date custom field
PAPERLESS_PAID_TAG = os.environ.get("PAPERLESS_PAID_TAG", "Paid")
PAPERLESS_REVIEW_TAG = os.environ.get("PAPERLESS_REVIEW_TAG", "Needs review")
# Optional date custom field for durable same-day reminder dedupe across restarts.
# Leave blank to dedupe in-process only (fine for a once-daily run).
PAPERLESS_LAST_REMINDED_FIELD = os.environ.get("PAPERLESS_LAST_REMINDED_FIELD", "")
# Text custom field storing the Invoice Ninja expense id (idempotency +
# paid-sync target). Only needed when the Invoice Ninja sync is enabled.
PAPERLESS_NINJA_ID_FIELD = os.environ.get("PAPERLESS_NINJA_ID_FIELD", "Invoice Ninja id")
# Text custom field for the invoice amount (e.g. "$12.72"). BillWatch fills it
# from OCR when it can; you can correct it. Read back for the IN expense.
PAPERLESS_AMOUNT_FIELD = os.environ.get("PAPERLESS_AMOUNT_FIELD", "Amount")

# --- Invoice Ninja expense sync (optional) ---
# Push confident/reviewed invoices into Invoice Ninja as Expenses, and mark the
# expense paid once the Paperless `Paid` tag is added. One-directional; Paperless
# remains the source of truth for reminders.
INVOICE_NINJA_ENABLED = os.environ.get("INVOICE_NINJA_ENABLED", "false").lower() in ("1", "true", "yes", "on")
INVOICE_NINJA_URL = os.environ.get("INVOICE_NINJA_URL", "").rstrip("/")
INVOICE_NINJA_TOKEN = os.environ.get("INVOICE_NINJA_TOKEN", "")
# Your Invoice Ninja company's base currency. Foreign-currency expenses get an
# exchange_rate to this, fetched (ECB, keyless) for the expense/payment date.
INVOICE_NINJA_BASE_CURRENCY = os.environ.get("INVOICE_NINJA_BASE_CURRENCY", "EUR").upper()

# --- Behaviour ---
POLL_INTERVAL = _int("POLL_INTERVAL", 900)          # seconds between inbox polls
DEFAULT_TERM_DAYS = _int("DEFAULT_TERM_DAYS", 30)   # fallback due date = received + this
REMIND_DAYS = _int_list("REMIND_DAYS", [7, 3, 1, 0])  # days-before-due to ping on
REMIND_BUFFER_DAYS = _int("REMIND_BUFFER_DAYS", 0)  # treat due date as (due - buffer)
DB_PATH = os.environ.get("DB_PATH", "/data/billwatch.db")
TZ = os.environ.get("TZ", "Europe/Amsterdam")

# Detection thresholds (see classify.py for how scores are computed).
CANDIDATE_MIN_SCORE = _int("CANDIDATE_MIN_SCORE", 2)  # below this = ignored
CONFIDENT_MIN_SCORE = _int("CONFIDENT_MIN_SCORE", 4)  # at/above = auto-schedule, else 'review'

BILL_KEYWORDS = _list("BILL_KEYWORDS", [
    "invoice", "factuur", "rekening", "vervaldatum", "te betalen", "betalen",
    "betaling", "bedrag", "totaal", "amount due", "payment", "iban", "btw",
    "nota", "bill", "due",
])
