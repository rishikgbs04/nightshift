"""Job sources.

Uses the public, documented job-board APIs that Greenhouse, Lever and Ashby
expose for each company's careers page. No scraping, no logins, nothing
against anyone's terms of service, and every posting comes with the full
description text.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urlparse

import httpx

from ..models import Job

UA = "nightshift-job-agent/0.1 (+https://github.com/rishikgbs04/nightshift)"


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    # Greenhouse double-escapes its HTML ("&lt;p&gt;")
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    p = _Text()
    p.feed(raw)
    text = html.unescape("".join(p.out))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):  # Lever: epoch millis
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _pretty(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


# --------------------------------------------------------------------------- parsers (pure)


def parse_greenhouse(board: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(Job(
            id=f"greenhouse:{board}:{j['id']}",
            source="greenhouse",
            company=(j.get("company_name") or _pretty(board)),
            title=j.get("title", "").strip(),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""),
            description=html_to_text(j.get("content", "")),
            posted_at=_dt(j.get("first_published") or j.get("updated_at")),
        ))
    return jobs


def parse_lever(company: str, data: list) -> list[Job]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description", ""))]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content", "")))
        parts.append(j.get("additionalPlain") or html_to_text(j.get("additional", "")))
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append(Job(
            id=f"lever:{company}:{j['id']}",
            source="lever",
            company=_pretty(company),
            title=j.get("text", "").strip(),
            location=loc,
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl") or (j.get("hostedUrl", "") + "/apply"),
            description="\n\n".join(p for p in parts if p).strip(),
            posted_at=_dt(j.get("createdAt")),
        ))
    return jobs


def parse_ashby(board: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "") or ""
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append(Job(
            id=f"ashby:{board}:{j.get('id') or j.get('jobUrl')}",
            source="ashby",
            company=_pretty(board),
            title=j.get("title", "").strip(),
            location=loc,
            url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            posted_at=_dt(j.get("publishedAt")),
        ))
    return jobs


# --------------------------------------------------------------------------- fetchers


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)


def fetch_greenhouse(board: str, client: httpx.Client) -> list[Job]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    r.raise_for_status()
    return parse_greenhouse(board, r.json())


def fetch_lever(company: str, client: httpx.Client) -> list[Job]:
    r = client.get(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    r.raise_for_status()
    return parse_lever(company, r.json())


def fetch_ashby(board: str, client: httpx.Client) -> list[Job]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", params={"includeCompensation": "true"})
    r.raise_for_status()
    return parse_ashby(board, r.json())


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def discover(companies, client: Optional[httpx.Client] = None) -> tuple[list[Job], list[str]]:
    """Fetch every configured board. Returns (jobs, errors); one bad board never stops the run."""
    client = client or _client()
    jobs: list[Job] = []
    errors: list[str] = []
    for source, fetch in FETCHERS.items():
        for slug in getattr(companies, source):
            try:
                jobs.extend(fetch(slug, client))
            except Exception as e:  # noqa: BLE001 - report and continue
                errors.append(f"{source}:{slug}: {type(e).__name__}: {str(e)[:120]}")
    return jobs, errors


def check_boards(companies, client: Optional[httpx.Client] = None) -> Iterable[tuple[str, str, str]]:
    """Yield (source, slug, status) so you can find typos in config.yaml."""
    client = client or _client()
    for source, fetch in FETCHERS.items():
        for slug in getattr(companies, source):
            try:
                n = len(fetch(slug, client))
                yield source, slug, f"ok ({n} open roles)"
            except httpx.HTTPStatusError as e:
                yield source, slug, f"HTTP {e.response.status_code} (wrong slug?)"
            except Exception as e:  # noqa: BLE001
                yield source, slug, f"error: {type(e).__name__}"


# --------------------------------------------------------------------------- single posting by URL


_GH = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)(?:/jobs/|.*?token=)(\d+)")
_LV = re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})")
_AB = re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})")


def job_from_url(url: str, client: Optional[httpx.Client] = None) -> Job:
    """Load one posting from its URL. Greenhouse/Lever/Ashby links use their
    APIs; anything else is fetched and converted to text."""
    client = client or _client()
    if m := _GH.search(url):
        board, jid = m.groups()
        r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}")
        r.raise_for_status()
        return parse_greenhouse(board, {"jobs": [r.json()]})[0]
    if m := _LV.search(url):
        company, jid = m.groups()
        r = client.get(f"https://api.lever.co/v0/postings/{company}/{jid}")
        r.raise_for_status()
        return parse_lever(company, [r.json()])[0]
    if m := _AB.search(url):
        board, jid = m.groups()
        for j in fetch_ashby(board, client):
            if jid in j.id or jid in j.url:
                return j
        raise LookupError(f"posting {jid} not found on Ashby board {board}")
    r = client.get(url)
    r.raise_for_status()
    text = html_to_text(r.text)
    title_m = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
    host = urlparse(url).hostname or "company"
    return Job(id=f"url:{url}", source="url", company=_pretty(host.split(".")[-2] if "." in host else host),
               title=html.unescape(title_m.group(1).strip()) if title_m else "Job posting",
               url=url, apply_url=url, description=text)
