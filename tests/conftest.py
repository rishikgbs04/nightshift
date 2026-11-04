import shutil
from pathlib import Path

import pytest

from nightshift.config import Config
from nightshift.models import Profile

ROOT = Path(__file__).parent
FIX = ROOT / "fixtures"
EXAMPLE_PROFILE = ROOT.parent / "nightshift" / "data" / "profile.example.yaml"


class FakeLLM:
    """Returns canned replies; records prompts."""

    def __init__(self, json_replies=None, text_reply=""):
        self.json_replies = list(json_replies or [])
        self.text_reply = text_reply
        self.calls = []

    def json(self, system, user):
        self.calls.append(("json", system, user))
        reply = self.json_replies.pop(0) if len(self.json_replies) > 1 else self.json_replies[0]
        return reply(user) if callable(reply) else reply

    def text(self, system, user):
        self.calls.append(("text", system, user))
        return self.text_reply


@pytest.fixture
def profile() -> Profile:
    return Profile.load(EXAMPLE_PROFILE)


@pytest.fixture
def cfg(tmp_path) -> Config:
    shutil.copy(EXAMPLE_PROFILE, tmp_path / "profile.yaml")
    c = Config(home=tmp_path)
    c.llm.provider = "offline"
    c.submit.browser_executable = shutil.which("google-chrome") or shutil.which("chromium")
    if c.submit.browser_executable is None:
        c.submit.browser_channel = None  # Playwright's bundled Chromium
    return c


@pytest.fixture
def jd() -> str:
    return (FIX / "jd_ai_platform.txt").read_text()
