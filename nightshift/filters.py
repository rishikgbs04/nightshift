"""Cheap, deterministic filters applied before spending any LLM tokens."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import SearchConfig
from .models import Job

_NEG = r"(?:not|unable to|cannot|can't|won't|will not|do not|does not|are not able to|is not able to)"
NO_SPONSORSHIP = re.compile(
    rf"(?:{_NEG}\s+(?:currently\s+)?(?:be\s+able\s+to\s+)?sponsor"
    rf"|{_NEG}\s+(?:currently\s+)?(?:be\s+able\s+to\s+)?(?:provide|offer|support)\s+(?:\w+\s+){{0,3}}?sponsorship"
    r"|without\s+(?:the\s+)?(?:need\s+for\s+|requiring\s+)?(?:current\s+or\s+future\s+)?(?:visa\s+|employer\s+)?sponsorship"
    r"|no\s+(?:visa\s+)?sponsorship"
    r"|sponsorship\s+(?:is\s+)?(?:not|un)\s*available"
    r"|not\s+eligible\s+for\s+(?:visa\s+)?sponsorship"
    r"|(?:u\.?s\.?|us)\s+citizen(?:ship)?\s+(?:is\s+)?required"
    r"|must\s+be\s+a\s+(?:u\.?s\.?|us)\s+citizen"
    r"|(?:active|current)\s+(?:secret|top secret|ts/sci)?\s*(?:security\s+)?clearance\s+(?:is\s+)?required)",
    re.I,
)


def sponsorship_blocker(description: str) -> Optional[str]:
    m = NO_SPONSORSHIP.search(description)
    return m.group(0) if m else None


def _has(text: str, needle: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(needle.lower())}(?![a-z])", text.lower()) is not None


def check(job: Job, s: SearchConfig, now: Optional[datetime] = None) -> tuple[bool, list[str]]:
    """Return (keep, notes). Notes on a kept job are flags shown in the review queue."""
    notes: list[str] = []
    title = job.title
    if s.title_include and not any(_has(title, k) for k in s.title_include):
        return False, ["title doesn't match title_include"]
    hit = next((k for k in s.title_exclude if _has(title, k)), None)
    if hit:
        return False, [f"title contains '{hit}'"]
    if s.locations and not any(_has(job.location, loc) for loc in s.locations):
        return False, [f"location '{job.location}' not in your list"]
    if job.posted_at and s.max_age_days:
        now = now or datetime.now(timezone.utc)
        posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
        if now - posted > timedelta(days=s.max_age_days):
            return False, [f"posted more than {s.max_age_days} days ago"]
    if s.needs_sponsorship and s.sponsorship_policy != "ignore":
        blocker = sponsorship_blocker(job.description)
        if blocker:
            if s.sponsorship_policy == "skip":
                return False, [f"posting says: “{blocker}”"]
            notes.append(f"sponsorship: posting says “{blocker}”")
    return True, notes
