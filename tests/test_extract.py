from datetime import date
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from billwatch.extract import parse_invoice, parse_due_date

RECEIVED = date(2026, 7, 12)

CASES = [
    # (text, expected_due, expected_source)
    ("Factuurnummer: 2026-0451\nFactuurdatum: 12-07-2026\n"
     "Vervaldatum: 11-08-2026\nTotaal te betalen: € 1.210,00",
     date(2026, 8, 11), "label"),

    ("Invoice #INV-9987\nInvoice date: 12 July 2026\n"
     "Please pay by 15 August 2026\nAmount due: € 450,00",
     date(2026, 8, 15), "label"),

    ("Factuur\nDatum: 01-06-2026\nBetalingstermijn 30 dagen\n"
     "Totaalbedrag € 89,95",
     date(2026, 7, 1), "term"),

    ("Rekening van uw studioruimte\nTe betalen vóór 31 augustus 2026\n"
     "IBAN NL00 BANK 0123 4567 89\nTotaal: €675,50",
     date(2026, 8, 31), "label"),

    ("Some invoice with no explicit due date at all.\nTotal € 100,00",
     date(2026, 8, 11), "fallback"),  # received + 30

    ("Uiterste betaaldatum 2026-09-01\nTe betalen: € 2.000,00",
     date(2026, 9, 1), "label"),
]


def run():
    ok = 0
    for i, (text, exp_due, exp_src) in enumerate(CASES, 1):
        inv = parse_invoice(text, RECEIVED, default_term_days=30)
        due_ok = inv.due == exp_due
        src_ok = inv.due_source == exp_src
        status = "PASS" if (due_ok and src_ok) else "FAIL"
        if due_ok and src_ok:
            ok += 1
        print(f"[{status}] case {i}: due={inv.due} ({inv.due_source}) "
              f"amount={inv.amount} no={inv.invoice_no}")
        if not (due_ok and src_ok):
            print(f"        expected due={exp_due} source={exp_src}")
    print(f"\n{ok}/{len(CASES)} passed")
    return ok == len(CASES)


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
