"""Minimal Invoice Ninja v5 REST client — just what the expense sync needs.

Received studio invoices are *payables*, so they map to Invoice Ninja **Expenses**
(with a Vendor), not Invoices (which are receivables). This client can find/create
a vendor, create an expense, read one back, and mark it paid (by setting the
expense's payment_date).

`requests` is imported lazily so the module stays importable without the HTTP
stack (matching extract.py / paperless.py).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

log = logging.getLogger("billwatch.invoiceninja")

# Invoice Ninja's built-in currency ids (stable across installs).
_CURRENCY_ID = {"USD": 1, "GBP": 2, "EUR": 3}


class InvoiceNinjaError(RuntimeError):
    pass


def fx_rate(frm: str, to: str, on: date) -> Optional[float]:
    """Historical FX rate (units of `to` per 1 `frm`) on/around a date, via the
    keyless ECB service frankfurter.app. Returns None on any failure (best-effort;
    a missing rate just means IN keeps its default)."""
    frm, to = frm.upper(), to.upper()
    if frm == to:
        return 1.0
    import requests
    url = f"https://api.frankfurter.app/{on.isoformat()}"
    try:
        r = requests.get(url, params={"from": frm, "to": to}, timeout=15,
                         headers={"User-Agent": "billwatch"})
        r.raise_for_status()
        return r.json().get("rates", {}).get(to)
    except Exception as e:
        log.warning("fx rate %s->%s on %s failed: %s", frm, to, on, e)
        return None


class InvoiceNinjaClient:
    def __init__(self, base_url: str, token: str, *, session=None, timeout: int = 30):
        if not base_url or not token:
            raise InvoiceNinjaError("INVOICE_NINJA_URL and INVOICE_NINJA_TOKEN are required.")
        import requests
        self.api = f"{base_url.rstrip('/')}/api/v1"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "X-API-TOKEN": token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # --- HTTP ---------------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> dict:
        import requests
        url = f"{self.api}/{path.lstrip('/')}"
        try:
            r = self.session.request(method, url, timeout=self.timeout, **kw)
            r.raise_for_status()
            return r.json() if r.content else {}
        except requests.RequestException as e:
            raise InvoiceNinjaError(f"{method} {url} failed: {e}") from e

    # --- vendors ------------------------------------------------------------
    def find_or_create_vendor(self, name: str, currency: Optional[str] = None) -> str:
        # IN takes an expense's currency from its vendor, so the vendor must carry
        # the right currency or the expense silently reverts to the company base.
        name = (name or "Unknown vendor").strip()
        cid = _CURRENCY_ID.get((currency or "").upper())
        found = self._req("GET", "vendors", params={"filter": name}).get("data", [])
        for v in found:
            if (v.get("name") or "").strip().lower() == name.lower():
                if cid and str(v.get("currency_id") or "") != str(cid):
                    self._req("PUT", f"vendors/{v['id']}", json={"currency_id": str(cid)})
                return v["id"]
        body = {"name": name}
        if cid:
            body["currency_id"] = str(cid)
        created = self._req("POST", "vendors", json=body).get("data", {})
        if not created.get("id"):
            raise InvoiceNinjaError(f"could not create vendor {name!r}")
        return created["id"]

    # --- expenses -----------------------------------------------------------
    def create_expense(self, *, vendor_id: str, amount: float, date: str,
                       currency: Optional[str] = None, base_currency: str = "EUR",
                       exchange_rate: Optional[float] = None,
                       public_notes: str = "", private_notes: str = "") -> str:
        body = {
            "vendor_id": vendor_id,
            "date": date,
            "public_notes": public_notes,
            "private_notes": private_notes,
        }
        foreign = _CURRENCY_ID.get((currency or "").upper())
        base = _CURRENCY_ID.get(base_currency.upper())
        if foreign and base and foreign != base and exchange_rate:
            # Matches how IN stores a foreign-currency expense: currency_id is the
            # expense currency, amount is in it, and foreign_amount is the value
            # converted to the company base. IN only keeps a non-base currency_id
            # when foreign_amount is supplied too.
            body["currency_id"] = str(foreign)
            body["amount"] = amount
            body["foreign_amount"] = round(amount * exchange_rate, 2)
            body["exchange_rate"] = exchange_rate
        else:
            body["amount"] = amount
        created = self._req("POST", "expenses", json=body).get("data", {})
        if not created.get("id"):
            raise InvoiceNinjaError("expense creation returned no id")
        return created["id"]

    def get_expense(self, expense_id: str) -> dict:
        return self._req("GET", f"expenses/{expense_id}").get("data", {})

    def is_expense_paid(self, expense_id: str) -> bool:
        return bool(self.get_expense(expense_id).get("payment_date"))

    def mark_expense_paid(self, expense_id: str, payment_date: str,
                          exchange_rate: Optional[float] = None) -> None:
        body: dict = {"payment_date": payment_date}
        if exchange_rate:
            body["exchange_rate"] = exchange_rate
        self._req("PUT", f"expenses/{expense_id}", json=body)

    def attach_document(self, expense_id: str, filename: str, data: bytes) -> None:
        """Attach a file to an expense. Invoice Ninja v5 saves uploaded
        `documents[]` on the entity's update route; we POST with a Laravel
        `_method=PUT` override so the multipart body is parsed correctly."""
        import requests
        url = f"{self.api}/expenses/{expense_id}"
        files = {"documents[]": (filename, data, "application/pdf")}
        try:
            # Drop the JSON Content-Type so requests sets the multipart boundary.
            r = self.session.post(url, data={"_method": "PUT"}, files=files,
                                  timeout=self.timeout, headers={"Content-Type": None})
            r.raise_for_status()
        except requests.RequestException as e:
            raise InvoiceNinjaError(f"attach to expense {expense_id} failed: {e}") from e
