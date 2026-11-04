"""Fill (and optionally submit) application forms for jobs you approved.

Safety model:
  - Only jobs you clicked "Approve" on are ever touched.
  - Answers come from your profile and answers.yaml. A required question
    with no answer is never guessed: the job goes to "needs_human".
  - Any visible CAPTCHA -> needs_human. Nothing here tries to solve one.
  - submit.live: false (the default) fills the form, takes a screenshot and
    stops before clicking submit.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import guard
from .config import Config
from .db import DB, row_to_job
from .llm import LLM, LLMError, for_task
from .models import Job, Profile
from .tailor import _profile_for_prompt, output_files

FILL_ME = "__FILL_ME__"

COLLECT_FIELDS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0 || el.type === 'file'); };
  const clean = t => (t || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return clean(lb.split(' ').map(i => (document.getElementById(i)||{}).innerText || '').join(' '));
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    let p = el.parentElement; for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = p.querySelector('label, .label, .application-label, legend'); if (l) return clean(l.innerText); }
    return clean(el.name || el.placeholder || el.id);
  };
  const out = []; let i = 0; const seenGroups = new Set();
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || !vis(el)) continue;
    el.setAttribute('data-ns-idx', String(i));
    const f = { idx: i++, type, name: el.name || '', label: labelOf(el),
      required: el.required || el.getAttribute('aria-required') === 'true',
      role: el.getAttribute('role') || '', options: [] };
    if (type === 'select-one' || type === 'select-multiple')
      f.options = [...el.options].map(o => clean(o.text)).filter(Boolean);
    if (type === 'radio' || type === 'checkbox') {
      const own = el.closest('label') ? clean(el.closest('label').innerText)
        : (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`) ? clean(document.querySelector(`label[for="${CSS.escape(el.id)}"]`).innerText) : el.value);
      f.option = own;
      const fs = el.closest('fieldset'); const q = fs && fs.querySelector('legend') ? clean(fs.querySelector('legend').innerText) : '';
      f.label = q || f.label; f.group = el.name || q;
      if (fs && fs.querySelector('[required], [aria-required="true"]')) f.required = true;
    }
    out.push(f);
  }
  const captcha = !!document.querySelector('iframe[src*="recaptcha"][title*="challenge" i], iframe[src*="hcaptcha"], .h-captcha, .cf-turnstile, iframe[src*="challenges.cloudflare"]')
    || /verify you are (a )?human/i.test(document.body.innerText);
  return { fields: out, captcha };
}
"""


@dataclass
class FillResult:
    filled: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    drafted: list[dict] = field(default_factory=list)  # open-ended answers written for this form
    captcha: bool = False


@dataclass
class SubmitSummary:
    submitted: int = 0
    needs_human: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- answers


def load_answers(path: Path) -> list[tuple[re.Pattern, str]]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    return [(re.compile(a["match"], re.I), str(a["value"])) for a in raw if a.get("match") is not None]


def profile_answers(p: Profile) -> list[tuple[re.Pattern, str]]:
    first, _, last = p.basics.name.partition(" ")
    links = {l.label.lower() + " " + l.url.lower(): l.url for l in p.basics.links}
    linkedin = next((u for k, u in links.items() if "linkedin" in k), "")
    github = next((u for k, u in links.items() if "github" in k), "")
    ed = p.education[0] if p.education else None
    pairs = [
        (r"^(legal )?first name|^given name|preferred first name", first),
        (r"^(legal )?last name|^family name|^surname", last),
        (r"^(full )?name$|^your name", p.basics.name),
        (r"e-?mail", p.basics.email),
        (r"phone|mobile", p.basics.phone),
        (r"linkedin", linkedin),
        (r"github", github),
        (r"^(current )?location|city|where are you (currently )?(located|based)", p.basics.location),
        (r"^school|university|college|institution", ed.school if ed else ""),
        (r"^degree", ed.degree if ed else ""),
    ]
    return [(re.compile(k, re.I), v) for k, v in pairs if v]


def answer_for(label: str, answers: list[tuple[re.Pattern, str]]) -> Optional[str]:
    for pat, val in answers:
        if pat.search(label):
            return None if val.strip() == FILL_ME else val
    return None


# --------------------------------------------------------------------------- open-ended questions

# Questions whose answer is a fact about you. These are NEVER drafted by a model:
# they come from answers.yaml / your profile, or the job goes to "Needs you".
FACTUAL = re.compile(
    r"authori[sz]|sponsor|visa|citizen|clearance|salary|compensation|\bpay\b|\brate\b|start date|when can you"
    r"|notice period|relocat|years of|how many years|\bgpa\b|graduat|degree|gender|race|ethnic|hispanic"
    r"|veteran|disabil|pronoun|\bage\b|birth|address|phone|e-?mail|linkedin|github|website|portfolio|url"
    r"|refer|how did you (hear|learn|find)|\bname\b|location|city|country|\bstate\b|zip|postal|signature"
    r"|criminal|convict|background check|drug|consent|agree|acknowledge|certif|privacy|terms", re.I)
OPEN_ENDED = re.compile(
    r"\bwhy\b|tell us|describe|share|explain|what (excites|interests|draws|attracts|motivates)|motivat"
    r"|anything else|additional (information|comments)|about yourself|interest(ed)? in (this|the|our)"
    r"|what would you|how would you|proud|challenge|cover letter", re.I)

ANSWER_SYSTEM = """You fill in one open-ended question on a job application for the candidate.

Write the answer in the candidate's voice, first person, plain text, 60-140 words.
Ground it in specifics from the job description and the candidate's profile.

Rules: every claim about the candidate must come from the profile. Never invent
numbers, technologies, employers, outcomes, dates or personal circumstances.
If the question asks for something the profile cannot answer (personal stories,
preferences, logistics), reply with exactly: SKIP"""


def is_open_ended(label: str, field_type: str) -> bool:
    if not label or FACTUAL.search(label):
        return False
    return field_type == "textarea" or bool(OPEN_ENDED.search(label))


def make_drafter(llm: Optional[LLM], profile: Profile, job: Job,
                 cover_letter: str = "") -> Callable[[str], Optional[str]]:
    """Returns label -> answer (or None to leave it for you)."""
    def draft(label: str) -> Optional[str]:
        if re.search(r"cover letter", label, re.I):
            return cover_letter or None
        if llm is None:
            return None
        user = (f"QUESTION: {label}\n\nJOB: {job.title} at {job.company}\n\n"
                f"JOB DESCRIPTION:\n{job.description[:6000]}\n\nCANDIDATE PROFILE:\n{_profile_for_prompt(profile)}")
        try:
            text = for_task(llm, "answers").text(ANSWER_SYSTEM, user).strip()
        except LLMError:
            return None
        if not text or text.upper().startswith("SKIP"):
            return None
        cleaned, _ = guard.enforce_letter(text, profile)
        return cleaned if len(cleaned.split()) >= 15 else None
    return draft


# --------------------------------------------------------------------------- filling


def fill_form(page, answers: list[tuple[re.Pattern, str]], resume: Optional[Path],
              letter: Optional[Path], drafter: Optional[Callable[[str], Optional[str]]] = None) -> FillResult:
    info = page.evaluate(COLLECT_FIELDS_JS)
    res = FillResult(captcha=info["captcha"])
    groups_done: set[str] = set()
    for f in info["fields"]:
        loc = page.locator(f'[data-ns-idx="{f["idx"]}"]')
        label, t = f["label"], f["type"]
        try:
            if t == "file":
                target = letter if re.search(r"cover", label, re.I) else resume
                if re.search(r"resume|cv|cover|attach", label + " " + f["name"], re.I) and target:
                    loc.set_input_files(str(target))
                    res.filled.append(f"{label} <- {target.name}")
                elif f["required"]:
                    res.missing_required.append(label)
                continue
            if t in ("radio", "checkbox"):
                group = f.get("group") or label
                if group in groups_done:
                    continue
                ans = answer_for(label, answers)
                if ans is None:
                    continue  # handled below once per group
                if ans.lower() in (f.get("option") or "").lower() or (f.get("option") or "").lower() in ans.lower():
                    loc.check()
                    groups_done.add(group)
                    res.filled.append(f"{label} <- {f.get('option')}")
                continue
            ans = answer_for(label, answers)
            if ans is None and drafter and t in ("text", "textarea") and is_open_ended(label, t):
                ans = drafter(label)
                if ans:
                    res.drafted.append({"question": label, "answer": ans})
            if ans is None:
                if f["required"]:
                    res.missing_required.append(label)
                continue
            if t.startswith("select"):
                opt = next((o for o in f["options"] if o.lower() == ans.lower()), None) or \
                    next((o for o in f["options"] if ans.lower() in o.lower()), None)
                if opt is None:
                    if f["required"]:
                        res.missing_required.append(f"{label} (no option matches '{ans}')")
                    continue
                loc.select_option(label=opt)
            elif f["role"] == "combobox":
                loc.fill(ans)
                page.keyboard.press("Enter")
            else:
                loc.fill(ans)
            res.filled.append(f"{label} <- {ans[:40]}")
        except Exception as e:  # noqa: BLE001
            if f["required"]:
                res.missing_required.append(f"{label} (error: {type(e).__name__})")

    # required radio groups nobody answered
    for f in info["fields"]:
        if f["type"] in ("radio", "checkbox") and f["required"]:
            g = f.get("group") or f["label"]
            if g not in groups_done and f["label"] not in res.missing_required:
                res.missing_required.append(f["label"])
                groups_done.add(g)
    return res


CONFIRM = re.compile(r"thank you|application (has been |was )?(received|submitted)|we('ve| have) received", re.I)


def click_submit(page) -> bool:
    btn = page.locator(
        'button[type=submit]:visible, input[type=submit]:visible, '
        'button:has-text("Submit application"):visible, button:has-text("Submit"):visible').first
    btn.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    return bool(CONFIRM.search(page.inner_text("body")))


def submitted_today(db: DB) -> int:
    """Forms filled since local midnight (dry runs count too: they still load the site)."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
    return db.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'submit' AND at >= ?", (since,)).fetchone()[0]


def submit_approved(db: DB, cfg: Config, profile: Profile, llm: Optional[LLM] = None,
                    progress: Callable[[str], None] = lambda s: None,
                    sleep: Callable[[float], None] = time.sleep) -> SubmitSummary:
    from playwright.sync_api import sync_playwright

    summary = SubmitSummary()
    budget = cfg.submit.max_per_day - submitted_today(db)
    if budget <= 0:
        progress(f"daily cap of {cfg.submit.max_per_day} reached; the rest wait for tomorrow")
        return summary
    rows = db.by_status("approved", order="score DESC, updated_at ASC", limit=budget)
    if not rows:
        return summary
    # answers.yaml wins over profile defaults
    answers = load_answers(cfg.resolve(cfg.answers_path)) + profile_answers(profile)
    lo, hi = cfg.submit.delay_seconds

    with sync_playwright() as pw:
        kwargs = {"headless": cfg.submit.headless}
        if cfg.submit.browser_executable:
            kwargs["executable_path"] = cfg.submit.browser_executable
        elif cfg.submit.browser_channel:
            kwargs["channel"] = cfg.submit.browser_channel
        try:
            browser = pw.chromium.launch(**kwargs)
        except Exception:  # noqa: BLE001 - fall back to Playwright's bundled Chromium
            browser = pw.chromium.launch(headless=cfg.submit.headless)
        for i, row in enumerate(rows):
            if i and hi > 0:
                sleep(random.uniform(lo, hi))  # spread applications out
            job = row_to_job(row)
            out = Path(row["out_dir"]) if row["out_dir"] else None
            files = output_files(out) if out else {"resume": None, "letter": None}
            letter_text = (out / "cover_letter.txt").read_text() if out and (out / "cover_letter.txt").exists() else ""
            drafter = make_drafter(llm, profile, job, letter_text) if cfg.submit.answer_open_questions else None
            page = browser.new_page()
            try:
                page.goto(job.apply_url or job.url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
                res = fill_form(page, answers, files["resume"], files["letter"], drafter)
                base = out or cfg.out_dir
                base.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(base / "form.png"), full_page=True)
                if res.drafted:
                    (base / "answers.json").write_text(json.dumps(res.drafted, indent=2))
                notes = [f"filled: {x}" for x in res.filled]
                if res.captcha:
                    db.set(job.id, status="needs_human", error="CAPTCHA on the form; finish this one yourself")
                    summary.needs_human += 1
                elif res.missing_required:
                    db.set(job.id, status="needs_human",
                           error="Unanswered required questions: " + "; ".join(res.missing_required)[:800])
                    summary.needs_human += 1
                elif not files["resume"]:
                    db.set(job.id, status="needs_human", error="No tailored resume found for this job")
                    summary.needs_human += 1
                elif not cfg.submit.live:
                    db.set(job.id, status="needs_human",
                           error="Dry run: form filled (see form.png) but not submitted. Set submit.live: true to auto-submit.")
                    summary.needs_human += 1
                elif click_submit(page):
                    page.screenshot(path=str(base / "confirmation.png"), full_page=True)
                    db.set(job.id, status="submitted", error=None)
                    summary.submitted += 1
                else:
                    page.screenshot(path=str(base / "after_submit.png"), full_page=True)
                    db.set(job.id, status="needs_human",
                           error="Clicked submit but saw no confirmation (see after_submit.png)")
                    summary.needs_human += 1
                db.log("submit", "; ".join(notes)[:2000], job.id)
                progress(f"{job.company}: {db.get(job.id)['status']}")
            except Exception as e:  # noqa: BLE001
                db.set(job.id, status="needs_human", error=f"{type(e).__name__}: {str(e)[:300]}")
                db.log("submit", f"error: {e}"[:500], job.id)
                summary.errors.append(f"submit {job.company} / {job.title}: {e}")
            finally:
                page.close()
        browser.close()
    return summary
