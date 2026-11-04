import re

import pytest
from conftest import FIX

from nightshift.submit import CONFIRM, FILL_ME, fill_form, profile_answers


@pytest.fixture
def page(cfg):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        kw = {"executable_path": cfg.submit.browser_executable} if cfg.submit.browser_executable else {}
        b = pw.chromium.launch(**kw)
        p = b.new_page()
        p.goto((FIX / "apply_form.html").as_uri())
        yield p
        b.close()


def _files(tmp_path):
    r, c = tmp_path / "Resume.pdf", tmp_path / "Cover_Letter.pdf"
    r.write_bytes(b"%PDF-1.4 resume")
    c.write_bytes(b"%PDF-1.4 letter")
    return r, c


def test_fills_known_fields_and_never_guesses(page, profile, tmp_path):
    resume, letter = _files(tmp_path)
    answers = [(re.compile("sponsor", re.I), FILL_ME), (re.compile("how did you hear", re.I), "Company website"),
               *profile_answers(profile)]
    res = fill_form(page, answers, resume, letter)
    assert page.input_value("#first_name") == "Jordan"
    assert page.input_value("#last_name") == "Rivera"
    assert page.input_value("#email") == "jordan.rivera@example.com"
    assert page.input_value("#li").startswith("https://linkedin.com")
    assert page.eval_on_selector("#hear", "e => e.value") == "Company website"
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == "Resume.pdf"
    assert page.eval_on_selector("#cover", "e => e.files[0].name") == "Cover_Letter.pdf"
    # the sponsorship radio is __FILL_ME__ and the essay has no answer: both must be left for a human
    assert not page.is_checked("input[name=sponsor][value=yes]") and not page.is_checked("input[name=sponsor][value=no]")
    missing = " | ".join(res.missing_required)
    assert "sponsorship" in missing and "Why do you want" in missing
    assert not res.captcha


def test_answers_radio_groups(page, profile, tmp_path):
    resume, letter = _files(tmp_path)
    answers = [(re.compile("sponsor", re.I), "No"), (re.compile("why do you want", re.I), "Because."),
               *profile_answers(profile)]
    res = fill_form(page, answers, resume, letter)
    assert page.is_checked("input[name=sponsor][value=no]")
    assert res.missing_required == []
    page.click("button[type=submit]")
    assert CONFIRM.search(page.inner_text("body"))
