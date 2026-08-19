"""
Phase 13 — PDF generation.

HTML/CSS -> Chromium/PDF, entirely local (spec section 20). Jinja2 renders
the fixed templates in templates/resume.html and templates/cover-letter.html
against structured data; Playwright drives a local headless Chromium to
rasterize that HTML to a PDF. No cloud rendering service, no paid API.

The templates are deliberately plain (single column, no images/icons, no
unusual fonts) for ATS compatibility, and the AI never touches them — it
only ever supplies the data (a MasterResume, or cover letter text) that
fills the placeholders. This keeps the visual design constant across every
application, per spec section 17.
"""
from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import PROJECT_ROOT
from src.resume.schema import MasterResume

TEMPLATES_DIR = PROJECT_ROOT / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_resume_html(resume: MasterResume) -> str:
    """Render the resume template against a (typically tailored) MasterResume."""
    template = _env.get_template("resume.html")
    return template.render(
        personal=resume.personal,
        summary=resume.summary,
        skills=resume.skills,
        experience=resume.experience,
        projects=resume.projects,
        education=resume.education,
        certifications=resume.certifications,
        additional=resume.additional,
    )


def _split_paragraphs(text: str, *, full_name: str = "") -> list[str]:
    """
    Cover letters come back as prose with blank-line-separated paragraphs.

    Defensive check: models sometimes open with the candidate's own name as
    its own leading paragraph (as if signing at the top) despite being told
    not to — redundant since the letterhead above already shows it, and it
    reads oddly ahead of the actual opening line. Only the LEADING instance
    is stripped; a closing signature line with the same name is normal and
    expected, so it's left alone.
    """
    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    if paragraphs and full_name and paragraphs[0].strip(" .,:").lower() == full_name.strip().lower():
        paragraphs = paragraphs[1:]
    return paragraphs


def render_cover_letter_html(
    cover_letter_text: str,
    resume: MasterResume,
    *,
    job_title: str,
    company: str,
    letter_date: date_cls | None = None,
) -> str:
    template = _env.get_template("cover-letter.html")
    return template.render(
        personal=resume.personal,
        job_title=job_title,
        company=company,
        date=(letter_date or date_cls.today()).strftime("%d %B %Y"),
        paragraphs=_split_paragraphs(cover_letter_text, full_name=resume.personal.full_name),
    )


def _html_to_pdf_bytes(html: str) -> bytes:
    # Imported lazily so importing this module doesn't require Playwright's
    # browser binaries to be installed unless a PDF is actually requested
    # (HTML-only rendering/tests don't need them at all).
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def render_resume_pdf(resume: MasterResume, output_path: str | Path) -> Path:
    html = render_resume_html(resume)
    pdf_bytes = _html_to_pdf_bytes(html)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
    return path


def render_cover_letter_pdf(
    cover_letter_text: str,
    resume: MasterResume,
    output_path: str | Path,
    *,
    job_title: str,
    company: str,
    letter_date: date_cls | None = None,
) -> Path:
    html = render_cover_letter_html(cover_letter_text, resume, job_title=job_title, company=company, letter_date=letter_date)
    pdf_bytes = _html_to_pdf_bytes(html)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
    return path


def render_cover_letter_markdown(
    cover_letter_text: str,
    resume: MasterResume,
    output_path: str | Path,
    *,
    job_title: str,
    company: str,
    letter_date: date_cls | None = None,
) -> Path:
    """Plain-text/markdown copy of the cover letter (spec: cover-letter.md)."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n\n".join(
        part
        for part in [
            f"**{resume.personal.full_name}**\n{resume.personal.email}",
            (letter_date or date_cls.today()).strftime("%d %B %Y"),
            f"Re: {job_title}" + (f" — {company}" if company else ""),
            *_split_paragraphs(cover_letter_text, full_name=resume.personal.full_name),
        ]
        if part
    )
    path.write_text(content + "\n")
    return path
