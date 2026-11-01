"""One lead table plus the lists the Validator reads. A lead's status is both its hand-off and its checkpoint."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from leadh.models import Lead, Status, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    siren TEXT PRIMARY KEY,
    lead_id TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,
    record TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
CREATE TABLE IF NOT EXISTS pages (
    sha256 TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    html TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_runs (
    lead_id TEXT NOT NULL,
    step TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    done_at TEXT NOT NULL,
    PRIMARY KEY (lead_id, step, rules_version)
);
CREATE TABLE IF NOT EXISTS optout (
    kind TEXT NOT NULL CHECK (kind IN ('siren', 'domain')),
    value TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS contacted (
    siren TEXT PRIMARY KEY,
    contacted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    lead_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    reason_code TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    lead_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # Leads

    def has(self, siren: str) -> bool:
        return self.db.execute("SELECT 1 FROM leads WHERE siren = ?", (siren,)).fetchone() is not None

    def next_lead_id(self) -> str:
        row = self.db.execute("SELECT MAX(CAST(SUBSTR(lead_id, 3) AS INTEGER)) FROM leads").fetchone()
        return f"L-{(row[0] or 0) + 1:06d}"

    def save(self, lead: Lead) -> None:
        lead.updated_at = now()
        self.db.execute(
            "INSERT INTO leads (siren, lead_id, status, record, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(siren) DO UPDATE SET status = excluded.status, record = excluded.record, "
            "updated_at = excluded.updated_at",
            (lead.siren, lead.lead_id, lead.status.value, lead.model_dump_json(), lead.updated_at.isoformat()))
        self.db.commit()

    def get(self, lead_id: str) -> Lead | None:
        row = self.db.execute("SELECT record FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return Lead.model_validate_json(row["record"]) if row else None

    def by_status(self, status: Status, limit: int | None = None) -> list[Lead]:
        sql = "SELECT record FROM leads WHERE status = ? ORDER BY lead_id"
        args: tuple[object, ...] = (status.value,)
        if limit is not None:
            sql += " LIMIT ?"
            args += (limit,)
        return [Lead.model_validate_json(r["record"]) for r in self.db.execute(sql, args)]

    def all(self) -> Iterator[Lead]:
        for r in self.db.execute("SELECT record FROM leads ORDER BY lead_id"):
            yield Lead.model_validate_json(r["record"])

    def status_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")}

    def delete(self, siren: str) -> None:
        self.db.execute("DELETE FROM leads WHERE siren = ?", (siren,))
        self.db.commit()

    # Pages

    def put_page(self, sha256: str, url: str, html: str, text: str, fetched_at: datetime) -> None:
        self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?)",
                        (sha256, url, html, text, fetched_at.isoformat()))
        self.db.commit()

    def page(self, sha256: str) -> tuple[str, str] | None:
        """(html, text) of a stored page, or None once it has expired and been purged."""
        row = self.db.execute("SELECT html, text FROM pages WHERE sha256 = ?", (sha256,)).fetchone()
        return (row["html"], row["text"]) if row else None

    def purge_pages(self, older_than: datetime) -> int:
        cur = self.db.execute("DELETE FROM pages WHERE fetched_at < ?", (older_than.isoformat(),))
        self.db.commit()
        return cur.rowcount

    # Idempotency: one row per (lead, step, rules version)

    def step_done(self, lead_id: str, step: str, rules_version: str) -> bool:
        return self.db.execute("SELECT 1 FROM step_runs WHERE lead_id = ? AND step = ? AND rules_version = ?",
                               (lead_id, step, rules_version)).fetchone() is not None

    def mark_step(self, lead_id: str, step: str, rules_version: str, run_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO step_runs VALUES (?, ?, ?, ?, ?)",
                        (lead_id, step, rules_version, run_id, now().isoformat()))
        self.db.commit()

    def clear_steps(self, lead_id: str) -> None:
        self.db.execute("DELETE FROM step_runs WHERE lead_id = ?", (lead_id,))
        self.db.commit()

    # Opt-out list and contacted ledger: read here, written only by the Reply handler, the Sender and you.

    def add_optout(self, kind: str, value: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO optout VALUES (?, ?, ?)", (kind, value.lower(), now().isoformat()))
        self.db.commit()

    def opted_out(self, siren: str, domain: str | None) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM optout WHERE (kind = 'siren' AND value = ?) OR (kind = 'domain' AND value = ?)",
            (siren, (domain or "").lower())).fetchone()
        return row is not None

    def add_contacted(self, siren: str, at: datetime | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO contacted VALUES (?, ?)", (siren, (at or now()).isoformat()))
        self.db.commit()

    def contacted(self, siren: str) -> bool:
        return self.db.execute("SELECT 1 FROM contacted WHERE siren = ?", (siren,)).fetchone() is not None

    # Human decisions on CONDITIONAL leads

    def log_decision(self, lead_id: str, decision: str, reason_code: str) -> None:
        self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)",
                        (lead_id, decision, reason_code, now().isoformat()))
        self.db.commit()

    def decisions(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM decisions ORDER BY decided_at, rowid"))

    # Run events feed the daily report. Never names or addresses: lead IDs and reason codes only.

    def event(self, run_id: str, step: str, kind: str, lead_id: str | None = None, detail: str = "") -> None:
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                        (run_id, step, lead_id, kind, detail, now().isoformat()))
        self.db.commit()

    def events(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY rowid", (run_id,)))

    def handed_off_on(self, day: str) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM events WHERE step = 'handoff' AND kind = 'out' AND at LIKE ?",
                              (f"{day}%",)).fetchone()
        return int(row[0])


def expired(fetched_at: datetime, days: int) -> bool:
    return now() - fetched_at > timedelta(days=days)
