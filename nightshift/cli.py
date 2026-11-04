"""Command line interface.

  nightshift init                     create config.yaml, profile.yaml, answers.yaml
  nightshift tailor JD_OR_URL         tailor resume/CV + cover letter for one job
  nightshift run                      the nightly pipeline
  nightshift serve                    review dashboard (http://127.0.0.1:8765)
  nightshift submit                   fill/submit forms for approved jobs
  nightshift schedule                 run every night via launchd (macOS) or cron
  nightshift boards                   check the company slugs in config.yaml
  nightshift status                   counts by pipeline stage
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from .config import Config
from .db import DB

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Tailor your resume to every job while you sleep.")
EXAMPLES = Path(__file__).parent / "data"


def _cfg(config: Path) -> Config:
    if not config.exists():
        typer.echo(f"No {config} here. Run `nightshift init` first.", err=True)
        raise typer.Exit(1)
    return Config.load(config)


ConfigOpt = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config.yaml")


@app.command()
def init(force: bool = typer.Option(False, help="Overwrite existing files")):
    """Create starter config.yaml, profile.yaml and answers.yaml in this folder."""
    for name in ("config.yaml", "profile.yaml", "answers.yaml"):
        dst = Path(name)
        if dst.exists() and not force:
            typer.echo(f"  keep    {name}")
            continue
        shutil.copy(EXAMPLES / f"{name.split('.')[0]}.example.yaml", dst)
        typer.echo(f"  create  {name}")
    typer.echo("\nNext: put your real resume into profile.yaml (keep every bullet, add more than fits on a page),"
               "\nthen try:  nightshift tailor path/to/job.txt")


@app.command()
def tailor(
    job: str = typer.Argument(..., help="A job description file, or a posting URL"),
    company: str = typer.Option("", help="Company (for pasted descriptions)"),
    title: str = typer.Option("", help="Role title (for pasted descriptions)"),
    location: str = typer.Option("", help="Location; UK/EU/AU/NZ locations get a CV"),
    kind: Optional[str] = typer.Option(None, help="Force 'resume' or 'cv'"),
    out: Optional[Path] = typer.Option(None, help="Output folder"),
    offline: bool = typer.Option(False, help="No LLM: reorder only, never reword"),
    config: Path = ConfigOpt,
):
    """Tailor your resume/CV and cover letter to one job."""
    from .llm import make_llm
    from .models import Job, Profile
    from .pipeline import job_dir
    from .sources import job_from_url
    from .tailor import output_files
    from .tailor import tailor as do_tailor

    cfg = _cfg(config)
    if offline:
        cfg.llm.provider = "offline"
    profile = Profile.load(cfg.resolve(cfg.profile_path))
    if job.startswith(("http://", "https://")):
        j = job_from_url(job)
    else:
        text = Path(job).read_text()
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "Role")
        j = Job(id=f"manual:{hashlib.sha1(text.encode()).hexdigest()[:10]}", source="manual",
                company=company or "Company", title=title or first[:80], location=location, description=text)
    if company:
        j.company = company
    if location:
        j.location = location
    out = out or job_dir(cfg, j)
    typer.echo(f"Tailoring for {j.title} at {j.company} ...")
    res = do_tailor(j, profile, cfg, make_llm(cfg.llm), out, kind=kind)

    db = DB(cfg.db_path)
    db.upsert_new(j)
    db.set(j.id, status="queued", out_dir=str(out),
           notes_json=[f"guard: {v.where}: {v.detail} ({v.action})" for v in res.violations])

    ca, cb = res.coverage_after, res.coverage_before
    files = output_files(out)
    typer.echo(f"\n  {res.doc_kind.upper()}: {files['resume']}  ({res.pages} page{'s' if res.pages != 1 else ''})")
    if files["letter"]:
        typer.echo(f"  Cover letter: {files['letter']}")
    typer.echo(f"  Keywords on the page: {ca.score:.0%}   near the top: {cb.prominence:.0%} -> {ca.prominence:.0%}")
    if ca.gaps:
        typer.echo(f"  Asked for, not in your profile (not added): {', '.join(ca.gaps)}")
    if res.violations:
        typer.echo(f"  Guard fixed {len(res.violations)} thing(s):")
        for v in res.violations:
            typer.echo(f"    - {v.where}: {v.detail} -> {v.action}")
    else:
        typer.echo("  Guard: every line traces back to your profile.")


@app.command()
def run(config: Path = ConfigOpt, offline: bool = typer.Option(False, help="No LLM calls")):
    """The nightly run: discover, filter, score, tailor, queue (and submit approved, if enabled)."""
    from .llm import make_llm
    from .pipeline import run as do_run

    cfg = _cfg(config)
    if offline:
        cfg.llm.provider = "offline"
    report = do_run(cfg, make_llm(cfg.llm), progress=lambda s: typer.echo(f"  {s}"))
    typer.echo("\n" + report.markdown())
    _notify(f"{report.queued} applications ready to review", "nightshift")


@app.command()
def serve(config: Path = ConfigOpt, port: int = 8765, host: str = "127.0.0.1"):
    """Open the review dashboard."""
    import uvicorn

    from .web.app import create_app

    cfg = _cfg(config)
    typer.echo(f"nightshift dashboard: http://{host}:{port}")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


@app.command()
def submit(config: Path = ConfigOpt, live: Optional[bool] = typer.Option(None, help="Override submit.live")):
    """Fill (and with --live, submit) application forms for approved jobs."""
    from .llm import make_llm
    from .models import Profile
    from .submit import submit_approved

    cfg = _cfg(config)
    if live is not None:
        cfg.submit.live = live
    s = submit_approved(DB(cfg.db_path), cfg, Profile.load(cfg.resolve(cfg.profile_path)),
                        llm=make_llm(cfg.llm), progress=lambda m: typer.echo(f"  {m}"))
    typer.echo(f"submitted {s.submitted}, need you: {s.needs_human}")
    for e in s.errors:
        typer.echo(f"  ! {e}")


@app.command()
def boards(config: Path = ConfigOpt,
           prune: bool = typer.Option(False, help="Remember dead boards and skip them from now on")):
    """Check every company job board you're watching (yours + the bundled list if enabled)."""
    from .config import dead_boards_path
    from .sources import check_boards

    cfg = _cfg(config)
    companies = cfg.companies.resolved(cfg.home)
    ok = dead = 0
    newly_dead: list[str] = []
    for source, slug, status in check_boards(companies):
        typer.echo(f"  {source:<10} {slug:<28} {status}")
        if status.startswith("ok"):
            ok += 1
        elif "HTTP 404" in status or "HTTP 410" in status:  # only definite misses; not timeouts
            dead += 1
            newly_dead.append(f"{source}:{slug}")
    typer.echo(f"\n{ok} boards working, {dead} not found.")
    if prune and newly_dead:
        path = dead_boards_path(cfg.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("\n".join(newly_dead) + "\n")
        typer.echo(f"Skipping {len(newly_dead)} dead boards from now on (listed in {path}).")
    elif newly_dead:
        typer.echo("Run again with --prune to skip the dead ones from now on.")


@app.command()
def status(config: Path = ConfigOpt):
    """How many jobs are at each stage."""
    counts = DB(_cfg(config).db_path).counts()
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {k:<14} {v}")


@app.command()
def schedule(
    config: Path = ConfigOpt,
    hour: int = typer.Option(2, help="Hour to run (local time, 0-23)"),
    minute: int = typer.Option(17),
    install: bool = typer.Option(False, help="Install it (otherwise just print it)"),
    remove: bool = typer.Option(False, help="Uninstall"),
):
    """Run every night. macOS: launchd (catches up after sleep). Linux: cron."""
    cfg_path = config.resolve()
    exe = shutil.which("nightshift") or f"{sys.executable} -m nightshift"
    log = cfg_path.parent / "data" / "nightly.log"
    if platform.system() == "Darwin":
        label = "com.nightshift.nightly"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        args = "".join(f"<string>{a}</string>" for a in [*exe.split(), "run", "--config", str(cfg_path)])
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>WorkingDirectory</key><string>{cfg_path.parent}</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
</dict></plist>
"""
        if remove:
            subprocess.run(["launchctl", "unload", str(plist)], check=False)
            plist.unlink(missing_ok=True)
            typer.echo("Removed nightly run.")
            return
        if not install:
            typer.echo(body)
            typer.echo(f"# run with --install to write {plist}")
            return
        log.parent.mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(body)
        subprocess.run(["launchctl", "unload", str(plist)], check=False, capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], check=True)
        typer.echo(f"Installed: runs daily at {hour:02d}:{minute:02d}. If the Mac is asleep it runs on wake.\n"
                   f"To wake it for the run:  sudo pmset repeat wakeorpoweron MTWRFSU {hour:02d}:{max(minute - 5, 0):02d}:00\n"
                   "Put your API key in ~/.zshenv or the plist's EnvironmentVariables.")
        return
    line = f"{minute} {hour} * * * cd {cfg_path.parent} && {exe} run --config {cfg_path} >> {log} 2>&1"
    if not install and not remove:
        typer.echo(line)
        typer.echo("# run with --install to add it to your crontab")
        return
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    kept = [ln for ln in current.splitlines() if "nightshift" not in ln]
    if install:
        kept.append(line)
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)
    typer.echo("Removed." if remove else f"Installed: {line}")


def _notify(msg: str, title: str) -> None:
    if platform.system() == "Darwin" and shutil.which("osascript"):
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'], check=False)


if __name__ == "__main__":
    app()
