"""Tests for the Paperless companion: clickable-URL building and which invoices
fire a reminder on a given day. No live Paperless instance — plain doc objects.
"""
from datetime import date
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from billwatch.paperless import PaperlessDoc, document_url
from billwatch.companion import select_reminders

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


def run() -> bool:
    print("== document_url ==")
    u = _check_urls()
    print("\n== select_reminders ==")
    s = _check_selection()
    total = len(_URL_CASES) + len(_SEL_CASES)
    passed = u + s
    print(f"\n{passed}/{total} passed")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
