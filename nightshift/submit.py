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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from .config import Config
from .db import DB, row_to_job
from .models import Profile
from .tailor import output_files

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


# --------------------------------------------------------------------------- filling


def fill_form(page, answers: list[tuple[re.Pattern, str]], resume: Optional[Path],
              letter: Optional[Path]) -> FillResult:
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


def submit_approved(db: DB, cfg: Config, profile: Profile,
                    progress: Callable[[str], None] = lambda s: None) -> SubmitSummary:
    from playwright.sync_api import sync_playwright

    summary = SubmitSummary()
    rows = db.by_status("approved", order="updated_at ASC", limit=50)
    if not rows:
        return summary
    # answers.yaml wins over profile defaults
    answers = load_answers(cfg.resolve(cfg.answers_path)) + profile_answers(profile)

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
        for row in rows:
            job = row_to_job(row)
            out = Path(row["out_dir"]) if row["out_dir"] else None
            files = output_files(out) if out else {"resume": None, "letter": None}
            page = browser.new_page()
            try:
                page.goto(job.apply_url or job.url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
                res = fill_form(page, answers, files["resume"], files["letter"])
                shot = (out or cfg.out_dir) / "form.png"
                page.screenshot(path=str(shot), full_page=True)
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
                    page.screenshot(path=str((out or cfg.out_dir) / "confirmation.png"), full_page=True)
                    db.set(job.id, status="submitted", error=None)
                    summary.submitted += 1
                else:
                    page.screenshot(path=str((out or cfg.out_dir) / "after_submit.png"), full_page=True)
                    db.set(job.id, status="needs_human",
                           error="Clicked submit but saw no confirmation (see after_submit.png)")
                    summary.needs_human += 1
                db.log("submit", "; ".join(notes)[:2000], job.id)
                progress(f"{job.company}: {db.get(job.id)['status']}")
            except Exception as e:  # noqa: BLE001
                db.set(job.id, status="needs_human", error=f"{type(e).__name__}: {str(e)[:300]}")
                summary.errors.append(f"submit {job.company} / {job.title}: {e}")
            finally:
                page.close()
        browser.close()
    return summary
