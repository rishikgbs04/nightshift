"""The nightly run: discover -> filter -> pre-rank -> score -> tailor -> queue (-> submit approved)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import filters, keywords, sources
from .config import Config
from .db import DB, row_to_job
from .llm import LLM, LLMError
from .models import Job, Profile
from .render import PDFRenderer
from .tailor import score_job, tailor


@dataclass
class RunReport:
    started: datetime = field(default_factory=datetime.now)
    discovered: int = 0
    new: int = 0
    filtered_out: int = 0
    scored: int = 0
    queued: int = 0
    submitted: int = 0
    needs_human: int = 0
    errors: list[str] = field(default_factory=list)
    top: list[tuple[int, str, str]] = field(default_factory=list)

    def markdown(self) -> str:
        lines = [
            f"# nightshift run, {self.started:%a %b %-d %Y %H:%M}",
            "",
            f"- postings seen: {self.discovered} ({self.new} new)",
            f"- filtered out: {self.filtered_out}",
            f"- scored by the model: {self.scored}",
            f"- **ready for your review: {self.queued}**",
        ]
        if self.submitted or self.needs_human:
            lines.append(f"- submitted: {self.submitted}, need you to finish: {self.needs_human}")
        if self.top:
            lines += ["", "## Best matches", ""]
            lines += [f"- {s}/10  {company}: {title}" for s, company, title in self.top[:10]]
        if self.errors:
            lines += ["", "## Problems", ""] + [f"- {e}" for e in self.errors[:30]]
        return "\n".join(lines) + "\n"


def job_dir(cfg: Config, job: Job) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{job.company}-{job.title}").strip("-")[:80]
    return cfg.out_dir / f"{safe}-{hashlib.sha1(job.id.encode()).hexdigest()[:7]}"


def ingest(db: DB, jobs: list[Job], cfg: Config, report: RunReport) -> None:
    for job in jobs:
        report.discovered += 1
        if not db.upsert_new(job):
            continue
        report.new += 1
        keep, notes = filters.check(job, cfg.search)
        if not keep:
            db.set(job.id, status="filtered_out", notes_json=notes)
            report.filtered_out += 1
            continue
        db.set(job.id, fit=keywords.profile_fit(job.description, profile_cache(cfg)), notes_json=notes)


_profiles: dict[tuple[str, float], Profile] = {}


def profile_cache(cfg: Config) -> Profile:
    path = cfg.resolve(cfg.profile_path)
    key = (str(path), path.stat().st_mtime)
    if key not in _profiles:
        _profiles[key] = Profile.load(path)
    return _profiles[key]


def score_and_tailor(db: DB, cfg: Config, llm: Optional[LLM], profile: Profile, report: RunReport,
                     progress: Callable[[str], None] = lambda s: None) -> None:
    pc = cfg.pipeline
    # cheapest first: rank by keyword fit, only send the best to the model
    pending = db.by_status("discovered", order="fit DESC", limit=pc.max_llm_scoring_per_run)
    for row in pending:
        job = row_to_job(row)
        try:
            s = score_job(llm, profile, job)
        except LLMError as e:
            report.errors.append(f"scoring {job.company} / {job.title}: {e}")
            continue
        report.scored += 1
        status = "scored" if s["score"] >= pc.min_score else "low_score"
        db.set(job.id, status=status, score=s["score"], score_json=s)
        progress(f"scored {s['score']}/10  {job.company}: {job.title}")

    to_tailor = db.by_status("scored", order="score DESC, fit DESC", limit=pc.max_tailor_per_run)
    if not to_tailor:
        return
    with PDFRenderer(cfg.submit.browser_channel, cfg.submit.browser_executable) as renderer:
        for row in to_tailor:
            job = row_to_job(row)
            out = job_dir(cfg, job)
            try:
                res = tailor(job, profile, cfg, llm, out, renderer=renderer)
            except Exception as e:  # noqa: BLE001 - one bad posting never stops the night
                db.set(job.id, status="failed", error=f"tailoring: {type(e).__name__}: {e}"[:500])
                report.errors.append(f"tailoring {job.company} / {job.title}: {e}")
                continue
            notes = json.loads(row["notes_json"] or "[]")
            notes += [f"guard: {v.where}: {v.detail} ({v.action})" for v in res.violations]
            db.set(job.id, status="queued", out_dir=str(out), notes_json=notes)
            report.queued += 1
            report.top.append((row["score"], job.company, job.title))
            progress(f"tailored {job.company}: {job.title}")
    report.top.sort(reverse=True)


def run(cfg: Config, llm: Optional[LLM], db: Optional[DB] = None,
        jobs: Optional[list[Job]] = None, progress: Callable[[str], None] = lambda s: None) -> RunReport:
    """One full night. `jobs` overrides discovery (used by tests and `import`)."""
    db = db or DB(cfg.db_path)
    report = RunReport()
    profile = profile_cache(cfg)

    if cfg.submit.enabled:
        from .submit import submit_approved
        sub = submit_approved(db, cfg, profile, progress=progress)
        report.submitted, report.needs_human = sub.submitted, sub.needs_human
        report.errors += sub.errors

    if jobs is None:
        jobs, errs = sources.discover(cfg.companies)
        report.errors += errs
    ingest(db, jobs, cfg, report)
    score_and_tailor(db, cfg, llm, profile, report, progress)

    digest = report.markdown()
    (cfg.home / "data").mkdir(parents=True, exist_ok=True)
    (cfg.home / "data" / "last_run.md").write_text(digest)
    db.log("run", json.dumps({k: getattr(report, k) for k in ("discovered", "new", "filtered_out",
                                                               "scored", "queued", "submitted")}))
    return report
