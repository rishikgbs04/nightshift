"""Tailor a resume/CV and cover letter to one job description.

Flow:  draft (LLM, or deterministic offline ranking)
    -> guard.enforce (remove anything not backed by the master profile)
    -> fit to page budget (densify, then drop least-relevant bullets)
    -> render PDF + text, measure keyword coverage before/after
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import guard, keywords
from .config import Config
from .llm import LLM, LLMError, for_task
from .models import (
    Job,
    Profile,
    TailoredBullet,
    TailoredEntry,
    TailoredResume,
    TailorResult,
    Violation,
)
from .render import DocStyle, PDFRenderer, count_pages, letter_html, resume_html, resume_text, write

# --------------------------------------------------------------------------- prompts

TAILOR_SYSTEM = """You tailor a candidate's resume to a job description.

You receive the candidate's MASTER PROFILE as JSON (every job and project has
an id, every bullet has an id) and a JOB DESCRIPTION.

Your job is editing, never inventing:
- Choose which projects to include and in what order (most relevant first).
  Keep every experience entry unless it is clearly irrelevant AND space is tight.
- For each included entry, choose the most relevant bullets (2-4 per entry,
  3-5 for the most relevant ones) and order them by relevance.
- You may reword a bullet to foreground what the job cares about, using the
  job's vocabulary ONLY where it truthfully describes what the bullet says.
- You may merge two bullets of the SAME entry into one.
- Each output bullet lists in "sources" the ids of the bullets it came from.

Hard rules (violations are automatically reverted, so breaking them wastes the edit):
- Never add a number, metric, percentage, team size or duration that is not
  in the source bullets. Keep every number exactly as written.
- Never add a technology, tool, language or skill to a bullet unless it is in
  that same entry's bullets or tech list. Do not borrow from other entries.
- Never upgrade the candidate's role: no "led", "managed", "architected",
  "owned", "spearheaded", "mentored" unless the source already says so.
- Keep each bullet under 230 characters. Start with a strong past-tense verb.
- Skills: reorder each category so the job's skills come first. Only use
  skills that exist in the profile; you may drop irrelevant ones.
- section_order: put "projects" first only if the projects are clearly more
  relevant to this job than the work experience.
- Coursework: pick up to 6 relevant courses from the profile's coursework.
{summary_rule}

Return JSON exactly in this shape:
{{
  "summary": "",
  "section_order": ["experience", "projects"],
  "experience": [{{"id": "<entry id>", "bullets": [{{"text": "...", "sources": ["<bullet id>"]}}]}}],
  "projects":   [{{"id": "<entry id>", "bullets": [{{"text": "...", "sources": ["<bullet id>"]}}]}}],
  "skills": {{"<category from profile>": ["skill", "..."]}},
  "coursework": ["..."]
}}"""

SUMMARY_ON = ("- Write a 1-2 sentence summary in third-person-implied style "
              "(no 'I'), using only facts from the profile.")
SUMMARY_OFF = '- Leave "summary" as an empty string.'

SCORE_SYSTEM = """You screen job postings for one candidate. Given the candidate's
profile and a job description, rate how worthwhile it is for this candidate
to apply, from 1 (pointless) to 10 (excellent fit).

Consider: required years of experience vs the candidate's level (student /
new grad / intern), required skills they have or lack, role type match, and
any hard blockers stated in the posting (clearances, citizenship, degree level).
Be calibrated: a 7 means a real shot, not a perfect match.

Return JSON: {"score": <1-10 int>, "reasons": ["short", "..."], "red_flags": ["short", "..."]}"""

LETTER_SYSTEM = """You write concise, specific cover letters.

Write 3 short paragraphs (220-300 words total) for the candidate applying to
the job. No greeting line and no sign-off; those are added by the template.

1. Why this role/company, grounded in specifics from the job description.
2. The 2-3 most relevant things the candidate has done, taken from the profile.
3. A brief, confident close.

Rules: every claim about the candidate must come from the profile. Do not
invent numbers, technologies, employers or outcomes. Do not claim experience
with any technology the profile doesn't list. No clichés like "I am writing
to express my interest". Plain text, paragraphs separated by a blank line."""


# --------------------------------------------------------------------------- helpers


def _profile_for_prompt(profile: Profile) -> str:
    data = profile.model_dump()
    data["basics"] = {"name": profile.basics.name}  # contact details aren't needed by the model
    return json.dumps(data, indent=1)


def _weights(jd: str) -> dict[str, int]:
    return keywords.term_counts(jd)


def _relevance(text: str, w: dict[str, int]) -> float:
    return float(sum(w.get(t, 0) for t in keywords.find_terms(text)))


def is_cv_region(job: Job, cfg: Config) -> bool:
    loc = f" {job.location} ".lower()
    return any(re.search(rf"\b{re.escape(r.lower())}\b", loc) for r in cfg.tailor.cv_regions)


# --------------------------------------------------------------------------- drafting


def draft_offline(profile: Profile, jd: str) -> TailoredResume:
    """Deterministic tailoring: reorder bullets, projects and skills by JD relevance.
    Wording is unchanged, so the result is always fully truthful."""
    w = _weights(jd)

    def ranked(entry) -> TailoredEntry:
        order = sorted(enumerate(entry.bullets), key=lambda ib: (-_relevance(ib[1].text, w), ib[0]))
        return TailoredEntry(id=entry.id, bullets=[TailoredBullet(text=b.text, sources=[b.id]) for _, b in order])

    exp = [ranked(e) for e in profile.experience]
    prj = sorted(
        (ranked(e) for e in profile.projects),
        key=lambda te: -_relevance(" ".join([*profile.entry(te.id).tech, *(b.text for b in te.bullets)]), w),
    )
    skills = {}
    for cat, items in profile.skills.items():
        skills[cat] = sorted(items, key=lambda s: (-_relevance(s, w), items.index(s)))
    cw_all = profile.education[0].coursework if profile.education else []
    cw = sorted(cw_all, key=lambda c: (-_relevance(c, w), cw_all.index(c)))[:6]
    # New grads often have their most relevant work in projects: lead with
    # projects when the best project beats every job on relevance.
    def entry_rel(te):
        e = profile.entry(te.id)
        return _relevance(" ".join([*e.tech, *(b.text for b in e.bullets)]), w)
    best_exp = max((entry_rel(te) for te in exp), default=0)
    best_prj = max((entry_rel(te) for te in prj), default=0)
    order = ["projects", "experience"] if best_prj > 1.5 * best_exp else ["experience", "projects"]
    return TailoredResume(section_order=order, experience=exp, projects=prj, skills=skills, coursework=cw)


def draft_llm(llm: LLM, profile: Profile, job: Job, write_summary: bool) -> TailoredResume:
    system = TAILOR_SYSTEM.format(summary_rule=SUMMARY_ON if write_summary else SUMMARY_OFF)
    user = (f"MASTER PROFILE:\n{_profile_for_prompt(profile)}\n\n"
            f"JOB: {job.title} at {job.company} ({job.location})\n\n"
            f"JOB DESCRIPTION:\n{job.description[:12000]}")
    raw = for_task(llm, "tailor").json(system, user)
    return TailoredResume.model_validate(raw)


def score_job(llm: Optional[LLM], profile: Profile, job: Job) -> dict:
    fit = keywords.profile_fit(job.description, profile)
    if llm is None:
        return {"score": max(1, min(10, round(fit * 10))), "reasons": [f"keyword fit {fit:.0%} (offline)"], "red_flags": []}
    user = (f"CANDIDATE PROFILE:\n{_profile_for_prompt(profile)}\n\n"
            f"JOB: {job.title} at {job.company} ({job.location})\n\nDESCRIPTION:\n{job.description[:10000]}")
    data = for_task(llm, "score").json(SCORE_SYSTEM, user)
    data["score"] = int(max(1, min(10, int(data.get("score", 1)))))
    return data


def write_letter(llm: Optional[LLM], profile: Profile, job: Job, r: TailoredResume) -> str:
    if llm is None:
        return offline_letter(profile, job, r)
    user = (f"CANDIDATE PROFILE:\n{_profile_for_prompt(profile)}\n\n"
            f"BULLETS ALREADY CHOSEN AS MOST RELEVANT:\n"
            + "\n".join(f"- {b.text}" for te in [*r.experience, *r.projects] for b in te.bullets[:2])
            + f"\n\nJOB: {job.title} at {job.company}\n\nDESCRIPTION:\n{job.description[:10000]}")
    return for_task(llm, "letter").text(LETTER_SYSTEM, user).strip()


def _lower_first(s: str) -> str:
    return s if s[:2].isupper() else s[:1].lower() + s[1:]


def offline_letter(profile: Profile, job: Job, r: TailoredResume) -> str:
    """Template letter used without an LLM. Every sentence about you is one of
    your own bullets, so it is truthful by construction."""
    kws = keywords.jd_keywords(job.description)
    have = keywords.find_terms(profile.full_text())
    overlap = [keywords.display(k) for k in kws if k in have][:4]
    if len(overlap) > 1:
        focus = f"The role's focus on {', '.join(overlap[:-1])} and {overlap[-1]} lines up closely with the work I've been doing."
    elif overlap:
        focus = f"The role's focus on {overlap[0]} lines up closely with the work I've been doing."
    else:
        focus = "It lines up closely with the work I've been doing."
    first = f"I'm applying for the {job.title} role at {job.company}. {focus}"

    examples = []
    for te in r.projects[:1]:
        e = profile.entry(te.id)
        examples.append(f"On {e.title.split(':')[0]}, I {_lower_first(te.bullets[0].text).rstrip('.')}.")
    for te in r.experience[:1]:
        e = profile.entry(te.id)
        examples.append(f"At {e.org}, I {_lower_first(te.bullets[0].text).rstrip('.')}.")
    second = " ".join(examples)
    third = (f"I'd welcome the chance to bring that experience to {job.company}. "
             "Thank you for your time and consideration.")
    return "\n\n".join(p for p in [first, second, third] if p)


# --------------------------------------------------------------------------- fitting


def fit_to_pages(profile: Profile, r: TailoredResume, jd: str, max_pages: int,
                 renderer: PDFRenderer, style: DocStyle) -> tuple[TailoredResume, bytes, int]:
    """Make the resume fit max_pages: drop irrelevant bullets, tighten spacing,
    then drop least-relevant bullets/projects. Returns (resume, pdf, pages)."""
    w = _weights(jd)
    r = r.model_copy(deep=True)

    dropped: list[tuple[float, str, str, int, TailoredBullet]] = []

    def candidates():
        out = []
        for kind, entries in (("experience", r.experience), ("projects", r.projects)):
            for ei, te in enumerate(entries):
                if len(te.bullets) <= 1:
                    continue  # every entry keeps its best bullet
                for bi, b in enumerate(te.bullets):
                    # drop first: least relevant, then later in its entry, then older/lower entries
                    out.append((_relevance(b.text, w), -bi, -ei, kind, ei, bi))
        return sorted(out)

    def drop(c) -> None:
        rel, _, _, kind, ei, bi = c
        te = getattr(r, kind)[ei]
        dropped.append((rel, kind, te.id, bi, te.bullets.pop(bi)))

    def render() -> tuple[bytes, int]:
        pdf = renderer.pdf(resume_html(profile, r, style), style.paper)
        return pdf, count_pages(pdf)

    while True:
        pdf, pages = render()
        if pages <= max_pages:
            break
        cands = candidates()
        if cands and cands[0][0] == 0:  # irrelevant to this job: drop before shrinking type
            drop(cands[0])
            continue
        if style.density < 2:
            style.density += 1
            continue
        if cands:
            drop(cands[0])
            continue
        if len(r.projects) > 1:
            r.projects.pop()
            continue
        if len(r.coursework) > 3:
            r.coursework = r.coursework[:3]
            continue
        return r, pdf, pages  # can't shrink further; report the overflow

    # Dropping whole bullets can overshoot and leave a gap at the bottom:
    # put back the most relevant ones that still fit.
    for rel, kind, entry_id, bi, bullet in sorted(dropped, key=lambda d: -d[0]):
        te = next((t for t in getattr(r, kind) if t.id == entry_id), None)
        if te is None:
            continue
        te.bullets.insert(min(bi, len(te.bullets)), bullet)
        new_pdf, new_pages = render()
        if new_pages <= max_pages:
            pdf, pages = new_pdf, new_pages
        else:
            te.bullets.remove(bullet)
    return r, pdf, pages


# --------------------------------------------------------------------------- main entry


def tailor(job: Job, profile: Profile, cfg: Config, llm: Optional[LLM], out_dir: Path,
           renderer: Optional[PDFRenderer] = None, kind: Optional[str] = None) -> TailorResult:
    kind = kind or ("cv" if is_cv_region(job, cfg) else "resume")
    style = DocStyle(paper="A4" if kind == "cv" and is_cv_region(job, cfg) else "Letter", kind=kind)
    max_pages = cfg.tailor.cv_pages if kind == "cv" else cfg.tailor.resume_pages

    notes: list = []
    if llm is None:
        draft = draft_offline(profile, job.description)
    else:
        try:
            draft = draft_llm(llm, profile, job, cfg.tailor.write_summary)
        except (LLMError, ValueError) as e:
            draft = draft_offline(profile, job.description)
            notes.append(Violation(where="tailoring", rule="llm", detail=str(e)[:300],
                                         action="fell back to offline tailoring"))

    resume, violations = guard.enforce(draft, profile)
    violations = notes + violations

    own = renderer is None
    renderer = renderer or PDFRenderer(cfg.submit.browser_channel, cfg.submit.browser_executable)
    if own:
        renderer.__enter__()
    try:
        resume, pdf, pages = fit_to_pages(profile, resume, job.description, max_pages, renderer, style)
        preview = renderer.png(resume_html(profile, resume, style), style.paper)

        letter = ""
        letter_pdf = None
        if cfg.tailor.cover_letter:
            try:
                letter = write_letter(llm, profile, job, resume)
            except LLMError as e:
                letter = offline_letter(profile, job, resume)
                violations.append(Violation(where="cover letter", rule="llm", detail=str(e)[:300],
                                                  action="used offline template"))
            letter, lv = guard.enforce_letter(letter, profile)
            violations += lv
            letter_pdf = renderer.pdf(letter_html(profile, job, letter, style.paper), style.paper)
    finally:
        if own:
            renderer.__exit__(None, None, None)

    base_text = resume_text(profile, draft_offline(profile, ""))
    result = TailorResult(
        resume=resume, cover_letter=letter, violations=violations, doc_kind=kind, pages=pages,
        coverage_before=keywords.coverage(job.description, base_text, profile),
        coverage_after=keywords.coverage(job.description, resume_text(profile, resume), profile),
    )

    stem = re.sub(r"[^A-Za-z0-9]+", "_", profile.basics.name).strip("_")
    label = "CV" if kind == "cv" else "Resume"
    write(out_dir / f"{stem}_{label}.pdf", pdf)
    write(out_dir / f"{stem}_{label}.txt", resume_text(profile, resume))
    write(out_dir / "preview.png", preview)
    if letter_pdf:
        write(out_dir / f"{stem}_Cover_Letter.pdf", letter_pdf)
        write(out_dir / "cover_letter.txt", letter)
    write(out_dir / "job.json", job.model_dump_json(indent=2))
    write(out_dir / "result.json", result.model_dump_json(indent=2))
    return result


def output_files(out_dir: Path) -> dict[str, Optional[Path]]:
    pdfs = sorted(out_dir.glob("*.pdf"))
    return {
        "resume": next((p for p in pdfs if p.stem.endswith(("_Resume", "_CV"))), None),
        "letter": next((p for p in pdfs if p.stem.endswith("_Cover_Letter")), None),
    }
