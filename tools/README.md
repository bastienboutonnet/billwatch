# tools/ — test helpers (not shipped in the image)

## gen_test_invoices.py

Generates sample invoices to exercise BillWatch end-to-end. Three documents,
always dated relative to **today** so they're always in the future:

| key       | language | due date        | exercises                                  |
|-----------|----------|-----------------|--------------------------------------------|
| `dutch`   | Dutch    | today + 7 days  | labelled `Vervaldatum` → "Due in 7d"       |
| `english` | English  | today + 3 days  | "Please pay by …" → "Due in 3d"            |
| `unclear` | —        | none            | no due date → +30d fallback, `Needs review`|

### Setup
```bash
pip install -r tools/requirements.txt
```

### Generate + inspect (no sending)
Writes PDFs to `./test-invoices/` and prints what BillWatch's own parser detects:
```bash
python tools/gen_test_invoices.py
python tools/gen_test_invoices.py --only dutch,unclear --out /tmp/inv
```

### Email them (each becomes its own Paperless document)
SMTP settings come from the environment — **no secrets in the repo**. iCloud
needs an app-specific password.
```bash
SMTP_USER=you@icloud.com SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
  python tools/gen_test_invoices.py --send --to accounting@bubbleform.xyz
```
Env vars: `SMTP_HOST` (default `smtp.mail.me.com`), `SMTP_PORT` (587),
`SMTP_SECURITY` (`starttls`|`ssl`|`none`), `SMTP_USER`, `SMTP_PASSWORD`,
and `--to` / `TEST_INVOICE_TO`.

### After testing
These land in your real Paperless archive once imported — delete the test
documents (or tag them `Paid`) so they don't linger as fake unpaid bills.
