"""User configuration (config.yaml)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    # "anthropic" | "openai" (any OpenAI-compatible endpoint, e.g. a local gateway) | "offline"
    provider: Literal["anthropic", "openai", "offline"] = "anthropic"
    model: str = "claude-sonnet-5-5"
    base_url: Optional[str] = None  # for provider=openai, e.g. http://localhost:8000/v1
    api_key_env: str = "ANTHROPIC_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 4000

    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)


class SearchConfig(BaseModel):
    title_include: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)  # empty = anywhere
    max_age_days: int = 30
    # Some postings say they won't sponsor work visas. Set needs_sponsorship
    # to true if you'll need one, and pick what to do with those postings.
    needs_sponsorship: bool = False
    sponsorship_policy: Literal["skip", "flag", "ignore"] = "flag"


class Companies(BaseModel):
    # Also watch the bundled list in nightshift/data/companies.yaml (a few hundred
    # tech companies). Run `nightshift boards --prune` once to drop dead slugs.
    include_defaults: bool = False
    exclude: list[str] = Field(default_factory=list)  # slugs to never use
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)

    def resolved(self, home: Optional[Path] = None) -> "Companies":
        """Your list plus the bundled one (if enabled), minus exclusions and
        boards that `nightshift boards --prune` found dead."""
        out = {k: list(getattr(self, k)) for k in ("greenhouse", "lever", "ashby")}
        if self.include_defaults:
            bundled = yaml.safe_load((Path(__file__).parent / "data" / "companies.yaml").read_text()) or {}
            for k in out:
                out[k] += [str(s) for s in bundled.get(k, [])]
        dead = set(dead_boards(home)) if home else set()
        for k in out:
            seen: set[str] = set()
            out[k] = [s for s in out[k] if not (s in seen or seen.add(s))
                      and s not in self.exclude and f"{k}:{s}" not in dead]
        return Companies(**out)


def dead_boards_path(home: Path) -> Path:
    return home / "data" / "dead_boards.txt"


def dead_boards(home: Path) -> list[str]:
    p = dead_boards_path(home)
    return [ln.strip() for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


class TailorConfig(BaseModel):
    resume_pages: int = 1
    cv_pages: int = 2
    # Jobs in these countries get a 2-page CV instead of a 1-page resume.
    cv_regions: list[str] = Field(
        default_factory=lambda: [
            "United Kingdom", "UK", "London", "Ireland", "Dublin", "Australia",
            "Sydney", "Melbourne", "New Zealand", "Auckland", "Germany", "Berlin",
            "Netherlands", "Amsterdam",
        ]
    )
    write_summary: bool = False
    cover_letter: bool = True


class PipelineConfig(BaseModel):
    max_llm_scoring_per_run: int = 60
    max_tailor_per_run: int = 25
    min_score: int = 6
    # Jobs scoring at least this go straight to "approved" (and get submitted the
    # same night if submit.enabled). None = you approve everything by hand.
    auto_approve_min_score: Optional[int] = None
    # Never auto-approve a job carrying a flag (e.g. "won't sponsor") or whose
    # tailoring fell back to offline because the model failed.
    auto_approve_clean_only: bool = True
    # Don't flood one company: at most this many new applications per company per night.
    max_per_company_per_run: int = 3


class SubmitConfig(BaseModel):
    enabled: bool = False  # submit approved applications on each run
    live: bool = False  # False = fill forms and screenshot, never click submit
    max_per_day: int = 50  # hard cap on forms filled per calendar day
    # Random pause between applications, in seconds, so submissions are spread
    # out instead of arriving in one burst.
    delay_seconds: tuple[int, int] = (45, 180)
    # Let the LLM draft answers to open-ended questions ("Why do you want to work
    # here?") from your profile and the posting, checked by the guard. Factual
    # questions (visa, salary, dates) are only ever answered from answers.yaml.
    answer_open_questions: bool = True
    browser_channel: Optional[str] = "chrome"  # use installed Chrome; None = Playwright Chromium
    browser_executable: Optional[str] = None
    headless: bool = True


class Config(BaseModel):
    home: Path = Path(".")
    profile_path: Path = Path("profile.yaml")
    answers_path: Path = Path("answers.yaml")
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    companies: Companies = Field(default_factory=Companies)
    tailor: TailorConfig = Field(default_factory=TailorConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    submit: SubmitConfig = Field(default_factory=SubmitConfig)

    @property
    def db_path(self) -> Path:
        return self.home / "data" / "nightshift.db"

    @property
    def out_dir(self) -> Path:
        return self.home / "data" / "applications"

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else self.home / p

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        path = Path(path).resolve()
        raw = yaml.safe_load(path.read_text()) or {}
        raw.setdefault("home", str(path.parent))
        return cls.model_validate(raw)
