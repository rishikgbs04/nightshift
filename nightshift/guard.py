"""The truthfulness guard.

The LLM is allowed to *select, reorder and reword* what is already in your
master profile. It is not allowed to add facts. Prompting asks for that; this
module enforces it in code. Any tailored line that breaks a rule is replaced
by your original wording, and the violation is reported in the review UI.

Rules, per tailored bullet:
  1. provenance   every bullet cites >= 1 source bullet id, and all of them
                  belong to the same job/project it is listed under
  2. numbers      every number in the bullet appears in its source bullets
                  (no inflated or invented metrics)
  3. technology   every technology/skill named appears in that same job or
                  project in your profile (no moving Docker into a job where
                  you didn't use it, no adding the JD's stack)
  4. role         ownership verbs (led, managed, architected, ...) only if a
                  source already uses a verb of that family
  5. length       bullets stay one or two lines on the page

Whole-document rules:
  - unknown job/project ids are dropped
  - skills are limited to skills in your profile
  - coursework is limited to coursework in your profile
  - summary sentences go through the number and technology rules against
    the whole profile
"""

from __future__ import annotations

import re

from .keywords import find_terms
from .models import (
    Entry,
    Profile,
    TailoredBullet,
    TailoredEntry,
    TailoredResume,
    Violation,
)

MAX_BULLET_CHARS = 250

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_QUANTITY_WORDS = {"doubled", "tripled", "quadrupled", "halved", "tenfold", "twofold", "threefold"}
_ROLE_FAMILIES: dict[str, set[str]] = {
    "lead": {"led", "lead", "leading", "leads", "spearheaded", "headed", "directed", "championed"},
    "manage": {"managed", "manage", "managing", "supervised", "oversaw", "overseeing"},
    "architect": {"architected", "architecting"},
    "own": {"owned", "own", "owning", "ownership"},
    "mentor": {"mentored", "mentoring", "mentor", "coached"},
    "found": {"founded", "co-founded", "cofounded"},
}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z\-]*", text.lower()))


def _entry_scope_text(entry: Entry) -> str:
    return "\n".join([entry.title, entry.org, *entry.tech, *(b.text for b in entry.bullets)])


def check_bullet(bullet: TailoredBullet, entry: Entry) -> list[tuple[str, str]]:
    """Return (rule, detail) for each rule the bullet breaks."""
    problems: list[tuple[str, str]] = []
    own_ids = {b.id: b for b in entry.bullets}

    if not bullet.sources:
        return [("provenance", "bullet cites no source bullet")]
    foreign = [s for s in bullet.sources if s not in own_ids]
    if foreign:
        return [("provenance", f"cites bullets not under this entry: {', '.join(foreign)}")]

    src_text = "\n".join(own_ids[s].text for s in bullet.sources)

    new_nums = _numbers(bullet.text) - _numbers(src_text)
    if new_nums:
        problems.append(("numbers", f"numbers not in source: {', '.join(sorted(new_nums))}"))

    qty = (_words(bullet.text) & _QUANTITY_WORDS) - _words(src_text)
    if qty:
        problems.append(("numbers", f"quantity claims not in source: {', '.join(sorted(qty))}"))

    new_tech = find_terms(bullet.text) - find_terms(_entry_scope_text(entry))
    if new_tech:
        problems.append(("technology", f"not used in this role/project: {', '.join(sorted(new_tech))}"))

    words, src_words = _words(bullet.text), _words(src_text)
    for fam, verbs in _ROLE_FAMILIES.items():
        if words & verbs and not src_words & verbs:
            problems.append(("role", f"claims '{sorted(words & verbs)[0]}' but source has no {fam}-type verb"))

    if len(bullet.text) > MAX_BULLET_CHARS:
        problems.append(("length", f"{len(bullet.text)} chars > {MAX_BULLET_CHARS}"))

    return problems


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", s.lower())


def enforce(draft: TailoredResume, profile: Profile) -> tuple[TailoredResume, list[Violation]]:
    """Return a resume that only contains facts from the profile, plus what was fixed."""
    violations: list[Violation] = []

    def fix_entries(entries: list[TailoredEntry], allowed: list[Entry], kind: str) -> list[TailoredEntry]:
        by_id = {e.id: e for e in allowed}
        out: list[TailoredEntry] = []
        seen_entries: set[str] = set()
        for te in entries:
            entry = by_id.get(te.id)
            if entry is None or te.id in seen_entries:
                violations.append(Violation(
                    where=f"{kind}:{te.id}", rule="provenance",
                    detail="unknown or duplicate entry id", action="dropped entry"))
                continue
            seen_entries.add(te.id)
            fixed: list[TailoredBullet] = []
            used: set[str] = set()
            for i, b in enumerate(te.bullets):
                problems = check_bullet(b, entry)
                if problems:
                    fallback = next((s for s in b.sources if s in {x.id for x in entry.bullets} and s not in used), None)
                    for rule, detail in problems:
                        violations.append(Violation(
                            where=f"{entry.id} bullet {i + 1}", rule=rule, detail=detail,
                            action="reverted to your original wording" if fallback else "dropped bullet"))
                    if fallback is None:
                        continue
                    orig = profile.bullet(fallback)
                    b = TailoredBullet(text=orig.text, sources=[fallback])
                if set(b.sources) <= used and len(b.sources) == 1:
                    # the same source line twice is padding, not tailoring
                    continue
                used.update(b.sources)
                b.original = " / ".join(profile.bullet(s).text for s in b.sources)
                fixed.append(b)
            if fixed:
                out.append(TailoredEntry(id=te.id, bullets=fixed))
        return out

    experience = fix_entries(draft.experience, profile.experience, "experience")
    # Experience stays reverse-chronological, in master-profile order: reordering
    # jobs reads as hiding something. Projects may be reordered by relevance.
    order = {e.id: i for i, e in enumerate(profile.experience)}
    experience.sort(key=lambda e: order[e.id])
    projects = fix_entries(draft.projects, profile.projects, "projects")

    # skills: keep only what the profile lists, in the order the draft chose
    known = {_norm(s): (cat, s) for cat, group in profile.skills.items() for s in group}
    skills: dict[str, list[str]] = {}
    for cat, group in draft.skills.items():
        for s in group:
            hit = known.get(_norm(s))
            if hit is None:
                violations.append(Violation(where="skills", rule="technology",
                                            detail=f"'{s}' is not in your profile", action="removed"))
                continue
            real_cat, real_name = hit
            bucket = skills.setdefault(real_cat if cat not in profile.skills else cat, [])
            if real_name not in bucket:
                bucket.append(real_name)
    if not skills:
        skills = {k: list(v) for k, v in profile.skills.items()}

    known_cw = {_norm(c): c for ed in profile.education for c in ed.coursework}
    coursework = []
    for c in draft.coursework:
        if _norm(c) in known_cw:
            coursework.append(known_cw[_norm(c)])
        else:
            violations.append(Violation(where="coursework", rule="provenance",
                                        detail=f"'{c}' is not in your profile", action="removed"))

    summary = draft.summary.strip()
    if summary:
        full = profile.full_text()
        bad_nums = _numbers(summary) - _numbers(full)
        bad_tech = find_terms(summary) - find_terms(full)
        if bad_nums or bad_tech:
            detail = "; ".join(filter(None, [
                f"numbers {sorted(bad_nums)}" if bad_nums else "",
                f"technologies {sorted(bad_tech)}" if bad_tech else "",
            ]))
            violations.append(Violation(where="summary", rule="technology" if bad_tech else "numbers",
                                        detail=f"not in profile: {detail}", action="removed summary"))
            summary = ""

    order = [x for x in draft.section_order if x in ("experience", "projects")]
    order = list(dict.fromkeys(order + ["experience", "projects"]))
    return TailoredResume(summary=summary, section_order=order, experience=experience, projects=projects,
                          skills=skills, coursework=coursework), violations


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
_FIRST_PERSON = re.compile(r"\b(I|I've|I'm|I'd|my)\b")


def enforce_letter(text: str, profile: Profile) -> tuple[str, list[Violation]]:
    """A cover letter may talk about the company and the role freely, but any
    sentence about *you* may only claim numbers and technologies that are in
    your profile. Offending sentences are removed."""
    full_nums, full_tech = _numbers(profile.full_text()), find_terms(profile.full_text())
    violations: list[Violation] = []
    paras_out: list[str] = []
    for pi, para in enumerate(p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()):
        kept: list[str] = []
        for sent in _SENT.split(para.strip()):
            if _FIRST_PERSON.search(sent):
                bad_nums = _numbers(sent) - full_nums
                bad_tech = find_terms(sent) - full_tech
                if bad_nums or bad_tech:
                    what = sorted(bad_nums) + sorted(bad_tech)
                    violations.append(Violation(
                        where=f"cover letter para {pi + 1}",
                        rule="technology" if bad_tech else "numbers",
                        detail=f"claims not in your profile: {', '.join(what)}",
                        action="removed sentence"))
                    continue
            kept.append(sent)
        if kept:
            paras_out.append(" ".join(kept))
    return "\n\n".join(paras_out), violations
