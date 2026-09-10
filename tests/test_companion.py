"""Tests for the Paperless companion: clickable-URL building and which invoices
fire a reminder on a given day. No live Paperless instance — plain doc objects.
"""
from datetime import date
import logging
import re
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from billwatch.paperless import PaperlessDoc, document_url, PaperlessUnavailable
from billwatch.invoiceninja import InvoiceNinjaUnavailable
from billwatch.companion import select_reminders, _run_step
import billwatch.companion as _comp

TODAY = date(2026, 7, 12)
REMIND_DAYS = [7, 3, 1, 0]


def _doc(doc_id: int, due):
    return PaperlessDoc(id=doc_id, title=f"Invoice {doc_id}", created=None, content="", due=due)


# --- document_url --------------------------------------------------------------
_URL_CASES = [
    ("https://paperless.example.com", 42, "https://paperless.example.com/documents/42/"),
    ("https://paperless.example.com/", 7, "https://paperless.example.com/documents/7/"),
    ("https://box.ts.net", 1001, "https://box.ts.net/documents/1001/"),
]


def _check_urls() -> int:
    ok = 0
    for base, doc_id, expected in _URL_CASES:
        got = document_url(base, doc_id)
        good = got == expected
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] url({base!r},{doc_id}) -> {got}")
        if not good:
            print(f"        expected {expected}")
    return ok


# --- select_reminders ----------------------------------------------------------
# (due_offset_days_from_today, buffer_days, should_fire, expected_overdue, expected_days)
_SEL_CASES = [
    (0, 0, True, False, 0),      # due today
    (1, 0, True, False, 1),      # 1 day before -> in REMIND_DAYS
    (2, 0, False, None, None),   # 2 days -> not a remind day
    (3, 0, True, False, 3),
    (7, 0, True, False, 7),
    (5, 0, False, None, None),
    (-1, 0, True, True, -1),     # overdue by 1 -> daily
    (-10, 0, True, True, -10),   # still overdue -> daily
    (2, 2, True, False, 0),      # buffer pulls a 2-day-out bill to "due today"
    (9, 2, True, False, 7),      # buffer makes a 9-day-out bill a 7-day reminder
]


def _check_selection() -> int:
    ok = 0
    # None-due doc must always be skipped; interleave it in every run.
    none_doc = _doc(999, None)
    for i, (offset, buf, should, exp_overdue, exp_days) in enumerate(_SEL_CASES, 1):
        due = date.fromordinal(TODAY.toordinal() + offset)
        doc = _doc(i, due)
        fired = select_reminders([none_doc, doc], TODAY, REMIND_DAYS, buffer_days=buf)
        assert all(r.doc.id != 999 for r in fired), "None-due doc should never fire"
        hit = next((r for r in fired if r.doc.id == i), None)
        if should:
            good = hit is not None and hit.overdue == exp_overdue and hit.days == exp_days
        else:
            good = hit is None
        ok += good
        detail = (f"overdue={hit.overdue} days={hit.days}" if hit else "no-fire")
        print(f"[{'PASS' if good else 'FAIL'}] due{offset:+d}d buf={buf} -> {detail} "
              f"(want {'fire' if should else 'no-fire'})")
    return ok


# --- _run_step connectivity throttling -----------------------------------------
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


_RUN_STEP_CASES = 4  # keep in sync with the checks below


def _check_run_step() -> int:
    """A transient outage should warn once (not every sweep) and recover cleanly;
    a real bug must still surface as a full traceback each sweep."""
    ok = 0
    log = logging.getLogger("billwatch.companion")
    cap = _Capture()
    log.addHandler(cap)

    def _boom(exc):
        def fn():
            raise exc
        return fn

    try:
        # 1. Two down sweeps -> exactly one WARNING, flag latched, step returns False.
        _comp._unreachable = False
        cap.records.clear()
        down = _boom(PaperlessUnavailable("GET http://wanker.lan:8000 unreachable: dns"))
        r1, r2 = _run_step("fill_due_dates", down), _run_step("fill_due_dates", down)
        warns = [r for r in cap.records if r.levelno == logging.WARNING]
        good = r1 is False and r2 is False and len(warns) == 1 and _comp._unreachable
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] transient outage: 2 sweeps -> {len(warns)} warning(s)")

        # 2. Recovery -> one INFO, flag cleared, step returns True.
        _comp._unreachable = True
        cap.records.clear()
        r3 = _run_step("fill_due_dates", lambda: None)
        infos = [r for r in cap.records if r.levelno == logging.INFO]
        good = r3 is True and len(infos) == 1 and not _comp._unreachable
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] recovery: -> {len(infos)} info(s), flag cleared")

        # 3. Invoice Ninja connectivity errors throttle the same way.
        _comp._unreachable = False
        cap.records.clear()
        r4 = _run_step("invoice ninja sync",
                       _boom(InvoiceNinjaUnavailable("POST http://wanker.lan:8012 unreachable: dns")))
        good = r4 is False and _comp._unreachable
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] ninja outage throttled: flag set")

        # 4. A genuine bug is NOT swallowed as an outage: ERROR each sweep, flag untouched.
        _comp._unreachable = False
        cap.records.clear()
        r5 = _run_step("fill_due_dates", _boom(ValueError("boom")))
        errs = [r for r in cap.records if r.levelno == logging.ERROR]
        good = r5 is False and len(errs) == 1 and not _comp._unreachable
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] real bug: -> {len(errs)} error(s), not an outage")
    finally:
        log.removeHandler(cap)
        _comp._unreachable = False
    return ok


# --- vendor sync ---------------------------------------------------------------
# The vendor pushed to Invoice Ninja must be the Paperless correspondent, never the
# email subject (doc.title) — and editing it in Paperless must follow through to IN.
class _FakeNinjaClient:
    """Records vendor/expense calls; vendor ids are stable per name so we can assert
    which name won and whether an expense got re-pointed."""
    def __init__(self):
        self.vendor_ids = {}          # name -> id
        self.expense_vendor = {}      # expense_id -> vendor_id
        self.created = []             # (vendor_id, amount)
        self.repointed = []           # (expense_id, vendor_id)

    def find_or_create_vendor(self, name, currency=None):
        return self.vendor_ids.setdefault(name, f"v-{name}")

    def create_expense(self, *, vendor_id, amount, date, public_notes="", private_notes=""):
        eid = f"e-{len(self.created) + 1}"
        self.created.append((vendor_id, amount))
        self.expense_vendor[eid] = vendor_id
        return eid

    def attach_document(self, *a, **k):
        pass

    def set_expense_vendor(self, expense_id, vendor_id):
        if str(self.expense_vendor.get(expense_id) or "") == str(vendor_id):
            return False
        self.expense_vendor[expense_id] = vendor_id
        self.repointed.append((expense_id, vendor_id))
        return True

    def reconcile_expense(self, *a, **k):
        return False

    def is_expense_paid(self, expense_id):
        return False

    def mark_expense_paid(self, *a, **k):
        pass


class _FakeClient:
    def __init__(self, docs):
        self._docs = docs
        self.ninja_ids = {}           # doc id -> value set via set_ninja_id
        self.tagged = []              # (doc id, tag key) added via add_tag

    def invoices(self):
        return self._docs

    def has_tag(self, doc, key):
        return (doc.id, key) in self.tagged

    def add_tag(self, doc, key):
        if (doc.id, key) not in self.tagged:
            self.tagged.append((doc.id, key))

    def set_ninja_id(self, doc, value):
        self.ninja_ids[doc.id] = value
        doc.ninja_id = value

    def document_url(self, doc_id):
        return f"https://p/documents/{doc_id}/"

    def download(self, doc_id):
        return b"%PDF-"

    def set_currency(self, *a, **k):
        pass

    def set_amount(self, *a, **k):
        pass

    def set_rate(self, *a, **k):
        pass


def _paid_doc(doc_id, correspondent, *, ninja_id=None, title="Weird email subject"):
    # currency == base (EUR) so _doc_money_fields needs no FX lookup or writes.
    return PaperlessDoc(id=doc_id, title=title, created=date(2026, 7, 1), content="",
                        due=date(2026, 8, 1), correspondent=correspondent,
                        ninja_id=ninja_id, currency_raw="EUR", amount_raw="100.00")


def _check_vendor_sync() -> int:
    from billwatch.companion import sync_invoice_ninja
    ok = 0
    prev_base = _comp.config.INVOICE_NINJA_BASE_CURRENCY
    _comp.config.INVOICE_NINJA_BASE_CURRENCY = "EUR"
    try:
        # 1. No correspondent yet -> defer: nothing created, no ninja id stored.
        doc = _paid_doc(1, None, title="Invoice from someone")
        client, ninja = _FakeClient([doc]), _FakeNinjaClient()
        sync_invoice_ninja(client, ninja)
        good = (not ninja.created and 1 not in client.ninja_ids
                and (1, "review_tag") in client.tagged)
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] no correspondent -> deferred + "
              f"flagged review (created={len(ninja.created)}, tagged={client.tagged})")

        # 2. Correspondent present -> vendor is the correspondent, NOT the subject.
        doc = _paid_doc(2, "Acme Studio", title="Re: your bill #42")
        client, ninja = _FakeClient([doc]), _FakeNinjaClient()
        sync_invoice_ninja(client, ninja)
        good = ninja.created and ninja.created[0][0] == "v-Acme Studio"
        ok += good
        got = ninja.created[0][0] if ninja.created else "<none>"
        print(f"[{'PASS' if good else 'FAIL'}] uses correspondent as vendor -> {got}")

        # 3. Already pushed, correspondent edited in Paperless -> expense re-pointed.
        doc = _paid_doc(3, "Corrected Vendor", ninja_id="e-1")
        client, ninja = _FakeClient([doc]), _FakeNinjaClient()
        ninja.expense_vendor["e-1"] = "v-Old Subject Vendor"  # the buggy original
        sync_invoice_ninja(client, ninja)
        good = ninja.repointed == [("e-1", "v-Corrected Vendor")]
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] edit in Paperless re-points vendor -> "
              f"{ninja.repointed}")
    finally:
        _comp.config.INVOICE_NINJA_BASE_CURRENCY = prev_base
    return ok


_VENDOR_SYNC_CASES = 3  # keep in sync with _check_vendor_sync


# --- Paperless client: skip tag + correspondent cache --------------------------
# Both are exercised against a stubbed `requests` module, since the real HTTP stack
# isn't a test dependency (same lazy-import contract as extract.py).
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Serves canned Paperless API rows and records which URLs were hit."""
    def __init__(self, docs, correspondents, tags):
        self.docs = docs
        self.correspondents = correspondents
        self.tags = tags
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if "/documents/" in url:
            rows = self.docs
        elif re.search(r"/correspondents/\d+/$", url):
            cid = int(url.rstrip("/").rsplit("/", 1)[1])
            return _FakeResponse(
                {"id": cid, "name": self.correspondents.get(cid, {}).get("name")})
        elif "/correspondents" in url:
            rows = [{"id": k, **v} for k, v in self.correspondents.items()
                    if v.get("listed", True)]
        elif "/tags" in url:
            rows = self.tags
        elif "/document_types" in url:
            rows = [{"id": 1, "name": "Invoice"}]
        elif "/custom_fields" in url:
            rows = [{"id": 10, "name": "Due date"}]
        else:
            rows = []
        return _FakeResponse({"results": rows, "next": None})


def _install_fake_requests():
    """Minimal stand-in so paperless.py's lazy `import requests` resolves."""
    import types
    mod = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    mod.RequestException = RequestException
    mod.ConnectionError = type("ConnectionError", (RequestException,), {})
    mod.Timeout = type("Timeout", (RequestException,), {})
    mod.Session = lambda: None
    sys.modules.setdefault("requests", mod)


def _make_client(session):
    from billwatch.paperless import PaperlessClient
    _install_fake_requests()
    return PaperlessClient(
        "http://paperless.test", "token",
        invoice_doc_type="Invoice", due_field="Due date",
        paid_tag="Paid", review_tag="Needs review", skip_tag="Skip",
        session=session,
    )


_TAGS = [{"id": 100, "name": "Paid"}, {"id": 101, "name": "Needs review"},
         {"id": 102, "name": "Skip"}]


def _check_paperless_client() -> int:
    ok = 0
    rows = [
        {"id": 1, "title": "Keep me", "created": "2026-07-01", "content": "", "tags": []},
        {"id": 2, "title": "Skip me", "created": "2026-07-01", "content": "", "tags": [102]},
    ]

    # 1. Skip-tagged invoices never reach any caller.
    client = _make_client(_FakeSession(rows, {}, _TAGS))
    got = [d.id for d in client.invoices()]
    good = got == [1]
    ok += good
    print(f"[{'PASS' if good else 'FAIL'}] skip tag filters the document -> ids {got}")

    # 2. Skip tag absent from Paperless -> feature off, nothing blows up.
    client = _make_client(_FakeSession(rows, {}, _TAGS[:2]))
    got = [d.id for d in client.invoices()]
    good = got == [1, 2]
    ok += good
    print(f"[{'PASS' if good else 'FAIL'}] missing skip tag disables filtering -> ids {got}")

    # 3. A correspondent created AFTER the name cache was built must still resolve;
    #    otherwise the doc looks vendor-less forever and gets re-flagged every sweep.
    late = {7: {"name": "Early Vendor"}, 9: {"name": "Late Vendor", "listed": False}}
    doc_rows = [{"id": 1, "title": "x", "created": "2026-07-01", "content": "",
                 "tags": [], "correspondent": 9}]
    session = _FakeSession(doc_rows, late, _TAGS)
    client = _make_client(session)
    name = client.invoices()[0].correspondent
    refetched = any(re.search(r"/correspondents/9/$", u) for u in session.calls)
    good = name == "Late Vendor" and refetched
    ok += good
    print(f"[{'PASS' if good else 'FAIL'}] correspondent added after cache -> {name!r} "
          f"(refetched={refetched})")
    return ok


_CLIENT_CASES = 3  # keep in sync with _check_paperless_client


def run() -> bool:
    print("== document_url ==")
    u = _check_urls()
    print("\n== select_reminders ==")
    s = _check_selection()
    print("\n== _run_step throttling ==")
    rs = _check_run_step()
    print("\n== vendor sync ==")
    vs = _check_vendor_sync()
    print("\n== paperless client ==")
    pc = _check_paperless_client()
    total = (len(_URL_CASES) + len(_SEL_CASES) + _RUN_STEP_CASES
             + _VENDOR_SYNC_CASES + _CLIENT_CASES)
    passed = u + s + rs + vs + pc
    print(f"\n{passed}/{total} passed")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
