"""SQLite store: every job seen, where it is in the pipeline, and what happened."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import Job

# discovered -> filtered_out | scored -> low_score | queued -> approved | skipped
# approved -> submitted | needs_human | failed
# submitted -> interview | rejected (from recruiter replies)
STATUSES = ["discovered", "filtered_out", "scored", "low_score", "queued", "approved", "skipped",
            "submitted", "needs_human", "failed", "interview", "rejected"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  location TEXT,
  url TEXT,
  apply_url TEXT,
  description TEXT,
  posted_at TEXT,
  discovered_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'discovered',
  fit REAL,
  score INTEGER,
  score_json TEXT,
  notes_json TEXT,
  out_dir TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,           -- the email's Message-ID
  at TEXT NOT NULL,
  from_addr TEXT, from_name TEXT, subject TEXT, snippet TEXT,
  job_id TEXT, contact_id INTEGER,
  kind TEXT,                     -- interview | assessment | offer | rejection | received | referral | other
  summary TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  name TEXT, title TEXT, company TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'recruiter',   -- recruiter | alumni | other
  source TEXT NOT NULL DEFAULT 'manual',    -- manual | hunter
  note TEXT,                                -- how you know them, e.g. "NYU MS '24"
  job_id TEXT,
  status TEXT NOT NULL DEFAULT 'new',       -- new | drafted | sent | followed_up | replied | skipped | failed
  subject TEXT, body TEXT, message_id TEXT,
  sent_at TEXT, followed_up_at TEXT, replied_at TEXT, error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (
  at TEXT NOT NULL,
  job_id TEXT,
  kind TEXT NOT NULL,
  detail TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def upsert_new(self, job: Job) -> bool:
        """Insert if unseen. Returns True when the job is new."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs (id, source, company, title, location, url, apply_url, description,"
                " posted_at, discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.source, job.company, job.title, job.location, job.url, job.apply_url,
                 job.description, job.posted_at.isoformat() if job.posted_at else None, _now(), _now()),
            )
            return cur.rowcount == 1

    def set(self, job_id: str, **fields) -> None:
        for k in ("score_json", "notes_json"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        if "status" in fields:
            assert fields["status"] in STATUSES, fields["status"]
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def log(self, kind: str, detail: str = "", job_id: Optional[str] = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events (at, job_id, kind, detail) VALUES (?,?,?,?)", (_now(), job_id, kind, detail))

    def get(self, job_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def by_status(self, *statuses: str, order: str = "score DESC, fit DESC, discovered_at DESC",
                  limit: int = 500) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY {order} LIMIT ?", (*statuses, limit)).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def kv_get(self, k: str, default: str = "") -> str:
        row = self.conn.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, k: str, v: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    def events_since(self, kind: str, since_iso: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events WHERE kind = ? AND at >= ?", (kind, since_iso)).fetchone()[0]

    def events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()


def row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"], source=row["source"], company=row["company"], title=row["title"],
        location=row["location"] or "", url=row["url"] or "", apply_url=row["apply_url"] or "",
        description=row["description"] or "",
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
    )
