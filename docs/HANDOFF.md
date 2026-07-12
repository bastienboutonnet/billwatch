# BillWatch — handoff & roadmap

Context transfer for continuing this project in Claude Code. `CLAUDE.md` (repo
root) has the operational summary, architecture, and constraints; this file has
the fuller reasoning so nothing from the design discussion is lost.

## The problem being solved

Studio invoices arrive in an iCloud inbox (Apple Mail) as PDF attachments, due
roughly a month after they're sent. Apple Mail's "Remind Me" resurfaces an email
silently (not unread, no flag, no reliable push), so bills get missed. Goal: never
miss a bill again, with as little manual effort as possible.

Key facts about the incoming mail (these shaped the design):
- Senders are VARIED — no reliable allowlist, so detection must be content-based.
- The due date lives in the attached PDF (Dutch: `vervaldatum` / `te betalen
  voor` / `betalingstermijn N dagen`), not in the email body.
- Reminders wanted in BOTH forms: an iCloud calendar event AND push notifications.
- "Mark paid" should stop the reminders.

## What exists now (standalone BillWatch)

A headless Python service (this repo). Loop: poll iCloud IMAP -> detect bills by
content (PDF attachment + keywords + a labelled due date) -> parse the due date
from the PDF -> store in SQLite -> create an iCloud calendar event + send an
initial ntfy push -> escalating reminders (7/3/1/0 days before, then daily once
overdue) until the email is moved to a `Betaald` folder. See `CLAUDE.md` for the
module map. `tests/test_extract.py` passes 6/6; the parser is the crown jewel.

## The decision: standalone vs. build on Paperless-ngx

Investigated whether an off-the-shelf tool already does this before maintaining
custom code.

- The invoicing/billing OSS tools (Invoice Ninja, InvoicePlane, SolidInvoice,
  FOSSBilling, etc.) are all for SENDING invoices to clients — wrong direction.
- Dedicated bill/payment reminder trackers exist: **Wallos** (self-hosted, Docker,
  notifies via email/Telegram/Discord/Gotify/ntfy webhooks) and **Firefly III**
  (bills with due dates). But they're manual-entry and subscription/recurring
  oriented — they don't read email or parse PDFs, so they don't fit varied
  one-off invoices.
- **Paperless-ngx** is the strong fit for the hard half: it consumes email
  attachments, OCRs them, auto-classifies (rules + learned correspondents), and
  archives everything searchably. As a Dutch ZZP the archive is worth having on
  its own (BTW / Box 3 / Belastingdienst paperwork). What it lacks is the
  "remind me to pay / escalate until paid" behaviour.

**Verdict:** If Paperless is (or will be) in the stack, build the reminder layer
on top of it — it deletes the maintenance-heavy parts (IMAP, OCR, classification,
storage) and keeps only the two irreducible pieces (`extract.py` due-date parser,
`remind.py` reminders), which port UNCHANGED. If bill reminders are ALL that's
wanted (no archive), ship standalone BillWatch — lighter, no Postgres/Redis/Tika/
Gotenberg dependency. The choice is cheap to defer: run standalone now, migrate
later, because the two hard modules don't change.

(Third-party email clients — Spark, Canary Mail — were also considered for their
snooze/follow-up that returns mail unread + push. Rejected as the primary answer:
still manual per-bill, and they route mail through their servers, which isn't
great for invoices full of IBANs. Fine as a general-inbox nicety, not the bill
solution.)

## Target architecture (Paperless companion)

Paperless owns capture/OCR/classification/storage/archive. The companion is a
thin API client. Their entire interface is a small shared vocabulary set up once
in Paperless: a document type `Invoice`, custom fields (`Due date` date-type,
optionally `Amount` / `Invoice no` / `Last reminded`), and tags (`Paid`,
`Needs review`), plus each document's OCR `content`.

Companion loop:
1. Find new invoices (document type/tag) that have no `Due date` yet; fetch their
   OCR `content`; run `extract.parse_due_date`; PATCH the `Due date` custom field
   (tag `Needs review` if the parse was low-confidence / used the +30-day fallback).
2. Daily, query the API (`custom_field_query` on `Due date`, range) excluding the
   `Paid` tag; escalate via ntfy + iCloud calendar.
3. "Mark paid" = human adds the `Paid` tag in the Paperless UI; companion observes
   it on the next sweep and stops. Reminders go to ntfy/calendar only — Paperless
   never hears about them.

Native alternative (least code): Paperless Workflows now have a **Scheduled**
trigger on a custom date field with a day offset, plus Email/Webhook actions.
Downside: discrete offsets only (no "every day overdue"), no calendar events,
patchy custom-field-in-webhook support. Prefer the companion poller.

## Usage flow (what the human actually does)

Automatic: bill arrives -> Paperless files it (OCR, tag, archive) -> companion
sets the due date -> reminders arrive (ntfy + calendar). Human, routine: pay the
bill, then tap the `Paid` tag (two taps). Human, occasional: if a bill is flagged
`Needs review`, open it and correct the `Due date`. That's the entire involvement.
A saved "unpaid invoices" view in Paperless doubles as a live to-pay list.

## Clickable notifications (the last feature discussed)

When a low-confidence bill is flagged, the push should link straight to the
document. Every Paperless doc has a stable URL `{PAPERLESS_PUBLIC_URL}/documents/
{id}/`. Set `PAPERLESS_PUBLIC_URL` to a host reachable from the phone (Tailscale
MagicDNS name or Cloudflare-tunnel hostname — not a LAN IP). `remind.py::ntfy()`
already takes a `click` param, so it's a couple of lines to pass the URL on both
the review flag and the reminders. Optional: an ntfy action button that POSTs the
`Paid` tag via the API so you can clear a bill without opening anything.

## Build backlog (rough order)

Companion track (recommended if adopting Paperless):
1. `billwatch/paperless.py` — REST client: find new invoices; fetch OCR content;
   set `Due date` custom field; add/remove tags; query due+unpaid via
   `custom_field_query`. Auth via `PAPERLESS_URL` + `PAPERLESS_TOKEN`.
2. Companion entry point (e.g. `billwatch/companion.py`) — the loop above. Reuse
   `extract.py` and `remind.py` unchanged. Keep the standalone entry point working.
3. Config keys: `PAPERLESS_URL`, `PAPERLESS_TOKEN`, `PAPERLESS_PUBLIC_URL`, and the
   invoice document-type / `Due date` field / `Paid` + `Needs review` tag names.
4. Clickable Paperless link in review + reminder pushes (see above).
5. Tests: URL building + reminder selection (which invoices fire today). Mock the
   Paperless client — no live instance needed.
6. Update `.env.example` and `CLAUDE.md`; document the Paperless setup (create the
   doc type, custom fields, tags, mail rule, API token).

Improvements useful to EITHER version:
- Widen the due-date parser (`extract.py::_DUE_LABELS` / `_TERM_RE`) against real
  vendor PDFs to cut the `fallback` rate — highest-value single improvement.
- Weekly "still unpaid" digest push.
- Tests for `classify.py` and the main loop.

Standalone-only:
- Small read-only review dashboard (FastAPI/Flask over `store.py`) to list
  pending/review/paid and mark paid / fix a due date / dismiss. Only worth it if
  staying standalone — the companion gets this free from Paperless.

## Ready-to-run Claude Code prompt (companion)

```
Build the Paperless companion described in CLAUDE.md and docs/HANDOFF.md.
Add billwatch/paperless.py (REST client: find new invoices by document type/tag
missing the Due-date field; fetch OCR content; set the Due-date custom field;
add/remove tags; query due-and-unpaid via custom_field_query). Add a companion
entry point that runs the loop: fill Due-date on new invoices via
extract.parse_due_date (tag Needs-review on low confidence), then a daily pass
that queries due+unpaid excluding the Paid tag and sends reminders. Reuse
extract.py and remind.py unchanged. Add config keys PAPERLESS_URL,
PAPERLESS_TOKEN, PAPERLESS_PUBLIC_URL, and the invoice document-type / Due-date
field / Paid + Needs-review tag names. Put a clickable link
{PAPERLESS_PUBLIC_URL}/documents/{id}/ into both the review-flag and reminder
ntfy pushes via the existing click param. Keep the standalone entry point
working. Add tests for URL building and reminder selection (mock the Paperless
client). Update .env.example and CLAUDE.md. Run the tests.
```
