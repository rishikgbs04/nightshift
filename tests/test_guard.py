from nightshift.guard import enforce, enforce_letter
from nightshift.models import TailoredResume


def draft(**kw) -> TailoredResume:
    return TailoredResume.model_validate(kw)


def test_honest_rewrite_is_kept(profile):
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Cut p95 latency from 1.8s to 300ms by adding Redis caching for the five most expensive report queries",
         "sources": ["acme-2"]}]}])
    r, v = enforce(d, profile)
    assert v == []
    assert r.experience[0].bullets[0].text.startswith("Cut p95 latency")
    assert r.experience[0].bullets[0].original.startswith("Added Redis caching")


def test_invented_number_is_reverted(profile):
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Added Redis caching that cut p95 latency by 90% for 2M daily requests", "sources": ["acme-2"]}]}])
    r, v = enforce(d, profile)
    assert any(x.rule == "numbers" for x in v)
    assert r.experience[0].bullets[0].text == profile.bullet("acme-2").text


def test_technology_from_the_jd_is_reverted(profile):
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Built a FastAPI service on Kubernetes that serves customer usage reports from PostgreSQL",
         "sources": ["acme-1"]}]}])
    r, v = enforce(d, profile)
    assert any(x.rule == "technology" and "Kubernetes" in x.detail for x in v)
    assert "Kubernetes" not in r.experience[0].bullets[0].text


def test_technology_from_another_job_is_reverted(profile):
    # React is in the profile, but not in the Acme internship
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Built a FastAPI and React service that serves customer usage reports from PostgreSQL",
         "sources": ["acme-1"]}]}])
    _, v = enforce(d, profile)
    assert any(x.rule == "technology" and "React" in x.detail for x in v)


def test_borrowing_a_bullet_from_another_entry_is_rejected(profile):
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Rebuilt the room-booking site in React and Django", "sources": ["lib-1"]},
        {"text": "Built a FastAPI service that serves customer usage reports", "sources": ["acme-1"]}]}])
    r, v = enforce(d, profile)
    assert any(x.rule == "provenance" for x in v)
    assert all("room-booking" not in b.text for b in r.experience[0].bullets)


def test_role_inflation_is_reverted(profile):
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Led the containerization of the service with Docker and wrote 40 pytest tests",
         "sources": ["acme-3"]}]}])
    r, v = enforce(d, profile)
    assert any(x.rule == "role" for x in v)
    assert r.experience[0].bullets[0].text == profile.bullet("acme-3").text


def test_merging_two_bullets_of_one_job_is_allowed(profile):
    d = draft(experience=[{"id": "acme", "bullets": [
        {"text": "Built a FastAPI reporting service on PostgreSQL and added Redis caching that cut p95 latency from 1.8s to 300ms",
         "sources": ["acme-1", "acme-2"]}]}])
    _, v = enforce(d, profile)
    assert v == []


def test_unknown_entries_skills_and_coursework_are_removed(profile):
    d = draft(
        experience=[{"id": "google", "bullets": [{"text": "Did things", "sources": ["x"]}]},
                    {"id": "acme", "bullets": [{"text": profile.bullet("acme-1").text, "sources": ["acme-1"]}]}],
        skills={"Languages": ["Python", "Rust"], "Data/Infra": ["Kubernetes", "Redis"]},
        coursework=["Machine Learning", "Quantum Computing"],
    )
    r, v = enforce(d, profile)
    assert [e.id for e in r.experience] == ["acme"]
    flat = [s for g in r.skills.values() for s in g]
    assert "Rust" not in flat and "Kubernetes" not in flat and "Python" in flat and "Redis" in flat
    assert r.coursework == ["Machine Learning"]
    assert {x.where for x in v} >= {"experience:google", "skills", "coursework"}


def test_experience_stays_chronological(profile):
    b = lambda i: {"text": profile.bullet(i).text, "sources": [i]}  # noqa: E731
    d = draft(experience=[{"id": "library", "bullets": [b("lib-1")]}, {"id": "acme", "bullets": [b("acme-1")]}])
    r, _ = enforce(d, profile)
    assert [e.id for e in r.experience] == ["acme", "library"]


def test_duplicate_source_is_not_repeated(profile):
    b = {"text": profile.bullet("acme-1").text, "sources": ["acme-1"]}
    r, _ = enforce(draft(experience=[{"id": "acme", "bullets": [b, b]}]), profile)
    assert len(r.experience[0].bullets) == 1


def test_summary_with_fake_claims_is_dropped(profile):
    r, v = enforce(draft(summary="Engineer with 5 years of Kubernetes experience"), profile)
    assert r.summary == "" and v


def test_letter_sentences_with_invented_claims_are_removed(profile):
    text = ("Your team is scaling Kubernetes to 50 regions, which excites me.\n\n"
            "I built a FastAPI service backed by PostgreSQL. I also ran Kubernetes clusters for 3 years.\n\n"
            "Thank you for your time.")
    cleaned, v = enforce_letter(text, profile)
    assert "Kubernetes to 50 regions" in cleaned  # about the company: fine
    assert "ran Kubernetes clusters" not in cleaned  # about me, not in profile: removed
    assert "I built a FastAPI service" in cleaned
    assert len(v) == 1
