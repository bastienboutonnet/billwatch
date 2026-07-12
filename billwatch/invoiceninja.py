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
from typing import Optional

log = logging.getLogger("billwatch.invoiceninja")


class InvoiceNinjaError(RuntimeError):
    pass


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
    def find_or_create_vendor(self, name: str) -> str:
        name = (name or "Unknown vendor").strip()
        found = self._req("GET", "vendors", params={"filter": name}).get("data", [])
        for v in found:
            if (v.get("name") or "").strip().lower() == name.lower():
                return v["id"]
        created = self._req("POST", "vendors", json={"name": name}).get("data", {})
        if not created.get("id"):
            raise InvoiceNinjaError(f"could not create vendor {name!r}")
        return created["id"]

    # --- expenses -----------------------------------------------------------
    def create_expense(self, *, vendor_id: str, amount: float, date: str,
                       public_notes: str = "", private_notes: str = "") -> str:
        body = {
            "vendor_id": vendor_id,
            "amount": amount,
            "date": date,
            "public_notes": public_notes,
            "private_notes": private_notes,
        }
        created = self._req("POST", "expenses", json=body).get("data", {})
        if not created.get("id"):
            raise InvoiceNinjaError("expense creation returned no id")
        return created["id"]

    def get_expense(self, expense_id: str) -> dict:
        return self._req("GET", f"expenses/{expense_id}").get("data", {})

    def is_expense_paid(self, expense_id: str) -> bool:
        return bool(self.get_expense(expense_id).get("payment_date"))

    def mark_expense_paid(self, expense_id: str, payment_date: str) -> None:
        self._req("PUT", f"expenses/{expense_id}", json={"payment_date": payment_date})

    def attach_document(self, expense_id: str, filename: str, data: bytes) -> None:
        """Upload a file to an expense (multipart). Invoice Ninja v5 exposes
        POST /{entity}/{id}/upload with the file(s) under `documents[]`."""
        import requests
        url = f"{self.api}/expenses/{expense_id}/upload"
        files = {"documents[]": (filename, data, "application/pdf")}
        try:
            # Drop the JSON Content-Type so requests sets the multipart boundary.
            r = self.session.post(url, files=files, timeout=self.timeout,
                                  headers={"Content-Type": None})
            r.raise_for_status()
        except requests.RequestException as e:
            raise InvoiceNinjaError(f"upload to expense {expense_id} failed: {e}") from e
