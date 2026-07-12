# BillWatch — context for Claude Code

Self-hosted service that watches an iCloud inbox for studio invoices and makes
sure they get paid on time (reminders via iCloud Calendar + ntfy). Runs 24/7 in
Docker on a home server. Outbound-only: IMAP + CalDAV to iCloud, HTTP to ntfy.

> Full decision narrative, alternatives considered, usage flow, and the ordered
> build backlog live in `docs/HANDOFF.md`. Read that first when picking up work.

## Run / test
- Install: `pip install -r requirements.txt`
- Config: copy `.env.example` -> `.env`, fill in iCloud app-specific password,
  ntfy topic, etc. Load with `set -a; source .env; set +a`.
- Run (standalone, iCloud IMAP): `python -m billwatch.main`
- Run (Paperless companion): `python -m billwatch.companion` (needs `PAPERLESS_*`)
- Tests: `python tests/test_extract.py` (7/7), `tests/test_companion.py` (13/13),
  `tests/test_ninja.py` (16/16: amount parsing + sync decision). None need
  `requests` installed — the HTTP deps are imported lazily, same as `extract.py`.
- Docker: `docker compose up -d --build` (state persists in `./data`)

## Architecture (billwatch/)
- `config.py`  — all settings from env vars.
- `mail.py`    — IMAP: fetch new inbox mail (read-only, tracked by UID high-water
                 mark + Message-ID); read the "paid" folder to close the loop.
- `extract.py` — PDF text extraction (pdfplumber, PyMuPDF fallback) + parsing of
                 due date / amount / invoice no. Dutch-first, then English.
- `classify.py`— content-based bill detection & scoring (no sender allowlist).
                 Two thresholds: candidate vs confident; borderline => "review".
- `remind.py`  — notification channels, each a no-op unless configured: ntfy
                 push, Pushover push, SMTP email (`send_email`), + optional
                 iCloud CalDAV event. Calendar is off by default now. The
                 companion fans out to all of them via `companion._notify`.
- `store.py`   — SQLite state (invoices + poll high-water mark).
- `main.py`    — STANDALONE loop: process_inbox -> process_paid -> run_reminders.

Paperless companion (alternative pipeline; reuses `extract.py` + `remind.py` as-is):
- `paperless.py` — Paperless-ngx REST client: resolve doc-type/tag/field names to
                   ids; list invoices (optionally excluding the Paid tag); read /
                   set the Due-date custom field (merge-preserving); add tags;
                   resolve correspondent names; read/write the Invoice-Ninja-id
                   field; `document_url()` for clickable links.
- `invoiceninja.py` — Invoice Ninja v5 REST client (optional): find/create vendor,
                   create Expense, mark it paid. Received invoices are payables ->
                   Expenses, not Invoices.
- `companion.py`— loop: `fill_due_dates` (parse Due-date onto new invoices, flag
                   `fallback` parses with Needs-review + create one calendar event),
                   `run_reminders` (`select_reminders` = pure escalation logic;
                   styled HTML+text email + Pushover/ntfy), and optional
                   `sync_invoice_ninja` (`_ninja_action` = pure create/mark-paid
                   decision). Entry: `python -m billwatch.companion`.
                   Nearly stateless: due date + paid live in Paperless; same-day
                   dedupe uses an optional `Last reminded` field, else in-process.

Standalone flow: new mail -> classify (PDF + keywords + labelled-due-date) -> parse invoice
-> store (`pending` or `review`) -> calendar event + initial push -> daily
escalating reminders (REMIND_DAYS before due, then every day overdue) until the
email is moved to the paid folder (default `Betaald`).

## Known limitations / good next tasks
- Due-date parser only knows the wordings in `extract.py::_DUE_LABELS` /
  `_TERM_RE`. Widen these against real vendor PDFs to cut `fallback` rate.
- No weekly "still unpaid" digest push yet.
- "Mark paid" is folder-move only; an ntfy action button -> webhook could mark
  paid without touching Mail (would need a tiny HTTP endpoint).
- Paid detection re-reads the folder each cycle (fine for personal volume);
  could track a high-water mark there too.
- No tests yet for classify.py / the main loop (only extract.py is covered).
- No UI: BillWatch is headless (no web server, no ports). "Review" today = the
  flagged ntfy push + the raw SQLite DB. A small read-only dashboard (list
  pending/review/paid from `store.py`, with buttons to mark paid / fix a due date
  / dismiss a false positive) is the main quality-of-life gap for the STANDALONE
  version. The Paperless companion needs none — Paperless IS the review UI (a
  saved "unpaid" view plus editable custom fields).

## Constraints
- iCloud with 2FA needs an APP-SPECIFIC password (appleid.apple.com). The normal
  password will not authenticate over IMAP/CalDAV.
- Never mark mail read or delete anything — keep IMAP access read-only.

---

## Decision: standalone vs. build on Paperless-ngx (recorded July 2026)

**Verdict:** If Paperless-ngx is (or will be) in the stack, build the reminder
layer on top of it rather than maintaining standalone BillWatch. For a Dutch ZZP
the OCR'd, auto-tagged document archive is worth having on its own merits
(BTW / Box 3 / Belastingdienst paperwork), so the marginal cost of the reminder
layer is small and it deletes the maintenance-heavy parts. If bill reminders are
ALL that's wanted (no archive), ship standalone BillWatch as-is — it's lighter and
has no Postgres/Redis/Tika/Gotenberg dependency.

The decision is cheap to defer: `extract.py` (due-date parser) and `remind.py`
(ntfy + CalDAV) are the only hard pieces, and they port to the Paperless companion
UNCHANGED. A reasonable path is: run standalone now, migrate to the companion later.

### If building on Paperless — recommended architecture (companion poller)

Paperless owns: IMAP fetch, OCR / text extraction, invoice classification
(rules + auto-learned correspondents), storage/dedup, and the archive itself.
"Mark paid" becomes a `Paid` tag in the Paperless UI (replaces the Betaald folder).

Stays custom (reuse existing modules):
- `extract.py::parse_due_date` — Paperless detects the *document* date, NOT the
  payment due date, so the vervaldatum parser is still required. Run it once per
  new invoice and write the result into a Paperless **date custom field**
  (e.g. "Due date").
- `remind.py` — ntfy + iCloud CalDAV escalation, almost verbatim.

New glue to write:
- `paperless_client.py` — REST API wrapper: find new invoices (by document type /
  tag), fetch the OCR `content`, set the Due-date custom field, query unpaid
  invoices.
- `main.py` — loop: (1) fill the Due-date field on new invoices; (2) daily, query
  `custom_field_query` on the Due-date field (range) excluding the `Paid` tag,
  then escalate via ntfy + calendar. This mirrors the community "document-expiry
  notifier" pattern (a small container that polls the Paperless API daily).

Dies: `mail.py`, `classify.py`, and most of `store.py`.

Clickable notifications (companion):
- New config `PAPERLESS_PUBLIC_URL` = a host reachable from the phone (the
  Paperless box's Tailscale MagicDNS name or its Cloudflare-tunnel hostname —
  NOT a bare LAN IP, or links break when away from home).
- Every document has a stable URL `{PAPERLESS_PUBLIC_URL}/documents/{id}/`.
- Pass it as the ntfy `click` target on BOTH the review flag and the reminders
  (`remind.py::ntfy()` already accepts a `click` param). Optionally add an ntfy
  action button ("Mark paid") that POSTs the `Paid` tag via the Paperless API.

The companion can be nearly stateless: due date and paid status both live in
Paperless, so "last reminded" can also be a custom field rather than local state.

Native alternative (least custom code): Paperless Workflows now include a
**Scheduled** trigger keyed to a custom date field with a day offset, plus Email
and Webhook actions — so Paperless itself can ping N days before due. Downsides:
discrete offsets only (no "every day once overdue"), no calendar events, and
custom-field values inside webhook payloads have been patchy across versions.
Prefer the companion poller for the escalation + calendar behaviour.

Paperless API facts to rely on:
- Custom date fields exist; API `custom_field_query` supports range and
  gt/gte/lt/lte comparisons.
- The API can set custom-field values on a document.
- Workflow triggers: Consumption Started, Document Added, Document Updated,
  Scheduled. Actions include assignment, tags, email, and webhook.

### iCloud + Paperless-ngx — does it work?

Yes. Paperless email consumption is plain IMAP, and iCloud speaks IMAP:
- Server `imap.mail.me.com`, port 993, SSL; use an **app-specific password**
  (appleid.apple.com → Sign-In & Security; requires 2FA). Same credential this
  project already uses.
- No OAuth for iCloud (Paperless OAuth support is Gmail/Outlook only) — not
  needed; the app-specific password is the supported path.
- Gotchas: resetting the main Apple ID password revokes ALL app-specific
  passwords; if the full address is rejected as the IMAP username, try the short
  name (the part before @icloud.com); Paperless tracks processed mail by UID, so
  server-side flagging isn't required. Server-side tagging of processed mail is
  supported via the `apple:<color>` keyword convention (e.g. `apple:green`).
- If a charset error appears on a mail rule, set the rule's charset to UTF-8.
