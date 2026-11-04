"""Referral and recruiter outreach for jobs you've applied to.

For each application: find a recruiter (Hunter.io) and use any alumni you've
added at that company, draft a short personal email from your profile and the
job, run it through the same guard as your resume, and send it within a daily
cap. One follow-up at most, never after a reply, never to the same person twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

from . import guard
from .config import Config
from .db import DB, row_to_job
from .llm import LLM, LLMError
from .mail import MailError, Sender, compose
from .models import Job, Profile
from .tailor import _profile_for_prompt

RECRUITING = re.compile(r"recruit|talent|university|campus|early career|people|hr\b|human resources|sourc", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- Hunter.io


class Hunter:
    """Domain Search: recruiting-team emails at a company. 1 credit per search."""

    def __init__(self, api_key: str, client: Optional[httpx.Client] = None):
        self.key = api_key
        self.client = client or httpx.Client(timeout=30)

    def recruiters(self, company: str, limit: int = 5) -> list[dict]:
        r = self.client.get("https://api.hunter.io/v2/domain-search", params={
            "company": company, "department": "hr", "type": "personal", "limit": limit, "api_key": self.key})
        if r.status_code == 429:
            raise RuntimeError("Hunter.io limit reached for now")
        if r.status_code >= 400:
            raise RuntimeError(f"Hunter.io error {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or {}
        out = []
        for e in data.get("emails") or []:
            if not e.get("value") or (e.get("confidence") or 0) < 70:
                continue
            name = " ".join(x for x in [e.get("first_name"), e.get("last_name")] if x)
            out.append({"email": e["value"].lower(), "name": name, "title": e.get("position") or ""})
        # recruiters first
        out.sort(key=lambda c: 0 if RECRUITING.search(c["title"]) else 1)
        return out


def hunter_credits_used(db: DB) -> int:
    return int(db.kv_get(f"hunter_credits_{datetime.now().strftime('%Y-%m')}", "0") or 0)


def _use_hunter_credit(db: DB) -> None:
    db.kv_set(f"hunter_credits_{datetime.now().strftime('%Y-%m')}", str(hunter_credits_used(db) + 1))


# ---------------------------------------------------------------------- contacts


def add_contact(db: DB, email: str, company: str, name: str = "", title: str = "", kind: str = "alumni",
                note: str = "", source: str = "manual", job_id: Optional[str] = None) -> Optional[int]:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        raise ValueError("That doesn't look like an email address")
    with db.tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO contacts (email, name, title, company, kind, source, note, job_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (email, name.strip(), title.strip(), company.strip(), kind, source, note.strip(), job_id, _now()))
        return cur.lastrowid if cur.rowcount else None


def _jobs_needing_outreach(db: DB) -> list:
    return db.conn.execute(
        "SELECT * FROM jobs WHERE status IN ('submitted', 'interview') ORDER BY score DESC, updated_at DESC").fetchall()


def _company_contacts(db: DB, company: str) -> list:
    return db.conn.execute("SELECT * FROM contacts WHERE lower(company) = lower(?)", (company,)).fetchall()


# ---------------------------------------------------------------------- drafting

DRAFT_SYSTEM = """You write a short, personal networking email from a job seeker.

The candidate just applied to the role below. Write to the recipient described.
- 80-130 words, plain text, warm and specific, no flattery, no buzzwords.
- Mention the exact role and that they applied.
- One or two concrete things the candidate has done that fit this role, taken
  ONLY from the profile. Never invent numbers, skills, employers or outcomes.
- For a recruiter: ask whether they're the right person for this role and if
  they'd look at the application. For an alum or contact: mention the shared
  connection given in NOTE, and ask for 15 minutes or a referral if they feel
  comfortable.
- End with the candidate's first name only (a signature is added after).

Return JSON: {"subject": "under 60 characters", "body": "the email"}"""

FOLLOWUP_SYSTEM = """Write a two-sentence, polite follow-up to the email below, from the same
person, adding nothing new about them. Plain text. Return JSON: {"body": "..."}"""


def _first_name(full: str) -> str:
    return (full or "").split()[0] if full else ""


def draft_offline(profile: Profile, job: Job, contact) -> tuple[str, str]:
    me = _first_name(profile.basics.name)
    hi = f"Hi {_first_name(contact['name'])}," if contact["name"] else "Hi,"
    proj = profile.projects[0] if profile.projects else None
    line = proj.bullets[0].text if proj and proj.bullets else ""
    if contact["kind"] == "recruiter":
        ask = (f"I just applied for the {job.title} role at {job.company}. Would you be the right person to "
               "take a look, or point me to whoever is hiring for it?")
    else:
        conn = f" ({contact['note']})" if contact["note"] else ""
        ask = (f"I'm reaching out because we share a connection{conn}. I just applied for the {job.title} role at "
               f"{job.company} and would really value 15 minutes to hear about your experience there, or a "
               "referral if you're comfortable.")
    body = f"{hi}\n\n{ask}\n\n"
    if line:
        body += f"Recently I {line[0].lower() + line[1:]}.\n\n"
    body += f"Thanks for your time,\n{me}"
    return f"{job.title} application, {profile.basics.name}", body


def draft(llm: Optional[LLM], profile: Profile, job: Job, contact) -> tuple[str, str, list]:
    if llm is None:
        s, b = draft_offline(profile, job, contact)
        return s, b, []
    who = (f"RECIPIENT: {contact['name'] or 'unknown name'}, {contact['title'] or contact['kind']} at {job.company}"
           f" ({contact['kind']})\nNOTE: {contact['note'] or 'none'}")
    user = (f"{who}\n\nROLE: {job.title} at {job.company}\n\nJOB DESCRIPTION:\n{job.description[:5000]}\n\n"
            f"CANDIDATE PROFILE:\n{_profile_for_prompt(profile)}")
    try:
        data = llm.json(DRAFT_SYSTEM, user)
        subject, body = str(data.get("subject", "")).strip()[:90], str(data.get("body", "")).strip()
    except LLMError:
        s, b = draft_offline(profile, job, contact)
        return s, b, []
    cleaned, violations = guard.enforce_letter(body, profile)
    if len(cleaned.split()) < 30:  # guard removed too much: use the safe template
        s, b = draft_offline(profile, job, contact)
        return s, b, violations
    return subject or f"{job.title} application", cleaned, violations


def _with_signature(body: str, cfg: Config) -> str:
    sig = cfg.outreach.signature.strip()
    return f"{body}\n\n{sig}" if sig else body


# ---------------------------------------------------------------------- the nightly pass


@dataclass
class OutreachReport:
    found: int = 0
    drafted: int = 0
    sent: int = 0
    followed_up: int = 0
    errors: list[str] = field(default_factory=list)


def sent_today(db: DB) -> int:
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.events_since("outreach_sent", midnight.astimezone(timezone.utc).isoformat(timespec="seconds"))


def run(db: DB, cfg: Config, profile: Profile, sender: Optional[Sender], llm: Optional[LLM] = None,
        hunter: Optional[Hunter] = None, progress: Callable[[str], None] = lambda s: None) -> OutreachReport:
    oc = cfg.outreach
    rep = OutreachReport()
    if hunter is None and oc.hunter_key():
        hunter = Hunter(oc.hunter_key())

    # 1) find recruiters for companies you applied to (one Hunter search per company, ever)
    for row in _jobs_needing_outreach(db):
        company = row["company"]
        if len(_company_contacts(db, company)) >= oc.max_per_company:
            continue
        searched = db.kv_get(f"hunter_searched:{company.lower()}")
        if hunter and not searched and hunter_credits_used(db) < oc.hunter_monthly_credits:
            try:
                found = hunter.recruiters(company)
                _use_hunter_credit(db)
                db.kv_set(f"hunter_searched:{company.lower()}", _now())
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"finding recruiters at {company}: {e}")
                found = []
            for c in found[: max(0, oc.max_per_company - len(_company_contacts(db, company)))]:
                if add_contact(db, c["email"], company, c["name"], c["title"], kind="recruiter", source="hunter",
                               job_id=row["id"]):
                    rep.found += 1

    # 2) draft for every new contact at a company you applied to
    jobs_by_company = {}
    for row in _jobs_needing_outreach(db):
        jobs_by_company.setdefault(row["company"].lower(), row)
    for c in db.conn.execute("SELECT * FROM contacts WHERE status = 'new'").fetchall():
        jrow = db.get(c["job_id"]) if c["job_id"] else jobs_by_company.get(c["company"].lower())
        if jrow is None:
            continue  # alumni at a company you haven't applied to yet: wait
        subject, body, _ = draft(llm, profile, row_to_job(jrow), c)
        db.conn.execute("UPDATE contacts SET status = 'drafted', subject = ?, body = ?, job_id = ? WHERE id = ?",
                        (subject, body, jrow["id"], c["id"]))
        db.conn.commit()
        rep.drafted += 1

    if sender is None:
        return rep
    budget = oc.max_per_day - sent_today(db)

    # 3) follow-ups first (they're time-sensitive), then first emails
    cutoff = (datetime.now(timezone.utc) - timedelta(days=oc.follow_up_after_days)).isoformat(timespec="seconds")
    due = db.conn.execute("SELECT * FROM contacts WHERE status = 'sent' AND sent_at <= ? ORDER BY sent_at",
                          (cutoff,)).fetchall()
    for c in due:
        if budget <= 0:
            break
        body = f"Hi {_first_name(c['name'])}," if c["name"] else "Hi,"
        body += ("\n\nI wanted to follow up on my note below in case it got buried. I'd really appreciate any "
                 f"pointers on the role.\n\nThanks,\n{_first_name(profile.basics.name)}")
        if llm is not None:
            try:
                data = llm.json(FOLLOWUP_SYSTEM, f"ORIGINAL EMAIL:\n{c['body']}")
                cand, _ = guard.enforce_letter(str(data.get("body", "")), profile)
                if len(cand.split()) >= 12:
                    body = cand
            except LLMError:
                pass
        quoted = "\n".join("> " + ln for ln in (c["body"] or "").splitlines())
        msg = compose(cfg.email, c["email"], c["name"], f"Re: {c['subject']}",
                      _with_signature(body, cfg) + f"\n\n{quoted}", in_reply_to=c["message_id"] or "")
        try:
            sender.send(msg)
        except MailError as e:
            rep.errors.append(f"follow-up to {c['email']}: {e}")
            break
        db.conn.execute("UPDATE contacts SET status = 'followed_up', followed_up_at = ? WHERE id = ?", (_now(), c["id"]))
        db.conn.commit()
        db.log("outreach_sent", f"follow-up {c['email']}", c["job_id"])
        rep.followed_up += 1
        budget -= 1

    if oc.auto_send:
        ready = db.conn.execute("SELECT * FROM contacts WHERE status = 'drafted' ORDER BY created_at").fetchall()
    else:
        ready = db.conn.execute("SELECT * FROM contacts WHERE status = 'approved' ORDER BY created_at").fetchall()
    for c in ready:
        if budget <= 0:
            break
        msg = compose(cfg.email, c["email"], c["name"], c["subject"], _with_signature(c["body"], cfg))
        try:
            sender.send(msg)
        except MailError as e:
            db.conn.execute("UPDATE contacts SET error = ? WHERE id = ?", (str(e)[:300], c["id"]))
            db.conn.commit()
            rep.errors.append(f"email to {c['email']}: {e}")
            break  # usually a login problem: stop instead of failing 10 times
        db.conn.execute("UPDATE contacts SET status = 'sent', sent_at = ?, message_id = ?, error = NULL WHERE id = ?",
                        (_now(), msg["Message-ID"], c["id"]))
        db.conn.commit()
        db.log("outreach_sent", c["email"], c["job_id"])
        rep.sent += 1
        budget -= 1
        progress(f"emailed {c['name'] or c['email']} at {c['company']}")
    return rep
