import json

from conftest import FIX, FakeLLM
from fastapi.testclient import TestClient
from pypdf import PdfReader

from nightshift.db import DB
from nightshift.models import Job
from nightshift.pipeline import run
from nightshift.sources import parse_ashby, parse_greenhouse, parse_lever
from nightshift.tailor import draft_offline, output_files, tailor

LLM_DRAFT = {
    "summary": "",
    "section_order": ["projects", "experience"],
    "experience": [
        {"id": "acme", "bullets": [
            {"text": "Cut p95 latency from 1.8s to 300ms by adding Redis caching to the five most expensive report queries",
             "sources": ["acme-2"]},
            # fabricated: Kubernetes + 10x -> must be reverted
            {"text": "Scaled the FastAPI reporting service 10x on Kubernetes", "sources": ["acme-1"]},
        ]},
        {"id": "library", "bullets": [{"text": "Rebuilt the room-booking site in React and Django, used by about 3,000 students per semester", "sources": ["lib-1"]}]},
    ],
    "projects": [{"id": "ragbot", "bullets": [
        {"text": "Built a retrieval-augmented Q&A assistant over 400 pages of lecture notes using embeddings and FAISS", "sources": ["rag-1"]}]}],
    "skills": {"Languages": ["Python", "SQL"], "Data/Infra": ["PostgreSQL", "Redis", "Docker", "Kubernetes"]},
    "coursework": ["Machine Learning", "Distributed Systems"],
}

LETTER = ("Lumen's AI platform sits exactly where I want to work.\n\n"
          "I built a retrieval-augmented Q&A assistant using embeddings and FAISS. I have deployed Kafka at scale.\n\n"
          "I'd love to talk.")


def _job(jd, location="New York, NY"):
    return Job(id="manual:t", source="manual", company="Lumen Labs", title="Software Engineer, AI Platform",
               location=location, description=jd)


def test_tailor_with_llm_enforces_guard_and_fits_one_page(cfg, profile, jd, tmp_path):
    llm = FakeLLM([LLM_DRAFT], text_reply=LETTER)
    res = tailor(_job(jd), profile, cfg, llm, tmp_path / "out")
    files = output_files(tmp_path / "out")
    assert files["resume"] and files["letter"]
    assert res.pages == 1 and len(PdfReader(files["resume"]).pages) == 1
    rules = {v.rule for v in res.violations}
    assert {"numbers", "technology"} <= rules
    all_text = " ".join(b.text for te in res.resume.experience for b in te.bullets)
    assert "Kubernetes" not in all_text and "10x" not in all_text
    assert "Kubernetes" not in sum(res.resume.skills.values(), [])
    assert "Kafka" not in res.cover_letter and "AI platform" in res.cover_letter
    assert res.resume.section_order == ["projects", "experience"]
    pdf_text = " ".join(" ".join(p.extract_text() for p in PdfReader(files["resume"]).pages).split())
    assert "Jordan Rivera" in pdf_text and "Redis" in pdf_text  # real, extractable text for ATS


def test_offline_tailoring_never_rewords(cfg, profile, jd, tmp_path):
    res = tailor(_job(jd), profile, cfg, None, tmp_path / "out")
    assert res.violations == []
    originals = {b.text for e in profile.entries() for b in e.bullets}
    assert all(b.text in originals for te in [*res.resume.experience, *res.resume.projects] for b in te.bullets)
    # the most relevant project leads its section
    assert res.resume.projects[0].id == "ragbot"


def test_uk_jobs_get_an_a4_cv(cfg, profile, jd, tmp_path):
    res = tailor(_job(jd, location="London, UK"), profile, cfg, None, tmp_path / "out")
    assert res.doc_kind == "cv"
    resume = output_files(tmp_path / "out")["resume"]
    assert resume.name.endswith("_CV.pdf")
    w, h = PdfReader(resume).pages[0].mediabox.upper_right
    assert abs(float(w) - 595) < 2 and abs(float(h) - 842) < 2  # A4


def test_page_fitting_trims_a_long_profile(cfg, profile, jd, tmp_path):
    # quadruple every entry's bullets so it cannot fit on one page untouched
    for e in profile.entries():
        extra = []
        for k in range(3):
            for b in e.bullets:
                extra.append(b.model_copy(update={"id": f"{b.id}-x{k}", "text": b.text + f" (variant {k})"}))
        e.bullets += extra
    res = tailor(_job(jd), profile, cfg, None, tmp_path / "out")
    assert res.pages == 1
    assert all(len(te.bullets) >= 1 for te in res.resume.experience)


def test_offline_draft_ranks_relevant_bullets_first(profile, jd):
    d = draft_offline(profile, jd)
    acme = next(te for te in d.experience if te.id == "acme")
    assert acme.bullets[-1].sources == ["acme-4"]  # the on-call runbook bullet is least relevant


def test_nightly_pipeline_and_dashboard(cfg, profile, tmp_path):
    jobs = [
        *parse_greenhouse("lumenlabs", json.loads((FIX / "greenhouse.json").read_text())),
        *parse_lever("northwind", json.loads((FIX / "lever.json").read_text())),
        *parse_ashby("brightwave", json.loads((FIX / "ashby.json").read_text())),
    ]
    cfg.search.title_include = ["engineer", "intern"]
    cfg.search.title_exclude = ["senior", "staff"]
    cfg.search.locations = ["New York", "Toronto", "London"]
    cfg.search.max_age_days = 0
    cfg.search.needs_sponsorship = True
    cfg.pipeline.min_score = 5
    score = lambda user: {"score": 8 if "New Grad" in user or "Backend" in user else 3,  # noqa: E731
                          "reasons": ["fake"], "red_flags": []}
    llm = FakeLLM([score], text_reply=LETTER)
    llm.json_replies = [lambda user: score(user) if "CANDIDATE PROFILE" in user else LLM_DRAFT]

    db = DB(cfg.db_path)
    report = run(cfg, llm, db=db, jobs=jobs)
    assert report.discovered == 4 and report.new == 4
    assert report.filtered_out == 1  # the senior staff role
    counts = db.counts()
    assert counts["queued"] == 2 and counts["low_score"] == 1
    lever_row = db.get("lever:northwind:5d2a2b8e-1111-4c1e-9a8f-000000000001")
    assert "sponsor" in lever_row["notes_json"]
    london = db.get("ashby:brightwave:a1b2c3d4-0000-4000-8000-000000000001")
    assert any(p.name.endswith("_CV.pdf") for p in (cfg.home / london["out_dir"]).glob("*.pdf")) or \
        any(p.name.endswith("_CV.pdf") for p in __import__("pathlib").Path(london["out_dir"]).glob("*.pdf"))
    assert (cfg.home / "data" / "last_run.md").exists()

    # second night: nothing new
    again = run(cfg, llm, db=db, jobs=jobs)
    assert again.new == 0 and again.queued == 0

    from nightshift.web.app import create_app
    client = TestClient(create_app(cfg, db))
    page = client.get("/")
    assert page.status_code == 200 and "Software Engineer, New Grad" in page.text
    jid = "greenhouse:lumenlabs:4012345"
    detail = client.get("/job", params={"id": jid})
    assert detail.status_code == 200 and "Keyword match" in detail.text and 'class="was"' in detail.text
    row = db.get(jid)
    pdf_name = output_files(__import__("pathlib").Path(row["out_dir"]))["resume"].name
    f = client.get("/file", params={"id": jid, "name": pdf_name})
    assert f.status_code == 200 and f.content[:4] == b"%PDF"
    assert client.get("/file", params={"id": jid, "name": "../../nightshift.db"}).status_code == 404
    r = client.post("/action", data={"id": jid, "do": "approve"}, follow_redirects=False)
    assert r.status_code == 303 and db.get(jid)["status"] == "approved"
    r = client.post("/letter", data={"id": jid, "text": "I have used Kafka for years.\n\nI built a FAISS assistant."},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = (__import__("pathlib").Path(row["out_dir"]) / "cover_letter.txt").read_text()
    assert "Kafka" not in saved and "FAISS" in saved
