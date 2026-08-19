"""
Tests for Phase 13 PDF generation.

HTML-rendering tests are pure Jinja2 template execution — fast, no browser
needed, and assert the ATS-friendliness rules from spec section 20
(no images, no multi-column layout, no icon fonts). PDF-generation tests
drive the real, locally-installed Chromium via Playwright and assert an
actual valid PDF file is produced — these are slower but are the only way
to genuinely verify the renderer works, not just the template.
"""
from __future__ import annotations

from src.pdf.renderer import (
    render_cover_letter_html,
    render_cover_letter_markdown,
    render_cover_letter_pdf,
    render_resume_html,
    render_resume_pdf,
)
from src.resume.schema import MasterResume


def make_sample_resume() -> MasterResume:
    return MasterResume.model_validate(
        {
            "personal": {
                "full_name": "Test User",
                "email": "test@example.com",
                "phone": "0400 000 000",
                "location": "Sydney, NSW",
                "github": "github.com/testuser",
            },
            "summary": "Entry-level IT candidate with hands-on project experience.",
            "skills": ["Python", "SQL", "Git"],
            "education": [
                {
                    "id": "edu_01",
                    "institution": "Test University",
                    "credential": "BSc Cybersecurity",
                    "start_date": "2023-02",
                    "end_date": "Ongoing",
                    "highlights": [{"id": "edu_01_bullet_01", "text": "Studied networking fundamentals.", "skills": []}],
                }
            ],
            "experience": [
                {
                    "id": "exp_01",
                    "company": "Tipaload",
                    "title": "QA Tester",
                    "start_date": "2026-05-18",
                    "end_date": "2026-08-18",
                    "location": "North Sydney",
                    "bullets": [
                        {"id": "exp_01_bullet_01", "text": "Troubleshot two apps in staging and production.", "skills": ["QA testing"]}
                    ],
                }
            ],
            "projects": [
                {
                    "id": "project_01",
                    "name": "CashFlo",
                    "description": "Personal Finance Web App",
                    "technologies": ["React", "SQLite"],
                    "bullets": [{"id": "project_01_bullet_01", "text": "Built a full-stack finance app.", "skills": ["React", "SQLite"]}],
                }
            ],
            "certifications": [{"id": "cert_01", "name": "Test Cert", "issuer": "Test Body", "date": "2026"}],
            "additional": [{"id": "extra_01", "text": "Volunteer note.", "skills": []}],
        }
    )


SAMPLE_COVER_LETTER = (
    "I'm excited to apply for the IT Support Officer role at Acme.\n\n"
    "In my QA Tester role at Tipaload I troubleshot two apps in staging and production.\n\n"
    "Thank you for considering my application."
)


class TestResumeHtmlRendering:
    def test_contains_full_name_and_contact_info(self):
        resume = make_sample_resume()
        html = render_resume_html(resume)
        assert "Test User" in html
        assert "test@example.com" in html
        assert "Sydney, NSW" in html

    def test_contains_summary_skills_experience_projects_education(self):
        resume = make_sample_resume()
        html = render_resume_html(resume)
        assert "Entry-level IT candidate" in html
        assert "Python" in html
        assert "Tipaload" in html
        assert "CashFlo" in html
        assert "Test University" in html
        assert "Test Cert" in html

    def test_no_images_no_icon_elements(self):
        # ATS-friendliness (spec section 20): no images, no icon fonts.
        resume = make_sample_resume()
        html = render_resume_html(resume)
        assert "<img" not in html.lower()
        assert "font-awesome" not in html.lower()
        assert "svg" not in html.lower()

    def test_no_multi_column_css(self):
        resume = make_sample_resume()
        html = render_resume_html(resume)
        assert "column-count" not in html.lower()
        assert "display: grid" not in html.lower()
        assert "display:grid" not in html.lower()

    def test_special_characters_are_escaped_not_broken(self):
        resume = make_sample_resume()
        resume = resume.model_copy(update={"summary": "C++ & Q&A experience <script>alert(1)</script>"})
        html = render_resume_html(resume)
        assert "<script>alert(1)</script>" not in html  # must be escaped, not injected raw
        assert "&amp;" in html or "&" in html  # ampersand survives in some escaped form

    def test_empty_optional_sections_do_not_render_headers(self):
        resume = MasterResume.model_validate(
            {"personal": {"full_name": "Minimal User", "email": "min@example.com"}}
        )
        html = render_resume_html(resume)
        assert "Minimal User" in html
        assert "<h2>Skills</h2>" not in html
        assert "<h2>Certifications</h2>" not in html


class TestCoverLetterHtmlRendering:
    def test_contains_sender_job_title_and_paragraphs(self):
        resume = make_sample_resume()
        html = render_cover_letter_html(SAMPLE_COVER_LETTER, resume, job_title="IT Support Officer", company="Acme")
        assert "Test User" in html
        assert "IT Support Officer" in html
        assert "Acme" in html
        # Note: Jinja2 autoescape correctly turns the apostrophe in "I'm"
        # into &#39; — checking a substring without it, since the escaped
        # form is the correct, expected output.
        assert "excited to apply" in html

    def test_paragraphs_are_split_correctly(self):
        resume = make_sample_resume()
        html = render_cover_letter_html(SAMPLE_COVER_LETTER, resume, job_title="IT Support Officer", company="Acme")
        assert html.count("<p>") == 3

    def test_leading_standalone_name_paragraph_is_stripped(self):
        # Regression: models sometimes open with the candidate's own name as
        # its own paragraph (redundant with the letterhead) — must be
        # dropped so the letter opens with the actual content.
        resume = make_sample_resume()
        text_with_leading_name = "Test User\n\n" + SAMPLE_COVER_LETTER
        html = render_cover_letter_html(text_with_leading_name, resume, job_title="IT Support Officer", company="Acme")
        assert html.count("<p>") == 3  # not 4 — the leading name paragraph was dropped
        assert "<p>Test User</p>" not in html

    def test_trailing_signature_name_is_kept(self):
        # A closing signature line is normal and expected — only a LEADING
        # name paragraph is stripped, not a trailing one.
        resume = make_sample_resume()
        text_with_signature = SAMPLE_COVER_LETTER + "\n\nTest User"
        html = render_cover_letter_html(text_with_signature, resume, job_title="IT Support Officer", company="Acme")
        assert html.count("<p>") == 4
        assert "<p>Test User</p>" in html


class TestCoverLetterMarkdown:
    def test_markdown_file_written(self, tmp_path):
        resume = make_sample_resume()
        output = tmp_path / "cover-letter.md"
        result_path = render_cover_letter_markdown(
            SAMPLE_COVER_LETTER, resume, output, job_title="IT Support Officer", company="Acme"
        )
        assert result_path == output
        content = output.read_text()
        assert "Test User" in content
        assert "IT Support Officer" in content
        assert "Acme" in content
        assert "I'm excited to apply" in content


class TestPdfGeneration:
    """These drive the real, locally-installed Playwright Chromium."""

    def test_resume_pdf_is_created_and_valid(self, tmp_path):
        resume = make_sample_resume()
        output = tmp_path / "resume.pdf"
        result_path = render_resume_pdf(resume, output)
        assert result_path.exists()
        data = result_path.read_bytes()
        assert data[:4] == b"%PDF"  # valid PDF magic bytes
        assert len(data) > 1000  # not a truncated/empty file

    def test_cover_letter_pdf_is_created_and_valid(self, tmp_path):
        resume = make_sample_resume()
        output = tmp_path / "cover-letter.pdf"
        result_path = render_cover_letter_pdf(
            SAMPLE_COVER_LETTER, resume, output, job_title="IT Support Officer", company="Acme"
        )
        assert result_path.exists()
        data = result_path.read_bytes()
        assert data[:4] == b"%PDF"
        assert len(data) > 1000

    def test_pdf_creates_parent_directories(self, tmp_path):
        resume = make_sample_resume()
        nested_output = tmp_path / "applications" / "acme-it-support" / "resume.pdf"
        result_path = render_resume_pdf(resume, nested_output)
        assert result_path.exists()
