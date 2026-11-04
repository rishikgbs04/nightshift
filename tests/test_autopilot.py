"""Auto-approve, per-company cap, daily submission cap, and drafted answers."""

import json
from pathlib import Path

import pytest
from conftest import EXAMPLE_PROFILE, FIX, FakeLLM

from nightshift.config import Companies, dead_boards_path
from nightshift.db import DB
from nightshift.models import Job
from nightshift.pipeline import run
from nightshift.models import Profile
from nightshift.submit import is_open_ended, make_drafter
from nightshift.tailor import draft_offline

JD = (FIX / "jd_ai_platform.txt").read_text()
FORM = (FIX / "apply_form.html").as_uri()


def _jobs(n_per_company=1, companies=("Acme", "Globex", "Initech")):
    jobs = []
    for c in companies:
        for i in range(n_per_company):
            jobs.append(Job(id=f"manual:{c}-{i}", source="manual", company=c, title=f"Software Engineer {i}",
                            location="New York, NY", url=FORM, apply_url=FORM, description=JD))
    return jobs


def _llm(score=9, why="I built a retrieval-augmented Q&A assistant over 400 pages of lecture notes using "
                      "embeddings and FAISS, and I want to keep building tools like that on your AI platform. "
                      "I also have run Kubernetes clusters in production for years."):
    draft = draft_offline(Profile.load(EXAMPLE_PROFILE), JD).model_dump()

    def reply(user):
        if user.startswith("MASTER PROFILE"):
            return draft  # tailoring
        return {"score": score, "reasons": ["fit"], "red_flags": []}  # scoring
    llm = FakeLLM([reply], text_reply=why)
    return llm


@pytest.fixture
def auto_cfg(cfg):
    cfg.search.title_include = []
    cfg.search.locations = []
    cfg.search.max_age_days = 0
    cfg.pipeline.min_score = 5
    cfg.pipeline.auto_approve_min_score = 8
    cfg.submit.delay_seconds = (0, 0)
    (cfg.home / "answers.yaml").write_text(
        '- match: "sponsor"\n  value: "No"\n- match: "how did you hear"\n  value: Company website\n')
    return cfg


def test_open_ended_classifier():
    assert is_open_ended("Why do you want to work here?", "textarea")
    assert is_open_ended("Describe a project you're proud of", "text")
    assert is_open_ended("Anything else you'd like us to know?", "textarea")
    assert is_open_ended("How accurate are you?", "textarea")  # "accurate" is not "rate"
    for label in ["Will you require visa sponsorship?", "Desired salary", "Earliest start date",
                  "LinkedIn Profile", "First Name", "Is the pay rate acceptable?"]:
        assert not is_open_ended(label, "textarea"), label


def test_drafter_removes_invented_claims_and_skips(profile):
    job = _jobs()[0]
    draft = make_drafter(_llm(), profile, job)
    text = draft("Why do you want to work here?")
    assert "FAISS" in text and "Kubernetes" not in text
    assert make_drafter(FakeLLM([{}], text_reply="SKIP"), profile, job)("Tell us about your weekend") is None
    assert make_drafter(None, profile, job, cover_letter="Dear team")("Cover letter") == "Dear team"
    assert make_drafter(None, profile, job)("Why us?") is None  # offline: never invents an answer


def test_auto_approve_cap_and_live_submit(auto_cfg):
    cfg = auto_cfg
    cfg.pipeline.max_per_company_per_run = 1
    cfg.submit.enabled = True
    cfg.submit.live = True
    cfg.submit.max_per_day = 2
    db = DB(cfg.db_path)
    report = run(cfg, _llm(), db=db, jobs=_jobs(n_per_company=2))  # 6 jobs, 3 companies

    # one per company tailored tonight; the rest wait for another night
    assert report.auto_approved == 3 and report.queued == 0
    assert db.counts().get("scored") == 3
    # daily cap 2: two submitted (fixture form shows a thank-you page), one still approved
    assert report.submitted == 2
    assert db.counts().get("submitted") == 2 and db.counts().get("approved") == 1

    sub = next(r for r in db.by_status("submitted"))
    answers = json.loads((Path(sub["out_dir"]) / "answers.json").read_text())
    why = next(a for a in answers if a["question"].startswith("Why do you want"))
    assert "FAISS" in why["answer"] and "Kubernetes" not in why["answer"]
    assert (Path(sub["out_dir"]) / "confirmation.png").exists()

    # same day, second run: cap already used, nothing more goes out
    again = run(cfg, _llm(), db=db, jobs=[])
    # (it tailors the 3 jobs held back by the per-company cap, but submits none)
    assert again.submitted == 0 and db.counts() == {"approved": 4, "submitted": 2}


def test_flagged_or_low_score_jobs_wait_for_you(auto_cfg):
    cfg = auto_cfg
    cfg.search.needs_sponsorship = True
    flagged = _jobs(companies=("Acme",))[0]
    flagged.description += "\nWe are unable to sponsor visas for this role."
    db = DB(cfg.db_path)
    run(cfg, _llm(score=9), db=db, jobs=[flagged])
    assert db.get(flagged.id)["status"] == "queued"

    db2 = DB(cfg.home / "data" / "other.db")
    run(cfg, _llm(score=7), db=db2, jobs=_jobs(companies=("Globex",)))
    assert db2.counts() == {"queued": 1}


def test_unanswerable_required_question_goes_to_you(auto_cfg):
    cfg = auto_cfg
    cfg.submit.enabled = True
    cfg.submit.live = True
    (cfg.home / "answers.yaml").write_text('- match: "sponsor"\n  value: __FILL_ME__\n')
    db = DB(cfg.db_path)
    run(cfg, _llm(), db=db, jobs=_jobs(companies=("Acme",)))
    row = db.by_status("needs_human")[0]
    assert "sponsorship" in row["error"]


def test_bundled_companies_and_prune(tmp_path):
    c = Companies(include_defaults=True, greenhouse=["stripe", "mycompany"], exclude=["reddit"])
    r = c.resolved(tmp_path)
    assert len(r.greenhouse) > 100 and r.greenhouse.count("stripe") == 1 and "mycompany" in r.greenhouse
    assert "reddit" not in r.greenhouse and "openai" in r.ashby
    p = dead_boards_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("greenhouse:mycompany\n")
    assert "mycompany" not in c.resolved(tmp_path).greenhouse
