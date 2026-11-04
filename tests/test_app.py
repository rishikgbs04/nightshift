"""The phone app: login, swipe actions with undo, settings, run-now, notifications, scheduler."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from nightshift import notify
from nightshift.config import Config, NotifyConfig
from nightshift.db import DB
from nightshift.models import Job
from nightshift.server import Runner, ensure_home, next_run
from nightshift.web import auth
from nightshift.web.app import create_app


@pytest.fixture
def home(tmp_path):
    ensure_home(tmp_path)
    return tmp_path


def _client(home, monkeypatch, password="hunter22"):
    if password:
        monkeypatch.setenv("NIGHTSHIFT_PASSWORD", password)
    else:
        monkeypatch.delenv("NIGHTSHIFT_PASSWORD", raising=False)
    cfg = Config.load(home / "config.yaml")
    db = DB(cfg.db_path)
    db.upsert_new(Job(id="greenhouse:acme:1", source="greenhouse", company="Acme Robotics", title="Backend Engineer",
                      location="New York, NY", url="https://example.com/1", apply_url="https://example.com/1",
                      description="Python FastAPI"))
    db.set("greenhouse:acme:1", status="queued", score=8, score_json={"score": 8, "reasons": ["Python match"], "red_flags": []})
    ran = []
    runner = Runner(home / "config.yaml", db, run_fn=lambda c, d, p: ran.append(1) or type("R", (), {"auto_approved": 0, "submitted": 0, "markdown": lambda self: "ok"})())
    return TestClient(create_app(cfg, db, runner=runner, config_path=home / "config.yaml")), db, ran


def _login(client, pw="hunter22"):
    return client.post("/login", data={"password": pw}, follow_redirects=False)


def test_first_boot_creates_editable_files(home):
    for name in ("config.yaml", "profile.yaml", "answers.yaml"):
        assert (home / name).exists()


def test_login_required_and_works(home, monkeypatch):
    client, _, _ = _client(home, monkeypatch)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/action", json={"id": "x", "do": "approve"}).status_code == 401
    for open_path in ("/login", "/manifest.webmanifest", "/sw.js", "/static/app.css", "/healthz"):
        assert client.get(open_path, follow_redirects=False).status_code == 200, open_path
    assert _login(client, "wrong").headers["location"] == "/login?error=1"
    r = _login(client)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    page = client.get("/")
    assert page.status_code == 200 and "1 job ready for you" in page.text and "Acme Robotics" in page.text


def test_tampered_or_expired_cookie_is_rejected(home, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_PASSWORD", "pw")
    tok = auth.make_token(home)
    assert auth.valid_token(home, tok)
    exp, sig = tok.split(".")
    assert not auth.valid_token(home, f"{int(exp) + 999}.{sig}")
    assert not auth.valid_token(home, tok, now=int(exp) + 1)
    monkeypatch.setenv("NIGHTSHIFT_PASSWORD", "changed")  # changing the password signs everyone out
    assert not auth.valid_token(home, tok)


def test_swipe_actions_and_undo(home, monkeypatch):
    client, db, _ = _client(home, monkeypatch)
    _login(client)
    r = client.post("/api/action", json={"id": "greenhouse:acme:1", "do": "approve"}).json()
    assert r["status"] == "approved" and r["previous"] == "queued" and r["counts"]["approved"] == 1
    r = client.post("/api/action", json={"id": "greenhouse:acme:1", "do": "undo", "to": r["previous"]}).json()
    assert r["status"] == "queued"
    assert client.post("/api/action", json={"id": "greenhouse:acme:1", "do": "undo", "to": "discovered"}).status_code == 400
    assert client.post("/api/action", json={"id": "greenhouse:acme:1", "do": "delete"}).status_code == 400
    client.post("/api/action", json={"id": "greenhouse:acme:1", "do": "skip"})
    assert db.get("greenhouse:acme:1")["status"] == "skipped"


def test_settings_validate_before_saving(home, monkeypatch):
    client, _, _ = _client(home, monkeypatch)
    _login(client)
    before = (home / "profile.yaml").read_text()
    r = client.post("/settings/profile", data={"text": "basics: {}\n"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and (home / "profile.yaml").read_text() == before
    r = client.post("/settings/config", data={"text": "schedule: {time: '03:05'}\nnotify: {ntfy_topic: t1}\n"},
                    follow_redirects=False)
    assert "saved=config" in r.headers["location"]
    assert "03:05" in (home / "config.yaml").read_text() and "Not scheduled here" in client.get("/settings").text


def test_answers_form_fills_blanks_without_yaml(home, monkeypatch):
    client, _, _ = _client(home, monkeypatch)
    _login(client)
    page = client.get("/settings").text
    assert "Will you need visa sponsorship?" in page and "still unanswered" in page
    items = yaml.safe_load((home / "answers.yaml").read_text())
    idx = next(i for i, a in enumerate(items) if a["label"] == "Will you need visa sponsorship?")
    client.post("/settings/answers-form", data={f"a{idx}": "No"})
    items = yaml.safe_load((home / "answers.yaml").read_text())
    assert items[idx]["value"] == "No" and items[idx]["match"] == "sponsor"


def test_run_now_from_the_app(home, monkeypatch):
    client, _, ran = _client(home, monkeypatch)
    _login(client)
    assert client.post("/api/run").json()["started"]
    for _ in range(50):
        if not client.get("/api/run").json()["running"]:
            break
        __import__("time").sleep(0.05)
    assert ran == [1]


def test_local_mode_without_password(home, monkeypatch):
    client, _, _ = _client(home, monkeypatch, password=None)
    assert client.get("/").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303


def test_ntfy_notification():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = NotifyConfig(ntfy_topic="nightshift-abc", app_url="https://me.fly.dev")
    title, msg = notify.morning_message(waiting=14, auto_approved=0, submitted=3, needs_you=1)
    assert notify.send(cfg, title, msg, client=client)
    assert seen["url"] == "https://ntfy.sh/nightshift-abc"
    assert seen["headers"]["title"] == "14 jobs to review" and seen["headers"]["click"] == "https://me.fly.dev"
    assert seen["body"] == "3 applications sent, 1 needs you to finish, tap to review."
    assert not notify.send(NotifyConfig(), "t", "m", client=client)  # no topic: silently off


def test_next_run_handles_timezones_and_rollover():
    ny = ZoneInfo("America/New_York")
    assert next_run(datetime(2026, 11, 4, 1, 0, tzinfo=ny), "02:17", "America/New_York") == \
        datetime(2026, 11, 4, 2, 17, tzinfo=ny)
    assert next_run(datetime(2026, 11, 4, 2, 17, tzinfo=ny), "02:17", "America/New_York") == \
        datetime(2026, 11, 5, 2, 17, tzinfo=ny)
    utc = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo("UTC"))  # 7:00 in New York
    assert next_run(utc, "02:17", "America/New_York").day == 5


def test_runner_notifies_after_a_run(home, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, title, msg, **kw: sent.append((title, msg)) or True)
    db = DB(home / "data" / "nightshift.db")
    db.upsert_new(Job(id="manual:1", source="manual", company="A", title="T"))
    db.set("manual:1", status="queued")
    r = Runner(home / "config.yaml", db, run_fn=lambda c, d, p: type("R", (), {"auto_approved": 0, "submitted": 0})())
    st = r.run_now()
    assert not st.running and not st.error
    assert sent and sent[0][0] == "1 job to review"
    boom = Runner(home / "config.yaml", db, run_fn=lambda c, d, p: (_ for _ in ()).throw(RuntimeError("no key")))
    st = boom.run_now()
    assert "no key" in st.error and sent[-1][0].startswith("nightshift couldn't finish")


def test_server_refuses_public_host_without_password(home, monkeypatch):
    from nightshift.server import serve
    monkeypatch.delenv("NIGHTSHIFT_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        serve(home, host="0.0.0.0", port=0)


def test_manifest_is_installable(home, monkeypatch):
    client, _, _ = _client(home, monkeypatch)
    m = client.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"}
    assert json.loads(json.dumps(m))["start_url"] == "/"


def test_schedule_text_matches_mode(home, monkeypatch):
    client, _, _ = _client(home, monkeypatch, password=None)
    page = client.get("/").text
    assert "No nightly search on this computer yet" in page and "tonight" not in page
    cfg = Config.load(home / "config.yaml")
    sched = TestClient(create_app(cfg, DB(cfg.db_path), config_path=home / "config.yaml", scheduled=True))
    assert "First search tonight at 2:17 AM" in sched.get("/").text


def test_approved_jobs_can_be_skipped(home, monkeypatch):
    client, db, _ = _client(home, monkeypatch, password=None)
    db.set("greenhouse:acme:1", status="approved")
    page = client.get("/?status=approved").text
    assert 'data-act="skip"' in page
    client.post("/api/action", json={"id": "greenhouse:acme:1", "do": "skip"})
    assert db.get("greenhouse:acme:1")["status"] == "skipped"
