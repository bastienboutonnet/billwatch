"""Thin Paperless-ngx REST client for the companion pipeline.

Paperless owns capture, OCR, classification, storage and the archive; this client
only reaches in to (a) read the OCR `content` of new invoices, (b) write the
payment Due-date into a custom date field, (c) add/remove tags (Needs review /
Paid), and (d) list due-and-unpaid invoices for the reminder pass.

The companion is nearly stateless: due date and paid status both live in Paperless,
so the only names that need pre-creating in the Paperless UI are the invoice
document type, the Due-date (and optional Last-reminded) custom date fields, and
the Paid / Needs-review tags. All are referenced by name here and resolved to ids
once against the API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# `requests` is imported lazily inside the methods that hit the network so the
# pure helpers (document_url, PaperlessDoc, select_reminders) stay importable — and
# testable — without the HTTP stack installed. Same lazy pattern as extract.py.

log = logging.getLogger("billwatch.paperless")


class PaperlessError(RuntimeError):
    """Raised when the Paperless API misbehaves or is misconfigured."""


def document_url(public_base: str, doc_id: int) -> str:
    """Stable, phone-openable URL for a document (used as the ntfy click target)."""
    return f"{public_base.rstrip('/')}/documents/{doc_id}/"


@dataclass
class PaperlessDoc:
    id: int
    title: str
    created: Optional[date]          # the *document* date, NOT the payment due date
    content: str                     # OCR text
    tag_ids: list[int] = field(default_factory=list)
    due: Optional[date] = None       # value of the Due-date custom field, if set
    last_reminded: Optional[date] = None
    correspondent: Optional[str] = None   # resolved correspondent name (the vendor)
    ninja_id: Optional[str] = None        # Invoice Ninja expense id, if pushed
    amount_raw: Optional[str] = None      # value of the Amount custom field
    # Raw custom-field entries ({"field": id, "value": ...}), kept so a PATCH can
    # preserve fields this companion doesn't manage.
    custom_fields: list[dict] = field(default_factory=list)


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        # Custom date fields come back as "yyyy-mm-dd"; `created` as an ISO datetime.
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class PaperlessClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        invoice_doc_type: str,
        due_field: str,
        paid_tag: str,
        review_tag: str,
        last_reminded_field: str = "",
        ninja_id_field: str = "",
        amount_field: str = "",
        public_base: str = "",
        session=None,
        timeout: int = 30,
    ):
        if not base_url or not token:
            raise PaperlessError("PAPERLESS_URL and PAPERLESS_TOKEN are required.")
        import requests
        self.api = f"{base_url.rstrip('/')}/api"
        self.public_base = (public_base or base_url).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {token}", "Accept": "application/json"}
        )
        self._names = {
            "invoice_doc_type": invoice_doc_type,
            "due_field": due_field,
            "paid_tag": paid_tag,
            "review_tag": review_tag,
            "last_reminded_field": last_reminded_field,
            "ninja_id_field": ninja_id_field,
            "amount_field": amount_field,
        }
        self._ids: dict[str, Optional[int]] = {}
        self._correspondents: Optional[dict[int, str]] = None  # id -> name cache

    # --- HTTP helpers --------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        import requests
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise PaperlessError(f"GET {url} failed: {e}") from e

    def _patch(self, path: str, body: dict) -> dict:
        import requests
        url = f"{self.api}/{path.lstrip('/')}"
        try:
            r = self.session.patch(url, json=body, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise PaperlessError(f"PATCH {url} failed: {e}") from e

    def _paginate(self, path: str, params: dict) -> list[dict]:
        """Follow Paperless' `next` links, returning every result row."""
        out: list[dict] = []
        params = {**params, "page_size": 100}
        data = self._get(path, params)
        while True:
            out.extend(data.get("results", []))
            nxt = data.get("next")
            if not nxt:
                return out
            data = self._get(nxt)

    # --- name -> id resolution ----------------------------------------------
    def _lookup_id(self, endpoint: str, name: str) -> Optional[int]:
        if not name:
            return None
        for row in self._paginate(endpoint, {"name__iexact": name}):
            if (row.get("name") or "").lower() == name.lower():
                return row["id"]
        # Fall back to a broader scan in case the server ignores name__iexact.
        for row in self._paginate(endpoint, {}):
            if (row.get("name") or "").lower() == name.lower():
                return row["id"]
        raise PaperlessError(f"Paperless {endpoint} named {name!r} not found; create it first.")

    def _resolve(self) -> None:
        if self._ids:
            return
        self._ids = {
            "invoice_doc_type": self._lookup_id("document_types/", self._names["invoice_doc_type"]),
            "due_field": self._lookup_id("custom_fields/", self._names["due_field"]),
            "paid_tag": self._lookup_id("tags/", self._names["paid_tag"]),
            "review_tag": self._lookup_id("tags/", self._names["review_tag"]),
            "last_reminded_field": (
                self._lookup_id("custom_fields/", self._names["last_reminded_field"])
                if self._names["last_reminded_field"] else None
            ),
            "ninja_id_field": (
                self._lookup_id("custom_fields/", self._names["ninja_id_field"])
                if self._names["ninja_id_field"] else None
            ),
            "amount_field": (
                self._lookup_id("custom_fields/", self._names["amount_field"])
                if self._names["amount_field"] else None
            ),
        }
        log.info("Resolved Paperless ids: %s", self._ids)

    def _id(self, key: str) -> Optional[int]:
        self._resolve()
        return self._ids[key]

    def _correspondent_name(self, cid: Optional[int]) -> Optional[str]:
        if cid is None:
            return None
        if self._correspondents is None:
            self._correspondents = {
                r["id"]: r.get("name") for r in self._paginate("correspondents/", {})
            }
        return self._correspondents.get(cid)

    # --- document parsing ----------------------------------------------------
    def _to_doc(self, row: dict) -> PaperlessDoc:
        due_id = self._id("due_field")
        lr_id = self._id("last_reminded_field")
        ninja_id_field = self._id("ninja_id_field")
        amount_id = self._id("amount_field")
        cfs = row.get("custom_fields") or []
        due = last_reminded = None
        ninja_id = amount_raw = None
        for cf in cfs:
            if cf.get("field") == due_id:
                due = _parse_date(cf.get("value"))
            elif lr_id is not None and cf.get("field") == lr_id:
                last_reminded = _parse_date(cf.get("value"))
            elif ninja_id_field is not None and cf.get("field") == ninja_id_field:
                ninja_id = cf.get("value") or None
            elif amount_id is not None and cf.get("field") == amount_id:
                amount_raw = cf.get("value") or None
        return PaperlessDoc(
            id=row["id"],
            title=row.get("title") or f"doc {row['id']}",
            created=_parse_date(row.get("created")),
            content=row.get("content") or "",
            tag_ids=list(row.get("tags") or []),
            due=due,
            last_reminded=last_reminded,
            correspondent=self._correspondent_name(row.get("correspondent")),
            ninja_id=ninja_id,
            amount_raw=amount_raw,
            custom_fields=cfs,
        )

    # --- queries -------------------------------------------------------------
    def invoices(self, *, exclude_paid: bool = False) -> list[PaperlessDoc]:
        """All documents of the invoice type, newest first.

        Filtering the due-date/paid state is done in Python by the caller so we
        don't depend on server-version quirks in `custom_field_query`. Fine for
        personal invoice volumes (tens of documents).
        """
        params: dict = {"document_type__id": self._id("invoice_doc_type"), "ordering": "-created"}
        if exclude_paid:
            paid = self._id("paid_tag")
            if paid is not None:
                params["tags__id__none"] = paid
        return [self._to_doc(r) for r in self._paginate("documents/", params)]

    def invoices_missing_due(self) -> list[PaperlessDoc]:
        """Invoices that don't yet have the Due-date custom field populated."""
        return [d for d in self.invoices() if d.due is None]

    def download(self, doc_id: int) -> bytes:
        """Raw bytes of a document's (archived) file."""
        import requests
        url = f"{self.api}/documents/{doc_id}/download/"
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            raise PaperlessError(f"download of doc {doc_id} failed: {e}") from e

    # --- mutations -----------------------------------------------------------
    def _merged_custom_fields(self, doc: PaperlessDoc, field_id: int, value) -> list[dict]:
        merged = [dict(cf) for cf in doc.custom_fields if cf.get("field") != field_id]
        merged.append({"field": field_id, "value": value})
        return merged

    def set_due_date(self, doc: PaperlessDoc, due: date) -> None:
        field_id = self._id("due_field")
        body = {"custom_fields": self._merged_custom_fields(doc, field_id, due.isoformat())}
        self._patch(f"documents/{doc.id}/", body)
        doc.due = due
        doc.custom_fields = body["custom_fields"]

    def set_last_reminded(self, doc: PaperlessDoc, when: date) -> None:
        field_id = self._id("last_reminded_field")
        if field_id is None:
            return  # feature not configured
        body = {"custom_fields": self._merged_custom_fields(doc, field_id, when.isoformat())}
        self._patch(f"documents/{doc.id}/", body)
        doc.last_reminded = when
        doc.custom_fields = body["custom_fields"]

    def set_ninja_id(self, doc: PaperlessDoc, value: str) -> None:
        field_id = self._id("ninja_id_field")
        if field_id is None:
            return  # feature not configured
        body = {"custom_fields": self._merged_custom_fields(doc, field_id, str(value))}
        self._patch(f"documents/{doc.id}/", body)
        doc.ninja_id = str(value)
        doc.custom_fields = body["custom_fields"]

    def set_amount(self, doc: PaperlessDoc, value: str) -> None:
        field_id = self._id("amount_field")
        if field_id is None:
            return  # feature not configured
        body = {"custom_fields": self._merged_custom_fields(doc, field_id, str(value))}
        self._patch(f"documents/{doc.id}/", body)
        doc.amount_raw = str(value)
        doc.custom_fields = body["custom_fields"]

    def add_tag(self, doc: PaperlessDoc, tag_key: str) -> None:
        tag_id = self._id(tag_key)
        if tag_id is None or tag_id in doc.tag_ids:
            return
        tags = doc.tag_ids + [tag_id]
        self._patch(f"documents/{doc.id}/", {"tags": tags})
        doc.tag_ids = tags

    def has_tag(self, doc: PaperlessDoc, tag_key: str) -> bool:
        tag_id = self._id(tag_key)
        return tag_id is not None and tag_id in doc.tag_ids

    def document_url(self, doc_id: int) -> str:
        return document_url(self.public_base, doc_id)
