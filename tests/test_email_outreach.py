"""Budget cap, recruiter-reply tracking, and referral outreach."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
import pytest

from nightshift import outreach, replies
from nightshift.config import Config
from nightshift.db import DB
from nightshift.llm import AnthropicLLM, BudgetExceeded, Spend
from nightshift.mail import Mail, compose, parse
from nightshift.models import Job, Profile
from nightshift.server import ensure_home

from conftest import EXAMPLE_PROFILE


class FakeBox:
    def __init__(self, mails):
        self.mails = mails
        self.calls = []

    def fetch_since(self, since, after_uid=0):
        self.calls.append((since, after_uid))
        return [m for m in self.mails if m.uid > after_uid]


class FakeSender:
    def __init__(self, fail=False):
        self.sent: list[EmailMessage] = []
        self.fail = fail

    def send(self, msg):
        if self.fail:
            from nightshift.mail import MailError
            raise MailError("Email sign-in failed")
        self.sent.append(msg)


def _mail(uid, frm, name, subject, text, in_reply_to=""):
    return Mail(message_id=f"<m{uid}@x>", at=datetime.now(timezone.utc), from_addr=frm, from_name=name,
                subject=subject, text=text, in_reply_to=in_reply_to, uid=uid)


@pytest.fixture
def env(tmp_path):
    ensure_home(tmp_path)
    (tmp_path / "profile.yaml").write_text(EXAMPLE_PROFILE.read_text())
    cfg = Config.load(tmp_path / "config.yaml")
    cfg.email.address = "jordan@example.edu"
    cfg.outreach.enabled = True
    cfg.outreach.follow_up_after_days = 6
    db = DB(cfg.db_path)
    for jid, company, status in [("gh:stripe:1", "Stripe", "submitted"), ("gh:acme:2", "Acme Robotics", "submitted"),
                                 ("gh:globex:3", "Globex", "queued")]:
        db.upsert_new(Job(id=jid, source="greenhouse", company=company, title="Software Engineer",
                          description="Python, FastAPI, PostgreSQL and Redis."))
        db.set(jid, status=status, score=8)
    return cfg, db, Profile.load(tmp_path / "profile.yaml")


# ---------------------------------------------------------------------- budget


def test_budget_cap_stops_calls(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}],
                                         "usage": {"input_tokens": 2_000_000, "output_tokens": 1_000_000}})

    from nightshift.config import LLMConfig
    import os
    os.environ["NS_TEST_KEY"] = "k"
    cfg = LLMConfig(api_key_env="NS_TEST_KEY")
    spend = Spend(tmp_path / "spend.json", 0.6, 0.10, 0.50)
    llm = AnthropicLLM(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)), spend=spend)
    assert llm.text("s", "u") == "hi"  # 2M in + 1M out = $0.20 + $0.50 = $0.70 >= $0.60 cap
    assert abs(spend.this_month()["usd"] - 0.70) < 1e-9
    with pytest.raises(BudgetExceeded):
        llm.text("s", "u")
    assert len(calls) == 1  # the second call never reached the API


def test_pipeline_stops_scoring_at_cap(tmp_path):
    from nightshift.pipeline import run

    from conftest import FakeLLM

    ensure_home(tmp_path)
    cfg = Config.load(tmp_path / "config.yaml")
    cfg.search.title_include, cfg.search.locations, cfg.search.max_age_days = [], [], 0

    class Broke(FakeLLM):
        def json(self, system, user):
            raise BudgetExceeded("Monthly AI budget of $9.00 is used up")

    jobs = [Job(id=f"m:{i}", source="manual", company=f"C{i}", title="SWE", description="Python") for i in range(3)]
    r = run(cfg, Broke([{}]), db=DB(cfg.db_path), jobs=jobs)
    assert r.budget_hit and r.scored == 0
    assert DB(cfg.db_path).counts() == {"discovered": 3}  # left for next month, not thrown away


# ---------------------------------------------------------------------- replies


def test_parse_strips_quoted_history():
    raw = (b"From: Jane Doe <jane@stripe.com>\r\nSubject: Re: Software Engineer\r\nMessage-ID: <a1@stripe.com>\r\n"
           b"In-Reply-To: <out1@example.edu>\r\nDate: Wed, 04 Nov 2026 10:00:00 -0500\r\n\r\n"
           b"Hi Jordan, could we schedule a call Thursday?\r\n\r\nOn Mon, Nov 2, 2026 Jordan wrote:\r\n> my email\r\n")
    m = parse(raw, uid=7)
    assert m.from_addr == "jane@stripe.com" and m.in_reply_to == "<out1@example.edu>"
    assert "schedule a call" in m.text and "my email" not in m.text


def test_replies_update_applications_and_notify(env, monkeypatch):
    cfg, db, _ = env
    sent = []
    monkeypatch.setattr(replies.notify, "send", lambda c, t, m, **k: sent.append((t, m)) or True)
    box = FakeBox([
        _mail(1, "no-reply@greenhouse.io", "Stripe Recruiting", "Thanks for applying to Stripe",
              "Thank you for your application. We've received your application and will review it."),
        _mail(2, "jane.doe@stripe.com", "Jane Doe", "Next steps",
              "Hi Jordan, we'd love to schedule a call with you this week. What's your availability?"),
        _mail(3, "talent@acmerobotics.com", "Acme Robotics Talent", "Your application",
              "Unfortunately we've decided to move forward with other candidates."),
        _mail(4, "newsletter@shop.com", "Shop", "50% off", "Big sale"),
        _mail(5, "jordan@example.edu", "Jordan", "Re: hi", "my own email"),
    ])
    r = replies.check(db, cfg, box)
    assert r.checked == 5 and r.matched == 3
    assert db.get("gh:stripe:1")["status"] == "interview"
    assert db.get("gh:acme:2")["status"] == "rejected"
    kinds = {row["id"]: row["kind"] for row in db.conn.execute("SELECT id, kind FROM messages")}
    assert kinds == {"<m1@x>": "received", "<m2@x>": "interview", "<m3@x>": "rejection"}  # the sale isn't stored
    assert sent and sent[0][0] == "Interview request from Stripe"
    # next check only asks for newer mail and doesn't repeat
    again = replies.check(db, cfg, box)
    assert box.calls[-1][1] == 5 and again.matched == 0


def test_rejection_never_overrides_an_interview(env):
    cfg, db, _ = env
    db.set("gh:stripe:1", status="interview")
    replies.check(db, cfg, FakeBox([_mail(9, "x@stripe.com", "Stripe", "Update",
                                          "Unfortunately this other role was filled.")]), send_notification=False)
    assert db.get("gh:stripe:1")["status"] == "interview"


# ---------------------------------------------------------------------- outreach


def hunter_client(emails):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"data": {"emails": emails}})

    return outreach.Hunter("key", client=httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_outreach_finds_drafts_sends_and_caps(env):
    cfg, db, profile = env
    cfg.outreach.max_per_day = 2
    hunter, seen = hunter_client([
        {"value": "sam@stripe.com", "first_name": "Sam", "last_name": "Lee", "position": "Engineering Manager", "confidence": 95},
        {"value": "ria@stripe.com", "first_name": "Ria", "last_name": "Patel", "position": "University Recruiter", "confidence": 92},
        {"value": "low@stripe.com", "first_name": "Lo", "position": "Recruiter", "confidence": 40},
    ])
    outreach.add_contact(db, "alum@globex.com", "Globex", "Alex Kim", kind="alumni", note="NYU MS '24")
    sender = FakeSender()
    r = outreach.run(db, cfg, profile, sender, hunter=hunter)
    # one search per company applied to (Stripe, Acme); none for Globex (not applied yet)
    assert {p["company"] for p in seen} == {"Stripe", "Acme Robotics"}
    assert all(p["department"] == "hr" for p in seen)
    emails = {row["email"]: row["status"] for row in db.conn.execute("SELECT email, status FROM contacts")}
    assert "low@stripe.com" not in emails  # low-confidence emails are dropped
    assert emails["alum@globex.com"] == "new"  # waits until you apply at Globex
    assert r.sent == 2 and len(sender.sent) == 2  # daily cap
    first = sender.sent[0]
    assert first["To"].endswith("<ria@stripe.com>")  # the recruiter is contacted before the manager
    assert "Software Engineer" in first.get_content()
    # same day: nothing more goes out, and Hunter isn't searched again
    r2 = outreach.run(db, cfg, profile, sender, hunter=hunter)
    assert r2.sent == 0 and len(seen) == 2


def test_follow_up_once_and_stop_on_reply(env):
    cfg, db, profile = env
    cfg.outreach.max_per_day = 10
    outreach.add_contact(db, "ria@stripe.com", "Stripe", "Ria Patel", kind="recruiter", job_id="gh:stripe:1")
    outreach.add_contact(db, "sam@acmerobotics.com", "Acme Robotics", "Sam Lee", kind="recruiter", job_id="gh:acme:2")
    sender = FakeSender()
    outreach.run(db, cfg, profile, sender)
    assert len(sender.sent) == 2
    ria_mid = db.conn.execute("SELECT message_id FROM contacts WHERE email = 'ria@stripe.com'").fetchone()[0]
    # a week later; Ria replied, Sam didn't
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE contacts SET sent_at = ?", (old,))
    db.conn.commit()
    replies.check(db, cfg, FakeBox([_mail(1, "ria@stripe.com", "Ria Patel", "Re: Software Engineer",
                                          "Happy to refer you, send me your resume.", in_reply_to=ria_mid)]),
                  send_notification=False)
    assert db.conn.execute("SELECT status FROM contacts WHERE email='ria@stripe.com'").fetchone()[0] == "replied"
    sender.sent.clear()
    outreach.run(db, cfg, profile, sender)
    assert len(sender.sent) == 1 and sender.sent[0]["To"].endswith("<sam@acmerobotics.com>")
    assert sender.sent[0]["Subject"].startswith("Re: ") and sender.sent[0]["In-Reply-To"]
    sender.sent.clear()
    db.conn.execute("UPDATE contacts SET followed_up_at = ?", (old,))
    db.conn.commit()
    outreach.run(db, cfg, profile, sender)
    assert sender.sent == []  # never a second follow-up


def test_drafts_are_guarded(env):
    cfg, db, profile = env
    from conftest import FakeLLM
    outreach.add_contact(db, "ria@stripe.com", "Stripe", "Ria Patel", kind="recruiter", job_id="gh:stripe:1")
    c = db.conn.execute("SELECT * FROM contacts").fetchone()
    llm = FakeLLM([{"subject": "Software Engineer at Stripe",
                    "body": "Hi Ria,\n\nI just applied for the Software Engineer role at Stripe. I built a FastAPI service "
                            "that serves customer usage reports from PostgreSQL. I also led a team of 12 engineers on "
                            "Kubernetes. Would you be the right person to look at my application?\n\nThanks,\nJordan"}])
    from nightshift.db import row_to_job
    subject, body, violations = outreach.draft(llm, profile, row_to_job(db.get("gh:stripe:1")), c)
    assert "FastAPI service" in body and "Kubernetes" not in body and "12 engineers" not in body
    assert violations


def test_login_failure_stops_sending(env):
    cfg, db, profile = env
    outreach.add_contact(db, "a@stripe.com", "Stripe", kind="recruiter", job_id="gh:stripe:1")
    outreach.add_contact(db, "b@stripe.com", "Stripe", kind="recruiter", job_id="gh:stripe:1")
    r = outreach.run(db, cfg, profile, FakeSender(fail=True))
    assert r.sent == 0 and len(r.errors) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM contacts WHERE status = 'drafted'").fetchone()[0] == 2


def test_add_contact_validates_and_dedupes(env):
    _, db, _ = env
    with pytest.raises(ValueError):
        outreach.add_contact(db, "not-an-email", "X")
    assert outreach.add_contact(db, "Ann@X.com", "X") is not None
    assert outreach.add_contact(db, "ann@x.com", "X") is None


def test_people_and_inbox_pages(env, monkeypatch):
    from fastapi.testclient import TestClient
    from nightshift.web.app import create_app
    cfg, db, _ = env
    monkeypatch.delenv("NIGHTSHIFT_PASSWORD", raising=False)
    client = TestClient(create_app(cfg, db, config_path=cfg.home / "config.yaml"))
    r = client.post("/people/add", data={"email": "alex@stripe.com", "company": "Stripe", "name": "Alex Kim",
                                         "kind": "alumni", "note": "NYU MS '24"}, follow_redirects=False)
    assert r.headers["location"] == "/people?added=1"
    page = client.get("/people").text
    assert "Alex Kim" in page and "NYU MS" in page
    cid = db.conn.execute("SELECT id FROM contacts").fetchone()[0]
    assert client.post("/api/contact", json={"id": cid, "do": "skip"}).json()["status"] == "skipped"
    replies.check(db, cfg, FakeBox([_mail(1, "jane@stripe.com", "Jane", "Interview", "Can we schedule a call?")]),
                  send_notification=False)
    assert "Interview" in client.get("/inbox").text
    assert "Interviews" in client.get("/").text
    s = client.get("/settings").text
    assert "AI spending this month" in s and "$0.00" in s


def test_nightly_run_checks_replies_then_sends_outreach(env):
    from nightshift.pipeline import run
    cfg, db, _ = env
    outreach.add_contact(db, "ria@stripe.com", "Stripe", "Ria", kind="recruiter", job_id="gh:stripe:1")
    box = FakeBox([_mail(1, "jane@stripe.com", "Jane", "Interview", "Could we schedule a call next week?")])
    sender = FakeSender()
    r = run(cfg, None, db=db, jobs=[], mailbox=box, sender=sender)
    assert r.replies_matched == 1 and r.good_news and r.outreach_sent == 1
    assert "Needs you" in r.markdown()


def test_router_uses_task_models_and_their_prices(tmp_path, monkeypatch):
    import json as _json

    from nightshift.config import LLMConfig
    from nightshift.llm import Router
    from nightshift.tailor import score_job

    monkeypatch.setenv("GEMINI_TEST_KEY", "k")
    seen = []

    def handler(req):
        body = _json.loads(req.content)
        seen.append((body["model"], body.get("reasoning_effort")))
        assert req.url.path.endswith("/v1beta/openai/chat/completions")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"score": 8, "reasons": ["fit"], "red_flags": []}'}}],
            # 1,000 thinking tokens are in total_tokens but not completion_tokens
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 2_100_000}})

    cfg = LLMConfig(provider="openai", base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    api_key_env="GEMINI_TEST_KEY", model="gemini-3.5-flash-lite", reasoning_effort="low",
                    task_models={"score": "gemini-3.1-flash-lite"},
                    prices={"gemini-3.1-flash-lite": [0.25, 1.50], "gemini-3.5-flash-lite": [0.30, 2.50]})
    spend = Spend(tmp_path / "spend.json", 50, cfg.price_input_per_mtok, cfg.price_output_per_mtok)
    router = Router(cfg, spend, client=httpx.Client(transport=httpx.MockTransport(handler)))
    profile = Profile.load(EXAMPLE_PROFILE)
    job = Job(id="m:1", source="manual", company="Acme", title="SWE", description="Python")
    assert score_job(router, profile, job)["score"] == 8
    assert seen[-1] == ("gemini-3.1-flash-lite", "low")
    # 1M input at $0.25 + 1.1M output (incl. thinking) at $1.50 = $1.90
    assert abs(spend.this_month()["usd"] - 1.90) < 1e-6
    router.text("s", "u")
    assert seen[-1][0] == "gemini-3.5-flash-lite"
    assert abs(spend.this_month()["usd"] - (1.90 + 0.30 + 1.1 * 2.50)) < 1e-6
