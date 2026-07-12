"""Tests for the Invoice Ninja sync helpers — amount normalisation and the pure
create/mark-paid decision. No HTTP; the companion's pure helpers import cleanly."""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from billwatch.companion import _amount_to_float, _ninja_action

# (amount string, expected float or None)
_AMOUNTS = [
    ("€816,75", 816.75),
    ("€1,250.00", 1250.00),   # US thousands
    ("€1.210,00", 1210.00),   # EU thousands
    ("€2.000,00", 2000.00),
    ("€45,50", 45.50),
    ("1234,56 EUR", 1234.56),
    ("EUR 1 250,00", 1250.00),  # space thousands
    ("950.00", 950.00),
    (None, None),
    ("", None),
]

# (pushed, needs_review, paid, has_due) -> action
_ACTIONS = [
    ((False, False, False, True), "create"),    # confident + due -> push
    ((False, True, False, True), None),         # needs review -> hold
    ((False, False, False, False), None),       # no due date yet -> hold
    ((True, False, False, True), None),         # pushed, unpaid -> nothing
    ((True, False, True, True), "mark_paid"),   # pushed + paid -> mark paid
    ((True, True, True, True), "mark_paid"),    # pushed + paid, review irrelevant
]


def run() -> bool:
    ok = 0
    for raw, exp in _AMOUNTS:
        got = _amount_to_float(raw)
        good = (got is None and exp is None) or (
            got is not None and exp is not None and abs(got - exp) < 1e-9)
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] amount {raw!r} -> {got} (want {exp})")
    for args, exp in _ACTIONS:
        got = _ninja_action(*args)
        good = got == exp
        ok += good
        print(f"[{'PASS' if good else 'FAIL'}] action {args} -> {got} (want {exp})")
    total = len(_AMOUNTS) + len(_ACTIONS)
    print(f"\n{ok}/{total} passed")
    return ok == total


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
