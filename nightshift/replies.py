"""Read the inbox, connect replies to applications and outreach, and tell you
when something needs you (an interview, an assessment, an offer)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from . import notify
from .config import Config
from .db import DB
from .llm import LLM, LLMError
from .mail import Mail, Mailbox, default_since

KINDS = ("interview", "assessment", "offer", "rejection", "received", "referral", "other")
GOOD = {"interview", "assessment", "offer"}

CLASSIFY_SYSTEM = """You sort emails a job seeker received about their applications.

Return JSON: {"kind": one of interview | assessment | offer | rejection | received | referral | other,
"summary": "one short sentence saying what they want or decided, with any date or deadline"}

interview = they want to schedule a call or interview. assessment = online
assessment, coding test or take-home. offer = job offer. rejection = not moving
forward. received = automatic confirmation that the application arrived.
referral = a person replying about a referral or networking request.
other = anything else."""

_PATTERNS = [
    ("offer", r"\boffer (letter|of employment)|pleased to offer|extend (you )?an offer"),
    ("rejection", r"not (be )?moving forward|decided to (move|proceed) (forward )?with other|other candidates"
                  r"|unfortunately|not selected|position has been filled|will not be proceeding|regret to inform"),
    ("assessment", r"online assessment|coding (challenge|assessment|test)|hackerrank|codesignal|codility"
                   r"|take[- ]home|technical assessment"),
    ("interview", r"schedule (a|an|your) (call|interview|chat|time)|interview|availability|calendly|phone screen"
                  r"|next steps?|speak with you|set up a (call|time)"),
    ("received", r"(thank you|thanks) for (applying|your application|your interest)|application (has been )?received"
                 r"|we('ve| have) received your application"),
]

_ATS = re.compile(r"greenhouse|lever\.co|ashbyhq|workday|myworkday|smartrecruiters|icims|jobvite|bamboohr|"
                  r"no-?reply|notifications?@|talent|careers|recruit", re.I)


def classify_offline(m: Mail) -> tuple[str, str]:
    text = f"{m.subject}\n{m.text}"
    for kind, pat in _PATTERNS:
        if re.search(pat, text, re.I):
            first = next((ln.strip() for ln in m.text.splitlines() if len(ln.strip()) > 30), m.subject)
            return kind, first[:200]
    return "other", m.subject[:200]


def classify(llm: Optional[LLM], m: Mail) -> tuple[str, str]:
    if llm is None:
        return classify_offline(m)
    try:
        data = llm.json(CLASSIFY_SYSTEM, f"FROM: {m.from_name} <{m.from_addr}>\nSUBJECT: {m.subject}\n\n{m.text[:3500]}")
        kind = str(data.get("kind", "other")).lower()
        return (kind if kind in KINDS else "other"), str(data.get("summary", ""))[:300]
    except LLMError:
        return classify_offline(m)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


_GENERIC = {"inc", "llc", "labs", "technologies", "technology", "the", "group", "corp", "company", "ai", "io", "hq"}


def company_keys(company: str) -> list[str]:
    words = [_norm(w) for w in re.split(r"[\s\-_.,&]+", company)]
    words = [w for w in words if len(w) >= 3 and w not in _GENERIC]
    joined = _norm(company)
    return list(dict.fromkeys([joined] + words)) if joined else words


def match_job(db: DB, m: Mail) -> Optional[str]:
    """The application this email is about, if we can tell."""
    rows = db.conn.execute(
        "SELECT id, company FROM jobs WHERE status IN ('submitted','needs_human','approved','interview','rejected')"
        " ORDER BY updated_at DESC").fetchall()
    domain = m.from_addr.split("@")[-1]
    domain_core = _norm(domain.rsplit(".", 1)[0]) if "." in domain else _norm(domain)
    head = _norm(f"{m.from_name} {m.subject}")
    body = _norm(m.text[:800])
    best, best_score = None, 0
    for r in rows:
        for key in company_keys(r["company"]):
            score = 0
            if key and key in domain_core:
                score = 3  # e.g. jane@stripe.com
            elif key in head:
                score = 2  # e.g. "Stripe Recruiting <no-reply@greenhouse.io>"
            elif _ATS.search(m.from_addr) and key in body:
                score = 1
            if score > best_score:
                best, best_score = r["id"], score
    return best


def match_contact(db: DB, m: Mail) -> Optional[int]:
    row = db.conn.execute("SELECT id FROM contacts WHERE lower(email) = ?", (m.from_addr,)).fetchone()
    if row:
        return row["id"]
    refs = [r for r in [m.in_reply_to, *m.references] if r]
    if refs:
        q = ",".join("?" * len(refs))
        row = db.conn.execute(f"SELECT id FROM contacts WHERE message_id IN ({q})", refs).fetchone()
        if row:
            return row["id"]
    return None


@dataclass
class InboxReport:
    checked: int = 0
    matched: int = 0
    good_news: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def check(db: DB, cfg: Config, mailbox: Mailbox, llm: Optional[LLM] = None, send_notification: bool = True,
          progress: Callable[[str], None] = lambda s: None) -> InboxReport:
    rep = InboxReport()
    last_uid = int(db.kv_get("inbox_last_uid", "0") or 0)
    since_iso = db.kv_get("inbox_since", "")
    since = datetime.fromisoformat(since_iso) if since_iso else default_since(cfg.email)
    mails = mailbox.fetch_since(since, after_uid=last_uid)
    me = cfg.email.address.lower()
    for m in mails:
        rep.checked += 1
        last_uid = max(last_uid, m.uid)
        if m.from_addr == me or db.conn.execute("SELECT 1 FROM messages WHERE id = ?", (m.message_id,)).fetchone():
            continue
        contact_id = match_contact(db, m)
        job_id = match_job(db, m)
        if contact_id is None and job_id is None:
            continue  # not about your search; nothing is stored
        kind, summary = classify(llm, m)
        if contact_id is not None and kind in ("other", "received"):
            kind = "referral"
        rep.matched += 1
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO messages (id, at, from_addr, from_name, subject, snippet, job_id,"
                      " contact_id, kind, summary) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (m.message_id, m.at.astimezone(timezone.utc).isoformat(timespec="seconds"), m.from_addr,
                       m.from_name, m.subject[:300], m.text[:1200], job_id, contact_id, kind, summary))
        if contact_id is not None:
            db.conn.execute("UPDATE contacts SET status = 'replied', replied_at = ? WHERE id = ?",
                            (m.at.astimezone(timezone.utc).isoformat(timespec="seconds"), contact_id))
            db.conn.commit()
        if job_id:
            row = db.get(job_id)
            if kind in GOOD and row["status"] != "interview":
                db.set(job_id, status="interview")
            elif kind == "rejection" and row["status"] not in ("interview",):
                db.set(job_id, status="rejected")
        if kind in GOOD or kind == "referral":
            company = db.get(job_id)["company"] if job_id else (m.from_name or m.from_addr)
            label = {"interview": "Interview request", "assessment": "Assessment", "offer": "Offer",
                     "referral": "Reply"}[kind]
            rep.good_news.append(f"{label} from {company}: {summary}")
        progress(f"{kind}: {m.subject[:60]}")
    db.kv_set("inbox_last_uid", str(last_uid))
    db.kv_set("inbox_checked_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.kv_set("inbox_since", (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)).isoformat())
    if send_notification and rep.good_news:
        title = rep.good_news[0].split(":")[0] if len(rep.good_news) == 1 else f"{len(rep.good_news)} replies need you"
        notify.send(cfg.notify, title, "\n".join(rep.good_news[:4]), priority="high")
    db.log("inbox", json.dumps({"checked": rep.checked, "matched": rep.matched}))
    return rep
