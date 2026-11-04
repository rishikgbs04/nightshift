import json
from datetime import datetime, timezone

from conftest import FIX

from nightshift import filters, keywords
from nightshift.config import SearchConfig
from nightshift.models import Job
from nightshift.sources import html_to_text, parse_ashby, parse_greenhouse, parse_lever


def test_term_boundaries():
    t = keywords.find_terms("C++ and Node.js on Google Cloud; Go services; postgres")
    assert {"C++", "Node.js", "GCP", "Go", "PostgreSQL"} <= t
    assert "Go" not in keywords.find_terms("We use Google Docs and good tooling")
    assert "Java" not in keywords.find_terms("JavaScript only")
    assert "React Native" in keywords.find_terms("React Native app")


def test_coverage_separates_gaps_from_cut_content(profile, jd):
    text = "Python FastAPI PostgreSQL"
    c = keywords.coverage(jd, text, profile)
    assert "Kubernetes" in c.gaps  # not in the example profile
    assert "Docker" in c.missing_but_in_profile  # in profile, not in this text
    assert "Python" in c.matched
    assert 0 < c.score < 1


def test_html_to_text_handles_double_escaped_greenhouse():
    assert html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;&lt;ul&gt;&lt;li&gt;one&lt;/li&gt;&lt;/ul&gt;") == "Hi & bye\n\n- one"


def test_parse_greenhouse():
    jobs = parse_greenhouse("lumenlabs", json.loads((FIX / "greenhouse.json").read_text()))
    assert len(jobs) == 2
    j = jobs[0]
    assert j.id == "greenhouse:lumenlabs:4012345" and j.company == "Lumen Labs" and j.location == "New York, NY"
    assert "PostgreSQL & Redis" in j.description and "<" not in j.description
    assert j.posted_at.year == 2026


def test_parse_lever():
    (j,) = parse_lever("northwind", json.loads((FIX / "lever.json").read_text()))
    assert j.company == "Northwind" and j.location == "Toronto, ON"
    assert "PyTorch" in j.description and "unable to sponsor" in j.description
    assert j.apply_url.endswith("/apply")


def test_parse_ashby_skips_unlisted():
    jobs = parse_ashby("brightwave", json.loads((FIX / "ashby.json").read_text()))
    assert [j.title for j in jobs] == ["Backend Engineer"]


def _job(title="Software Engineer", location="New York, NY", description="", days_old=1):
    return Job(id="x", source="manual", company="C", title=title, location=location, description=description,
               posted_at=datetime.now(timezone.utc).replace(microsecond=0) - __import__("datetime").timedelta(days=days_old))


S = SearchConfig(title_include=["software engineer", "intern"], title_exclude=["senior", "sr", "staff"],
                 locations=["New York", "Remote", "London"], needs_sponsorship=True, sponsorship_policy="flag")


def test_filters_titles_locations_age():
    assert filters.check(_job(), S)[0]
    assert not filters.check(_job(title="Sr. Software Engineer"), S)[0]
    assert not filters.check(_job(title="Product Designer"), S)[0]
    assert not filters.check(_job(location="Austin, TX"), S)[0]
    assert not filters.check(_job(days_old=60), S)[0]
    # "intern" must not match "internal"
    assert not filters.check(_job(title="Internal Tools Designer"), S)[0]


def test_sponsorship_flag_and_skip():
    desc = "Applicants must be authorized to work in the US without the need for visa sponsorship."
    keep, notes = filters.check(_job(description=desc), S)
    assert keep and notes and "sponsorship" in notes[0]
    keep, _ = filters.check(_job(description=desc), S.model_copy(update={"sponsorship_policy": "skip"}))
    assert not keep
    assert filters.sponsorship_blocker("We are unable to sponsor visas for this role.")
    assert filters.sponsorship_blocker("We sponsor work visas for eligible candidates.") is None
    assert filters.sponsorship_blocker("Visa sponsorship is available.") is None
