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
    model: str = "claude-haiku-5-5"
    base_url: Optional[str] = None  # for provider=openai, e.g. http://localhost:8000/v1
    api_key_env: str = "ANTHROPIC_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 4000
    # Hard spending cap per calendar month (USD). When it's reached, model calls
    # stop until the 1st: tailoring falls back to offline mode, nothing is
    # drafted, and you get one notification. 0 = no cap.
    monthly_budget_usd: float = 9.0
    # Price per million tokens, used to count spend. Defaults are Claude Haiku 5.5.
    price_input_per_mtok: float = 0.10
    price_output_per_mtok: float = 0.50
    # Optional: a different model per task, to spend more only where it shows.
    # Tasks: score, tailor, letter, answers, outreach, classify.
    task_models: dict[str, str] = Field(default_factory=dict)
    # Optional: prices for those models, {model: [input, output]} per million tokens.
    prices: dict[str, list[float]] = Field(default_factory=dict)
    # Passed through to OpenAI-compatible APIs that support it (e.g. Gemini:
    # "none" | "low" | "medium" | "high"). Lower = fewer billed thinking tokens.
    reasoning_effort: Optional[str] = None

    def price_for(self, model: str) -> tuple[float, float]:
        p = self.prices.get(model)
        return (float(p[0]), float(p[1])) if p and len(p) == 2 else (self.price_input_per_mtok, self.price_output_per_mtok)

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


class ScheduleConfig(BaseModel):
    """Used by `nightshift server`: runs the nightly pipeline by itself."""
    enabled: bool = True
    time: str = "02:17"  # local time, 24h
    timezone: str = "America/New_York"


class NotifyConfig(BaseModel):
    # Free push notifications via the ntfy app (ntfy.sh). Pick a long, random
    # topic name; anyone who knows it can read your notifications.
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    app_url: str = ""  # where the notification opens, e.g. https://your-app.fly.dev


class EmailConfig(BaseModel):
    """Your mailbox, for reading recruiter replies and sending outreach.

    Works with any IMAP/SMTP account. For Gmail or NYU (Google) email, turn on
    2-Step Verification and create an app password at
    myaccount.google.com/apppasswords, then put it in the environment variable
    named by password_env (never in this file)."""
    address: str = ""
    from_name: str = ""
    password_env: str = "NIGHTSHIFT_EMAIL_PASSWORD"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    check_replies: bool = True
    check_every_minutes: int = 120
    lookback_days: int = 30  # how far back the first inbox check looks

    def password(self) -> Optional[str]:
        return os.environ.get(self.password_env)

    def ready(self) -> bool:
        return bool(self.address and self.password())


class OutreachConfig(BaseModel):
    """Emails to recruiters and alumni at companies you've applied to."""
    enabled: bool = False
    auto_send: bool = True  # false = drafts wait in the app until you tap Send
    max_per_day: int = 10  # first emails + follow-ups, across everyone
    max_per_company: int = 2
    follow_up_after_days: int = 6  # one follow-up, only if they haven't replied
    # Find recruiter emails with Hunter.io (free plan: 50 credits/month).
    hunter_api_key_env: str = "HUNTER_API_KEY"
    hunter_monthly_credits: int = 50
    signature: str = ""  # appended to every email, e.g. your LinkedIn URL

    def hunter_key(self) -> Optional[str]:
        return os.environ.get(self.hunter_api_key_env)


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
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)

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
