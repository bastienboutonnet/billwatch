"""SQLite persistence: what we've already seen, and the state of each invoice."""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    message_id     TEXT PRIMARY KEY,
    uid            INTEGER,
    sender         TEXT,
    subject        TEXT,
    invoice_no     TEXT,
    amount         TEXT,
    due_date       TEXT,      -- ISO yyyy-mm-dd
    due_source     TEXT,      -- label | term | fallback
    received_date  TEXT,
    status         TEXT,      -- pending | review | paid
    calendar_uid   TEXT,
    last_reminded  TEXT,      -- ISO date of last ntfy ping
    created_at     TEXT
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- meta / high-water mark ---------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    # --- invoices ------------------------------------------------------------
    def seen(self, message_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM invoices WHERE message_id=?", (message_id,)
        ).fetchone() is not None

    def add(self, **kw) -> None:
        cols = ("message_id", "uid", "sender", "subject", "invoice_no", "amount",
                "due_date", "due_source", "received_date", "status",
                "calendar_uid", "last_reminded", "created_at")
        vals = tuple(kw.get(c) for c in cols)
        placeholders = ",".join("?" * len(cols))
        self.db.execute(
            f"INSERT OR IGNORE INTO invoices({','.join(cols)}) VALUES({placeholders})",
            vals,
        )
        self.db.commit()

    def unpaid(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM invoices WHERE status IN ('pending','review') ORDER BY due_date"
        ).fetchall()

    def mark_paid(self, message_id: str) -> None:
        self.db.execute(
            "UPDATE invoices SET status='paid' WHERE message_id=?", (message_id,)
        )
        self.db.commit()

    def set_last_reminded(self, message_id: str, iso: str) -> None:
        self.db.execute(
            "UPDATE invoices SET last_reminded=? WHERE message_id=?", (iso, message_id)
        )
        self.db.commit()

    def set_calendar_uid(self, message_id: str, cal_uid: str) -> None:
        self.db.execute(
            "UPDATE invoices SET calendar_uid=? WHERE message_id=?", (cal_uid, message_id)
        )
        self.db.commit()

    def pending_message_ids(self) -> set[str]:
        rows = self.db.execute(
            "SELECT message_id FROM invoices WHERE status IN ('pending','review')"
        ).fetchall()
        return {r["message_id"] for r in rows}
