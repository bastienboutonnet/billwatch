# BillWatch

A small self-hosted service that watches your iCloud inbox for studio invoices
and makes sure you pay them on time. It runs 24/7 on your home server — so
unlike Apple Mail's *Remind Me*, it doesn't depend on your phone or Mac being
awake, and you don't have to tag anything by hand.

## What it does

On a loop (every 15 min by default) it:

1. **Reads new inbox mail** over IMAP (read-only — nothing is marked read or
   deleted).
2. **Detects bills by content**, not sender: a PDF attachment plus invoice
   keywords (Dutch first — *factuur, vervaldatum, te betalen, IBAN, BTW* — then
   English). Since your senders vary, anything that looks bill-ish but is
   borderline is flagged *"please check"* rather than dropped, so you never miss
   one.
3. **Extracts the due date from the PDF** — a labelled *vervaldatum* / *te
   betalen voor* / *due date* wins; otherwise *factuurdatum + betalingstermijn*;
   otherwise *received + 30 days* as a safe fallback. It also pulls the amount
   and invoice number.
4. **Schedules two reminders**: an all-day **iCloud calendar event** on the due
   date (with a 1-day-before alarm), and **ntfy push notifications** that
   escalate — 7/3/1/0 days before, then every day once overdue.
5. **Closes the loop**: drag the bill's email into a *Betaald* (paid) mailbox
   and BillWatch marks it paid and stops reminding you.

Everything is stored in a local SQLite file, so nothing is processed twice and
state survives restarts.

## Setup

### 1. iCloud app-specific password
iCloud with 2FA won't accept your normal password over IMAP/CalDAV. Create an
app-specific password at **appleid.apple.com → Sign-In & Security →
App-Specific Passwords**. The same password works for both IMAP and CalDAV.

### 2. Create the "paid" mailbox
In Apple Mail, add a mailbox under your iCloud account called `Betaald` (or
change `IMAP_PAID_FOLDER`). This is your "mark as paid" gesture.

### 3. ntfy
Either use `https://ntfy.sh` with an obscure topic name, or (better, given your
setup) your self-hosted ntfy behind Caddy/Tailscale. Install the ntfy app on
your phone and subscribe to the topic. Reminders arrive there.

### 4. Configure & run
```bash
cp .env.example .env
# edit .env with your app-specific password, ntfy topic, etc.
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

To run without Docker:
```bash
pip install -r requirements.txt
set -a; source .env; set +a
python -m billwatch.main
```

## Notes & tuning

- **Calendar discovery**: the CalDAV client auto-discovers your calendars. If it
  can't find one, set `CALENDAR_NAME` to an existing calendar, or disable the
  calendar with `CALENDAR_ENABLED=false` and rely on ntfy only.
- **False positives**: raise `CANDIDATE_MIN_SCORE` if non-bills slip through, or
  add distinctive words to `BILL_KEYWORDS`. Borderline detections come through
  as *"please check"* pushes so you stay in control.
- **Missed due dates**: if a vendor uses wording the parser doesn't know, it
  falls back to +30 days and tells you the source was `fallback` in the push —
  that's your cue to add their phrasing to `_DUE_LABELS` in `extract.py`.
- **Buffer**: set `REMIND_BUFFER_DAYS=2` to always treat bills as due two days
  early, giving yourself slack.

## Layout
```
billwatch/
  config.py     env-driven settings
  mail.py       IMAP: fetch new mail, read the paid folder
  extract.py    PDF text + due date / amount / invoice-number parsing
  classify.py   content-based bill detection & scoring
  remind.py     iCloud CalDAV events + ntfy pushes
  store.py      SQLite state
  main.py       the loop
tests/
  test_extract.py   parser tests (Dutch + English)
```

Run the parser tests with `python tests/test_extract.py`.
