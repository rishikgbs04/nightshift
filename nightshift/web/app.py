"""Morning review dashboard: approve, skip, edit, or tailor a job on demand."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import guard
from ..config import Config
from ..db import DB, STATUSES, row_to_job
from ..llm import make_llm
from ..models import Job, Profile, TailorResult
from ..pipeline import job_dir, profile_cache
from ..render import PDFRenderer, letter_html, write
from ..sources import job_from_url
from ..tailor import output_files, tailor

HERE = Path(__file__).parent
TABS = [("queued", "To review"), ("approved", "Approved"), ("needs_human", "Needs you"),
        ("submitted", "Applied"), ("low_score", "Low score"), ("skipped", "Skipped"),
        ("filtered_out", "Filtered"), ("failed", "Failed")]


def create_app(cfg: Config, db: Optional[DB] = None) -> FastAPI:
    app = FastAPI(title="nightshift")
    db = db or DB(cfg.db_path)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def load_result(row) -> Optional[TailorResult]:
        if not row["out_dir"]:
            return None
        p = Path(row["out_dir"]) / "result.json"
        return TailorResult.model_validate_json(p.read_text()) if p.exists() else None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str = "queued"):
        if status not in STATUSES:
            raise HTTPException(404)
        rows = db.by_status(status)
        return templates.TemplateResponse(request, "index.html", {
            "rows": rows, "status": status, "tabs": TABS, "counts": db.counts(),
            "events": db.events(8),
        })

    @app.get("/job", response_class=HTMLResponse)
    def detail(request: Request, id: str):
        row = db.get(id)
        if row is None:
            raise HTTPException(404)
        result = load_result(row)
        profile = profile_cache(cfg)
        files = output_files(Path(row["out_dir"])) if row["out_dir"] else {}
        entries = {e.id: e for e in profile.entries()}
        out = Path(row["out_dir"]) if row["out_dir"] else None
        drafted = json.loads((out / "answers.json").read_text()) if out and (out / "answers.json").exists() else []
        form_shot = out is not None and (out / "form.png").exists()
        return templates.TemplateResponse(request, "job.html", {
            "row": row, "r": result, "files": files, "entries": entries,
            "drafted": drafted, "form_shot": form_shot,
            "score": json.loads(row["score_json"]) if row["score_json"] else None,
            "notes": json.loads(row["notes_json"]) if row["notes_json"] else [],
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
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    @app.post("/action")
    def action(id: str = Form(...), do: str = Form(...), back: str = Form("queued")):
        row = db.get(id)
        if row is None:
            raise HTTPException(404)
        new = {"approve": "approved", "skip": "skipped", "applied": "submitted", "requeue": "queued"}.get(do)
        if new is None:
            raise HTTPException(400, "unknown action")
        db.set(id, status=new)
        db.log(do, "", id)
        return RedirectResponse(f"/?status={back}", status_code=303)

    @app.post("/letter")
    def save_letter(id: str = Form(...), text: str = Form(...)):
        row = db.get(id)
        if row is None or not row["out_dir"]:
            raise HTTPException(404)
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
        return RedirectResponse(f"/job?id={id}", status_code=303)

    def _tailor_and_store(job: Job) -> str:
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

    @app.post("/tailor")
    def tailor_now(url: str = Form(""), company: str = Form(""), title: str = Form(""),
                   location: str = Form(""), description: str = Form("")):
        if url.strip() and not description.strip():
            try:
                job = job_from_url(url.strip())
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"Couldn't load that link ({e}). Paste the description instead.")
        else:
            if not description.strip():
                raise HTTPException(400, "Paste a job description or a link")
            key = hashlib.sha1(description.encode()).hexdigest()[:10]
            job = Job(id=f"manual:{key}", source="manual", company=company.strip() or "Company",
                      title=title.strip() or "Role", location=location.strip(), url=url.strip(),
                      apply_url=url.strip(), description=description.strip())
        return RedirectResponse(f"/job?id={_tailor_and_store(job)}", status_code=303)

    return app
