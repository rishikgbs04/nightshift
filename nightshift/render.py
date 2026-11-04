"""Render tailored resumes and cover letters to PDF with headless Chrome.

HTML + CSS -> PDF keeps the output ATS-friendly (real, selectable text in a
single column with standard section headings) and needs no LaTeX install.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from jinja2 import Environment, PackageLoader, select_autoescape
from pypdf import PdfReader

from .models import Job, Profile, TailoredResume

_env = Environment(loader=PackageLoader("nightshift", "templates"), autoescape=select_autoescape(["html", "j2"]))

# density levels used by the page fitter: (font pt, line height, gap pt, margin)
DENSITY = [
    (10.5, 1.28, 5, "0.55in"),
    (10.0, 1.22, 4, "0.5in"),
    (9.5, 1.18, 3, "0.45in"),
]


@dataclass
class DocStyle:
    paper: str = "Letter"  # "A4" for UK/EU/AU style CVs
    density: int = 0
    kind: str = "resume"


def resume_html(profile: Profile, r: TailoredResume, style: DocStyle) -> str:
    font, line, gap, margin = DENSITY[min(style.density, len(DENSITY) - 1)]
    exp_by = {e.id: e for e in profile.experience}
    prj_by = {e.id: e for e in profile.projects}
    coursework = r.coursework or (profile.education[0].coursework if profile.education else [])
    return _env.get_template("resume.html.j2").render(
        p=profile, r=r,
        experience=[(exp_by[t.id], t) for t in r.experience if t.id in exp_by],
        projects=[(prj_by[t.id], t) for t in r.projects if t.id in prj_by],
        coursework=coursework, extras=profile.extras,
        paper=style.paper, font_pt=font, line=line, gap=gap, margin=margin,
        kind_title="CV" if style.kind == "cv" else "Resume",
    )


def resume_text(profile: Profile, r: TailoredResume) -> str:
    """Plain-text version (what an ATS 'sees'); also used for keyword coverage."""
    exp_by = {e.id: e for e in profile.experience}
    prj_by = {e.id: e for e in profile.projects}
    lines = [profile.basics.name, r.summary]
    for ed in profile.education:
        lines += [ed.school, ed.degree]
    lines += r.coursework or (profile.education[0].coursework if profile.education else [])
    for section in r.section_order:
        if section == "experience":
            for t in r.experience:
                e = exp_by.get(t.id)
                if e:
                    lines += [e.org, e.title, *(b.text for b in t.bullets)]
        else:
            for t in r.projects:
                e = prj_by.get(t.id)
                if e:
                    lines += [e.title, e.org, ", ".join(e.tech), *(b.text for b in t.bullets)]
    for cat, items in r.skills.items():
        lines.append(f"{cat}: {', '.join(items)}")
    lines += [f"{x.label}: {x.text}" for x in profile.extras]
    return "\n".join(x for x in lines if x)


def letter_html(profile: Profile, job: Job, body: str, paper: str = "Letter") -> str:
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    return _env.get_template("letter.html.j2").render(
        p=profile, job=job, paragraphs=paragraphs, paper=paper,
        date=date.today().strftime("%B %-d, %Y"),
    )


def count_pages(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


class PDFRenderer:
    """Keeps one headless browser open; rendering a page then takes ~100 ms."""

    def __init__(self, channel: Optional[str] = "chrome", executable: Optional[str] = None):
        self.channel, self.executable = channel, executable
        self._pw = self._browser = None

    def __enter__(self) -> "PDFRenderer":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = {}
        if self.executable:
            kwargs["executable_path"] = self.executable
        elif self.channel:
            kwargs["channel"] = self.channel
        try:
            self._browser = self._pw.chromium.launch(**kwargs)
        except Exception:
            if not kwargs:
                raise
            # fall back to Playwright's bundled Chromium (`playwright install chromium`)
            self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page()
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def png(self, html: str, paper: str = "Letter") -> bytes:
        """Preview image (browsers can't always show PDFs inline, e.g. on phones)."""
        width = 794 if paper == "A4" else 816  # page width in CSS px at 96 dpi
        page = self._browser.new_page(viewport={"width": width, "height": 1056}, device_scale_factor=1.5)
        try:
            page.set_content(html.replace("<body>", '<body style="padding:0.5in;background:#fff">', 1), wait_until="load")
            return page.screenshot(full_page=True)
        finally:
            page.close()

    def pdf(self, html: str, paper: str = "Letter") -> bytes:
        self._page.set_content(html, wait_until="load")
        return self._page.pdf(format=paper, prefer_css_page_size=True, print_background=True)


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    return path
