"""The nightshift app: review tailored applications from your phone.

Mobile-first, installable to the home screen, swipe right to approve and left
to skip. Also serves the settings page so the cloud server never needs a
terminal after the first deploy.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import guard, notify
from ..config import Config
from ..db import DB, STATUSES, row_to_job
from ..llm import make_llm
from ..models import Job, Profile, TailorResult
from ..pipeline import job_dir, profile_cache
from ..render import PDFRenderer, letter_html, write
from ..sources import job_from_url
from ..tailor import output_files, tailor
from . import auth

HERE = Path(__file__).parent
TABS = [("queued", "Review"), ("approved", "Approved"), ("needs_human", "Needs you"),
        ("submitted", "Applied"), ("skipped", "Skipped"), ("low_score", "Low match"),
        ("failed", "Failed")]
ACTIONS = {"approve": "approved", "skip": "skipped", "applied": "submitted", "requeue": "queued"}
UNDOABLE_TO = {"queued", "approved", "needs_human", "skipped", "low_score"}


def create_app(cfg: Config, db: Optional[DB] = None, runner=None,
               config_path: Optional[Path] = None, scheduled: bool = False) -> FastAPI:
    """scheduled=True when the nightly schedule runs alongside the app (`nightshift server`)."""
    app = FastAPI(title="nightshift", docs_url=None, redoc_url=None, openapi_url=None)
    db = db or DB(cfg.db_path)
    state = {"cfg": cfg}
    config_path = Path(config_path) if config_path else cfg.home / "config.yaml"
    if runner is None:
        from ..server import Runner
        runner = Runner(config_path, db)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    templates.env.globals["initials"] = _initials
    templates.env.globals["avatar_hue"] = _hue
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------------ auth

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if auth.password() and not auth.is_open(request.url.path):
            if not auth.valid_token(state["cfg"].home, request.cookies.get(auth.COOKIE)):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"error": "signed out"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        if not auth.password():
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        if not auth.check_password(password):
            import time
            time.sleep(1)  # slow down guessing
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(auth.COOKIE, auth.make_token(state["cfg"].home), max_age=auth.MAX_AGE,
                        httponly=True, samesite="lax", secure=secure)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ------------------------------------------------------------------ PWA

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": "nightshift",
            "short_name": "nightshift",
            "description": "Approve the jobs nightshift tailored for you overnight.",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#1B1F3B",
            "theme_color": "#1B1F3B",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }, media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        return Response((HERE / "static" / "sw.js").read_text(), media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ helpers

    def load_result(row) -> Optional[TailorResult]:
        if not row["out_dir"]:
            return None
        p = Path(row["out_dir"]) / "result.json"
        return TailorResult.model_validate_json(p.read_text()) if p.exists() else None

    def card(row) -> dict:
        score = json.loads(row["score_json"]) if row["score_json"] else {}
        notes = json.loads(row["notes_json"]) if row["notes_json"] else []
        res = load_result(row)
        cov = res.coverage_after if res else None
        return {
            "id": row["id"], "title": row["title"], "company": row["company"],
            "location": row["location"] or "", "status": row["status"],
            "score": row["score"], "fit": row["fit"],
            "why": (score.get("reasons") or [])[:2], "red_flags": score.get("red_flags") or [],
            "flags": [n for n in notes if not n.startswith("guard:")],
            "matched": len(cov.matched) if cov else None,
            "wanted": (len(cov.matched) + len(cov.missing_but_in_profile) + len(cov.gaps)) if cov else None,
            "kind": res.doc_kind if res else None,
            "apply_url": row["apply_url"] or row["url"] or "",
            "error": row["error"] or "",
            "has_preview": bool(row["out_dir"] and (Path(row["out_dir"]) / "preview.png").exists()),
        }

    def header(cfg: Config) -> dict:
        tz = ZoneInfo(cfg.schedule.timezone)
        now = datetime.now(tz)
        hr = now.hour
        greet = ("Good morning" if 5 <= hr < 12 else "Good afternoon" if 12 <= hr < 17
                 else "Good evening" if 17 <= hr < 22 else "Up late")
        try:
            first = profile_cache(cfg).basics.name.split()[0]
        except Exception:  # noqa: BLE001
            first = ""
        last = db.conn.execute("SELECT at FROM events WHERE kind = 'run' ORDER BY rowid DESC LIMIT 1").fetchone()
        last_run = None
        if last:
            last_run = datetime.fromisoformat(last["at"]).astimezone(tz)
        h, m = (int(x) for x in cfg.schedule.time.split(":"))
        return {"greeting": f"{greet}, {first}" if first else greet,
                "scheduled": scheduled and cfg.schedule.enabled,
                "last_run": last_run, "next_time": datetime(2000, 1, 1, h, m),
                "counts": db.counts()}

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def feed(request: Request, status: str = "queued"):
        if status not in STATUSES:
            raise HTTPException(404)
        cfg = state["cfg"]
        rows = db.by_status(status)
        return templates.TemplateResponse(request, "feed.html", {
            "cards": [card(r) for r in rows], "status": status, "tabs": TABS,
            "h": header(cfg), "run": runner.state,
        })

    @app.get("/job", response_class=HTMLResponse)
    def detail(request: Request, id: str):
        row = db.get(id)
        if row is None:
            raise HTTPException(404)
        cfg = state["cfg"]
        result = load_result(row)
        profile = profile_cache(cfg)
        out = Path(row["out_dir"]) if row["out_dir"] else None
        files = output_files(out) if out else {}
        drafted = json.loads((out / "answers.json").read_text()) if out and (out / "answers.json").exists() else []
        return templates.TemplateResponse(request, "job.html", {
            "row": row, "c": card(row), "r": result, "files": files,
            "entries": {e.id: e for e in profile.entries()},
            "drafted": drafted, "form_shot": bool(out and (out / "form.png").exists()),
        })

    @app.get("/file")
    def file(id: str, name: str):
        row = db.get(id)
        if row is None or not row["out_dir"]:
            raise HTTPException(404)
        base = Path(row["out_dir"]).resolve()
        target = (base / name).resolve()
        if base not in target.parents or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, headers={"Cache-Control": "private, max-age=300"})

    # ------------------------------------------------------------------ actions

    def apply_action(id: str, do: str, to: str = "") -> str:
        row = db.get(id)
        if row is None:
            raise HTTPException(404)
        if do == "undo":
            if to not in UNDOABLE_TO:
                raise HTTPException(400, "can't undo to that state")
            new = to
        else:
            new = ACTIONS.get(do)
            if new is None:
                raise HTTPException(400, "unknown action")
        db.set(id, status=new)
        db.log(do, to, id)
        return new

    @app.post("/action")
    def action_form(id: str = Form(...), do: str = Form(...), back: str = Form("queued")):
        apply_action(id, do)
        return RedirectResponse(f"/?status={back}", status_code=303)

    @app.post("/api/action")
    async def action_api(request: Request):
        body = await request.json()
        row = db.get(body.get("id", ""))
        prev = row["status"] if row else ""
        new = apply_action(body.get("id", ""), body.get("do", ""), body.get("to", ""))
        return {"ok": True, "status": new, "previous": prev, "counts": db.counts()}

    @app.post("/letter")
    def save_letter(id: str = Form(...), text: str = Form(...)):
        row = db.get(id)
        if row is None or not row["out_dir"]:
            raise HTTPException(404)
        cfg = state["cfg"]
        profile = profile_cache(cfg)
        job = row_to_job(row)
        out = Path(row["out_dir"])
        cleaned, violations = guard.enforce_letter(text.replace("\r\n", "\n"), profile)
        result = load_result(row)
        paper = "A4" if result and result.doc_kind == "cv" else "Letter"
        with PDFRenderer(cfg.submit.browser_channel, cfg.submit.browser_executable) as r:
            stem = (output_files(out)["letter"] or out / "Cover_Letter.pdf").name
            write(out / stem, r.pdf(letter_html(profile, job, cleaned, paper), paper))
        write(out / "cover_letter.txt", cleaned)
        if result:
            result.cover_letter = cleaned
            result.violations = [v for v in result.violations if not v.where.startswith("cover letter")] + violations
            write(out / "result.json", result.model_dump_json(indent=2))
        return RedirectResponse(f"/job?id={id}#letter", status_code=303)

    def _tailor_and_store(job: Job) -> str:
        cfg = state["cfg"]
        profile: Profile = profile_cache(cfg)
        db.upsert_new(job)
        out = job_dir(cfg, job)
        res = tailor(job, profile, cfg, make_llm(cfg.llm), out)
        notes = [f"guard: {v.where}: {v.detail} ({v.action})" for v in res.violations]
        db.set(job.id, status="queued", out_dir=str(out), notes_json=notes)
        return job.id

    @app.post("/retailor")
    def retailor(id: str = Form(...)):
        row = db.get(id)
        if row is None:
            raise HTTPException(404)
        _tailor_and_store(row_to_job(row))
        return RedirectResponse(f"/job?id={id}", status_code=303)

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request, "add.html", {"error": error})

    @app.post("/tailor")
    def tailor_now(url: str = Form(""), company: str = Form(""), title: str = Form(""),
                   location: str = Form(""), description: str = Form("")):
        if url.strip() and not description.strip():
            try:
                job = job_from_url(url.strip())
            except Exception:  # noqa: BLE001
                return RedirectResponse("/add?error=link", status_code=303)
        else:
            if not description.strip():
                return RedirectResponse("/add?error=empty", status_code=303)
            key = hashlib.sha1(description.encode()).hexdigest()[:10]
            job = Job(id=f"manual:{key}", source="manual", company=company.strip() or "Company",
                      title=title.strip() or "Role", location=location.strip(), url=url.strip(),
                      apply_url=url.strip(), description=description.strip())
        return RedirectResponse(f"/job?id={_tailor_and_store(job)}", status_code=303)

    # ------------------------------------------------------------------ settings

    FILES = {"profile": "Your resume (profile.yaml)", "answers": "Application answers (answers.yaml)",
             "config": "Settings (config.yaml)"}

    def _path(name: str) -> Path:
        cfg = state["cfg"]
        return {"profile": cfg.resolve(cfg.profile_path), "answers": cfg.resolve(cfg.answers_path),
                "config": config_path}[name]

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: str = "", error: str = "", which: str = ""):
        cfg = state["cfg"]
        texts = {k: (_path(k).read_text() if _path(k).exists() else "") for k in FILES}
        answers_raw = [a for a in (yaml.safe_load(texts["answers"] or "[]") or []) if isinstance(a, dict)]
        answer_rows = [{"i": i, "label": a.get("label") or _pretty_pattern(str(a.get("match", ""))),
                        "value": "" if str(a.get("value")) == "__FILL_ME__" else str(a.get("value", ""))}
                       for i, a in enumerate(answers_raw)]
        todo = [r["label"] for r in answer_rows if not r["value"]]
        return templates.TemplateResponse(request, "settings.html", {
            "files": FILES, "texts": texts, "saved": saved, "error": error, "which": which,
            "cfg": cfg, "run": runner.state, "has_key": bool(cfg.llm.api_key()) or cfg.llm.provider == "offline",
            "todo": todo, "h": header(cfg), "answer_rows": answer_rows,
        })

    @app.post("/settings/answers-form")
    async def save_answers_form(request: Request):
        form = await request.form()
        path = _path("answers")
        items = [a for a in (yaml.safe_load(path.read_text() if path.exists() else "[]") or []) if isinstance(a, dict)]
        for i, item in enumerate(items):
            key = f"a{i}"
            if key in form:
                v = str(form[key]).strip()
                item["value"] = v if v else "__FILL_ME__"
        path.write_text(yaml.safe_dump(items, sort_keys=False, allow_unicode=True, width=1000))
        return RedirectResponse("/settings?saved=answers-form#answers-form", status_code=303)

    @app.post("/settings/{name}")
    def save_settings(name: str, text: str = Form(...)):
        if name not in FILES:
            raise HTTPException(404)
        text = text.replace("\r\n", "\n")
        try:
            data = yaml.safe_load(text)
            if name == "profile":
                Profile.model_validate(data)
            elif name == "config":
                Config.model_validate({**(data or {}), "home": str(state["cfg"].home)})
            elif name == "answers":
                if not isinstance(data, list):
                    raise ValueError("answers.yaml must be a list of '- match: ... / value: ...' items")
        except (yaml.YAMLError, ValidationError, ValueError) as e:
            msg = str(e).splitlines()[0][:300] if not isinstance(e, ValidationError) else \
                "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])
            from urllib.parse import quote
            return RedirectResponse(f"/settings?which={name}&error={quote(msg)}#{name}", status_code=303)
        _path(name).write_text(text)
        if name == "config":
            state["cfg"] = Config.load(config_path)
        return RedirectResponse(f"/settings?saved={name}#{name}", status_code=303)

    @app.post("/api/run")
    def run_now():
        started = runner.start()
        return {"started": started, "running": True}

    @app.get("/api/run")
    def run_status():
        st = runner.state
        return {"running": st.running, "error": st.error, "log": st.log[-8:],
                "finished": st.finished.isoformat() if st.finished else None, "counts": db.counts()}

    @app.post("/api/notify-test")
    def notify_test():
        cfg = state["cfg"]
        ok = notify.send(cfg.notify, "nightshift is connected", "You'll get a note here each morning.")
        return {"ok": ok}

    return app


# ---------------------------------------------------------------------- display helpers


def _initials(company: str) -> str:
    words = [w for w in company.replace("-", " ").split() if w[:1].isalnum()]
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def _pretty_pattern(pattern: str) -> str:
    """Turn an answers.yaml regex into something readable when it has no label."""
    first = pattern.split("|")[0]
    first = re.sub(r"\\b|[\^$?*+()\[\]]", "", first)
    return first[:1].upper() + first[1:] if first else pattern


def _hue(company: str) -> int:
    return int(hashlib.md5(company.lower().encode()).hexdigest()[:4], 16) % 360
