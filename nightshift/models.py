"""Data models: the master profile, jobs, and tailored documents."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------- profile


class Link(BaseModel):
    label: str
    url: str


class Basics(BaseModel):
    name: str
    location: str = ""
    phone: str = ""
    email: str = ""
    links: list[Link] = Field(default_factory=list)


class Bullet(BaseModel):
    id: str
    text: str


class Education(BaseModel):
    id: str
    school: str
    location: str = ""
    degree: str
    gpa: str = ""
    start: str = ""
    end: str = ""
    coursework: list[str] = Field(default_factory=list)


class Entry(BaseModel):
    """A job or a project. Both have bullets; jobs have a company, projects a name."""

    id: str
    title: str  # job title, or project name
    org: str = ""  # company (jobs) or role line (projects)
    location: str = ""
    start: str = ""
    end: str = ""
    link: str = ""
    tech: list[str] = Field(default_factory=list)
    bullets: list[Bullet]


class Extra(BaseModel):
    label: str
    text: str


class Profile(BaseModel):
    """Your master resume. Every fact the tailored resume may contain lives here."""

    basics: Basics
    education: list[Education] = Field(default_factory=list)
    experience: list[Entry] = Field(default_factory=list)
    projects: list[Entry] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    extras: list[Extra] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Profile":
        seen: set[str] = set()
        for entry in [*self.experience, *self.projects]:
            for i in [entry.id, *(b.id for b in entry.bullets)]:
                if i in seen:
                    raise ValueError(f"duplicate id in profile: {i!r}")
                seen.add(i)
        return self

    # ---- lookups
    def entries(self) -> list[Entry]:
        return [*self.experience, *self.projects]

    def entry(self, entry_id: str) -> Optional[Entry]:
        return next((e for e in self.entries() if e.id == entry_id), None)

    def bullet(self, bullet_id: str) -> Optional[Bullet]:
        for e in self.entries():
            for b in e.bullets:
                if b.id == bullet_id:
                    return b
        return None

    def all_skills(self) -> list[str]:
        return [s for group in self.skills.values() for s in group]

    def full_text(self) -> str:
        """Every word in the profile; the universe of facts the guard checks against."""
        parts: list[str] = [self.basics.location]
        for ed in self.education:
            parts += [ed.school, ed.location, ed.degree, f"GPA {ed.gpa}", ed.start, ed.end, *ed.coursework]
        for e in self.entries():
            parts += [e.title, e.org, e.location, e.start, e.end, e.link, *e.tech, *(b.text for b in e.bullets)]
        parts += self.all_skills()
        parts += [x.text for x in self.extras]
        return "\n".join(parts)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


# --------------------------------------------------------------------------- jobs


class Job(BaseModel):
    id: str  # "<source>:<board>:<native id>"
    source: Literal["greenhouse", "lever", "ashby", "manual", "url"]
    company: str
    title: str
    location: str = ""
    url: str = ""
    apply_url: str = ""
    description: str = ""  # plain text
    posted_at: Optional[datetime] = None


# --------------------------------------------------------------------------- tailoring


class TailoredBullet(BaseModel):
    text: str
    sources: list[str]  # ids of master-profile bullets this line is derived from
    original: str = ""  # filled in by the guard for diffs


class TailoredEntry(BaseModel):
    id: str
    bullets: list[TailoredBullet]


class TailoredResume(BaseModel):
    summary: str = ""
    section_order: list[str] = Field(default_factory=lambda: ["experience", "projects"])
    experience: list[TailoredEntry] = Field(default_factory=list)
    projects: list[TailoredEntry] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    coursework: list[str] = Field(default_factory=list)


class Violation(BaseModel):
    where: str
    rule: str
    detail: str
    action: str


class Coverage(BaseModel):
    jd_keywords: list[str]
    matched: list[str]
    missing_but_in_profile: list[str]
    gaps: list[str]  # the JD wants it, your profile doesn't have it -> never added
    score: float  # share of the JD keywords you have that the resume shows
    prominence: float = 0.0  # share that appear near the top of the resume


class ScoreResult(BaseModel):
    score: int = Field(ge=1, le=10)
    reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)


class TailorResult(BaseModel):
    resume: TailoredResume
    cover_letter: str = ""
    violations: list[Violation] = Field(default_factory=list)
    coverage_before: Optional[Coverage] = None
    coverage_after: Optional[Coverage] = None
    doc_kind: Literal["resume", "cv"] = "resume"
    pages: int = 0
