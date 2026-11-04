"""Build a demo workspace from the example profile and sample postings.

No API key needed (offline tailoring). Then open the dashboard:

    python scripts/demo.py /tmp/nightshift-demo
    nightshift serve --config /tmp/nightshift-demo/config.yaml
"""

import json
import shutil
import sys
from pathlib import Path

import yaml

from nightshift.config import Config
from nightshift.db import DB
from nightshift.models import Job
from nightshift.pipeline import run
from nightshift.sources import parse_ashby, parse_greenhouse, parse_lever

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
DATA = ROOT / "nightshift" / "data"

home = Path(sys.argv[1] if len(sys.argv) > 1 else "nightshift-demo").resolve()
home.mkdir(parents=True, exist_ok=True)
shutil.copy(DATA / "profile.example.yaml", home / "profile.yaml")
shutil.copy(DATA / "answers.example.yaml", home / "answers.yaml")
settings = {
    "llm": {"provider": "offline"},
    "search": {"title_include": ["engineer", "intern"], "title_exclude": ["senior", "staff"],
               "max_age_days": 0, "needs_sponsorship": True},
    "pipeline": {"min_score": 4},
}
(home / "config.yaml").write_text(yaml.safe_dump(settings))
cfg = Config.load(home / "config.yaml")

jobs = [
    *parse_greenhouse("lumenlabs", json.loads((FIX / "greenhouse.json").read_text())),
    *parse_lever("northwind", json.loads((FIX / "lever.json").read_text())),
    *parse_ashby("brightwave", json.loads((FIX / "ashby.json").read_text())),
    Job(id="manual:lumen-ai", source="manual", company="Lumen Labs",
        title="Software Engineer, AI Platform (New Grad)", location="New York, NY (Hybrid)",
        description=(FIX / "jd_ai_platform.txt").read_text()),
]
print(run(cfg, None, db=DB(cfg.db_path), jobs=jobs).markdown())
print(f"Now run:  nightshift serve --config {home / 'config.yaml'}")
