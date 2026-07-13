from datetime import date
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from billwatch.extract import parse_invoice, parse_due_date

RECEIVED = date(2026, 7, 12)

CASES = [
    # (text, expected_due, expected_source, expected_amount)
    ("Factuurnummer: 2026-0451\nFactuurdatum: 12-07-2026\n"
     "Vervaldatum: 11-08-2026\nTotaal te betalen: € 1.210,00",
     date(2026, 8, 11), "label", "€1.210,00"),

    ("Invoice #INV-9987\nInvoice date: 12 July 2026\n"
     "Please pay by 15 August 2026\nAmount due: € 450,00",
     date(2026, 8, 15), "label", "€450,00"),

    ("Factuur\nDatum: 01-06-2026\nBetalingstermijn 30 dagen\n"
     "Totaalbedrag € 89,95",
     date(2026, 7, 1), "term", "€89,95"),

    ("Rekening van uw studioruimte\nTe betalen vóór 31 augustus 2026\n"
     "IBAN NL00 BANK 0123 4567 89\nTotaal: €675,50",
     date(2026, 8, 31), "label", "€675,50"),

    ("Some invoice with no explicit due date at all.\nTotal € 100,00",
     date(2026, 8, 11), "fallback", "€100,00"),  # received + 30

    ("Uiterste betaaldatum 2026-09-01\nTe betalen: € 2.000,00",
     date(2026, 9, 1), "label", "€2.000,00"),

    # US-style thousands separator must not truncate to €1,25.
    ("Invoice date: 12 July 2026\nPlease pay by 20 August 2026\n"
     "Amount due: € 1,250.00",
     date(2026, 8, 20), "label", "€1,250.00"),

    # Stacked label/value columns (real autobestickeren.com invoice): the date
    # right after "Vervaldatum" is actually the factuurdatum value. The restated
    # payment sentence must win -> 12-08-2026, not 13-07-2026.
    ("Factuurnummer\nFactuurdatum\nVervaldatum\nUw referentie\n"
     "20260410\n13-07-2026\n12-08-2026\n"
     "Totaal inclusief BTW € 415,03\n"
     "Wij verzoeken u het bedrag van € 415,03 voor 12-08-2026 te voldoen.",
     date(2026, 8, 12), "sentence", "€415,03"),

    # Same stacked header WITHOUT the sentence: rather than confidently return the
    # wrong 13-07-2026, the guard makes it fall back (flagged for review).
    ("Factuurnummer\nFactuurdatum\nVervaldatum\nUw referentie\n"
     "20260410\n13-07-2026\n12-08-2026\nTotaal inclusief BTW € 415,03",
     date(2026, 8, 11), "fallback", "€415,03"),

    # Bare stacked header — no sentence AND no competing label between. The
    # two-dates-in-the-window signal must still force a review-flagged fallback,
    # not a confident wrong date.
    ("Factuurnummer\nFactuurdatum\nVervaldatum\n"
     "20260410\n13-07-2026\n12-08-2026\nTotaal € 415,03",
     date(2026, 8, 11), "fallback", "€415,03"),
]


def run():
    ok = 0
    for i, (text, exp_due, exp_src, exp_amt) in enumerate(CASES, 1):
        inv = parse_invoice(text, RECEIVED, default_term_days=30)
        due_ok = inv.due == exp_due
        src_ok = inv.due_source == exp_src
        amt_ok = inv.amount == exp_amt
        status = "PASS" if (due_ok and src_ok and amt_ok) else "FAIL"
        if due_ok and src_ok and amt_ok:
            ok += 1
        print(f"[{status}] case {i}: due={inv.due} ({inv.due_source}) "
              f"amount={inv.amount} no={inv.invoice_no}")
        if not (due_ok and src_ok and amt_ok):
            print(f"        expected due={exp_due} source={exp_src} amount={exp_amt}")
    print(f"\n{ok}/{len(CASES)} passed")
    return ok == len(CASES)


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
