"""Always-on mode: the web app plus a built-in nightly scheduler.

`nightshift server` is what runs in the cloud. Each night at schedule.time it
discovers, scores, tailors (and, if enabled, submits), then sends a push
notification. You only ever open the app.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import notify
from .config import Config
from .db import DB


def next_run(now: datetime, hhmm: str, tz: str) -> datetime:
    """Next occurrence of hh:mm in tz, strictly after now (aware datetimes)."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(ZoneInfo(tz))
    candidate = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= local:
        candidate = (candidate + timedelta(days=1)).replace(hour=h, minute=m)
    return candidate


@dataclass
class RunState:
    running: bool = False
    started: Optional[datetime] = None
    finished: Optional[datetime] = None
    summary: str = ""
    error: str = ""
    log: list[str] = field(default_factory=list)


class Runner:
    """Runs the pipeline at most once at a time, from the scheduler or the app."""

    def __init__(self, config_path: Path, db: Optional[DB] = None,
                 run_fn: Optional[Callable[[Config, DB, Callable[[str], None]], object]] = None):
        self.config_path = Path(config_path)
        self.db = db
        self.state = RunState()
        self._lock = threading.Lock()
        self._run_fn = run_fn or _default_run

    def config(self) -> Config:
        return Config.load(self.config_path)

    def start(self) -> bool:
        """Start a run in the background. False if one is already running."""
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run_locked, daemon=True).start()
        return True

    def run_now(self) -> RunState:
        with self._lock:
            self._run_locked(already_locked=True)
        return self.state

    def _run_locked(self, already_locked: bool = False) -> None:
        st = self.state = RunState(running=True, started=datetime.now().astimezone())

        def progress(msg: str) -> None:
            st.log = (st.log + [msg])[-200:]

        try:
            cfg = self.config()
            db = self.db or DB(cfg.db_path)
            report = self._run_fn(cfg, db, progress)
            st.summary = getattr(report, "markdown", lambda: str(report))()
            counts = db.counts()
            title, msg = notify.morning_message(
                waiting=counts.get("queued", 0),
                auto_approved=getattr(report, "auto_approved", 0),
                submitted=getattr(report, "submitted", 0),
                needs_you=counts.get("needs_human", 0))
            notify.send(cfg.notify, title, msg)
        except Exception as e:  # noqa: BLE001 - surface it in the app, keep the server alive
            st.error = f"{type(e).__name__}: {e}"
            st.log.append(traceback.format_exc()[-2000:])
            try:
                notify.send(self.config().notify, "nightshift couldn't finish last night",
                            st.error[:200], priority="high")
            except Exception:  # noqa: BLE001
                pass
        finally:
            st.running = False
            st.finished = datetime.now().astimezone()
            if not already_locked:
                self._lock.release()


def _default_run(cfg: Config, db: DB, progress: Callable[[str], None]):
    from .llm import make_llm
    from .pipeline import run

    return run(cfg, make_llm(cfg.llm), db=db, progress=progress)


def scheduler_loop(runner: Runner, stop: threading.Event, clock: Callable[[], datetime] = None) -> None:
    clock = clock or (lambda: datetime.now().astimezone())
    while not stop.is_set():
        try:
            cfg = runner.config()
            if not cfg.schedule.enabled:
                stop.wait(300)
                continue
            due = next_run(clock(), cfg.schedule.time, cfg.schedule.timezone)
        except Exception:  # noqa: BLE001 - bad config: re-check in a minute
            stop.wait(60)
            continue
        # sleep in short steps so schedule changes from the app take effect
        while not stop.is_set() and clock() < due:
            stop.wait(min(60.0, max(1.0, (due - clock()).total_seconds())))
            try:
                c = runner.config()
                if (c.schedule.time, c.schedule.timezone, c.schedule.enabled) != (
                        cfg.schedule.time, cfg.schedule.timezone, cfg.schedule.enabled):
                    break
            except Exception:  # noqa: BLE001
                pass
        else:
            if not stop.is_set():
                runner.start()
                time.sleep(1)


def serve(home: Path, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    from .web.app import create_app
    from .web.auth import password

    home = Path(home).resolve()
    config_path = home / "config.yaml"
    ensure_home(home)
    if host not in ("127.0.0.1", "localhost") and not password():
        raise SystemExit("Set NIGHTSHIFT_PASSWORD before exposing nightshift to the internet.")
    cfg = Config.load(config_path)
    db = DB(cfg.db_path)
    runner = Runner(config_path, db)
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(runner, stop), daemon=True).start()
    try:
        uvicorn.run(create_app(cfg, db, runner=runner, config_path=config_path),
                    host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
    finally:
        stop.set()


def ensure_home(home: Path) -> None:
    """First boot on a fresh volume: create starter files the app can edit."""
    import shutil

    data = Path(__file__).parent / "data"
    home.mkdir(parents=True, exist_ok=True)
    for name in ("config", "profile", "answers"):
        dst = home / f"{name}.yaml"
        if not dst.exists():
            shutil.copy(data / f"{name}.example.yaml", dst)
