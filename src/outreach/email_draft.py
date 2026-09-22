"""
Outreach email drafting — write a short, human outreach email and refuse to
store it unless it survives the fabrication guard.

Structured exactly like src/cover_letter/generator.py, which already solves
this problem for cover letters: build the prompt only from facts we hold,
validate the output with the shared fabrication_guard heuristics, feed the
specific failures back for a retry, and raise rather than return unvalidated
prose. The differences are register and grounding — this is a short cold
email from a student, not a cover letter for a specific advertised job, and
its company-side grounding pool is the real postings gathered by
research.py rather than one job description.

What this module will NOT do, by construction:

  - claim a skill, project, employer or certification not in the master
    resume (fabrication guard, plus personalization.py already reduced
    candidate_overlap to real resume skills)
  - state a fact about the company not present in its own postings
    (same guard, with the postings as the allowed pool)
  - pretend to be applying for a specific advertised role (checked
    explicitly — this is general outreach)
  - send anything. It writes a DRAFT row and stops.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

from src.ai.base import AIProvider, AIResponseError
from src.ai.factory import get_ai_provider
from src.config import settings
from src.cover_letter.generator import CLICHE_PHRASES
from src.database import outreach_repo
from src.database.models import OutreachStatus
from src.resume.fabrication_guard import find_fabricated_numbers, find_suspicious_terms
from src.resume.schema import MasterResume

from .personalization import PersonalizationResult
from .research import CompanyResearch

logger = logging.getLogger("job_hunter.outreach.email_draft")

# The cover letter's cliché list, plus the phrasings that specifically make a
# cold email read as machine-written. Kept as one list so both documents get
# the benefit of anything added to either.
OUTREACH_CLICHES = CLICHE_PHRASES + [
    "i am writing to express",
    "i am reaching out to express",
    "passionate and motivated",
    "highly motivated individual",
    "excellent addition to your team",
    "valuable addition to your team",
    "i am excited about the opportunity",
    "excited about the opportunity to",
    "unique skill set",
    "innovative culture",
    "aligns perfectly",
    "aligns with my passion",
    "cutting-edge",
    "industry-leading",
    "world-class",
    "esteemed organization",
    "esteemed company",
    "i hope this email finds you well",
    "i would welcome the opportunity to discuss",
    "at your earliest convenience",
    "please do not hesitate to",
    "thank you for considering my application",
]

# Phrases that mean "I am applying to this specific advertised role". This is
# general outreach — a company that never advertised the role the email
# claims to be applying for will simply bin it.
APPLICATION_PHRASES = [
    "i am applying for",
    "i'm applying for",
    "my application for",
    "apply for the position",
    "applying for the position",
    "applying for the role of",
    "in response to your advertisement",
    "i saw your job posting for",
    "as advertised on",
]

# Unfilled template markers. A real person does not send "[Company]".
PLACEHOLDER_RE = re.compile(r"[\[{]{1,2}\s*[A-Za-z_][A-Za-z0-9 _-]*\s*[\]}]{1,2}")

_SUBJECT_RE = re.compile(r"^\s*subject\s*:\s*(.+)$", re.IGNORECASE)

SUBJECT_MAX_WORDS = 10

SYSTEM_PROMPT = (
    "You write short, natural emails as a university student in Australia reaching out to a "
    "company directly about entry-level opportunities. Write the way a real person types an "
    "email: plain words, contractions, no marketing language, no cover-letter formality. Use "
    "ONLY the facts you are given about the candidate and the company — never invent a skill, "
    "project, employer, certification, achievement, or anything at all about the company. Be "
    "honest that the candidate is a student looking for a first opportunity; do not oversell "
    "them. Never use phrases like 'I am writing to express my interest', 'passionate', "
    "'leverage my skills', 'excited about the opportunity', or 'perfect fit'."
)

_PROMPT_TEMPLATE = """You are writing ONE email for this candidate to send to {company}.

CANDIDATE FACTS (the only true things about the candidate — do not add anything):

Name: {full_name}
Studying: {education}
Summary: {summary}
Skills: {skills}
Experience and projects:
{bullets}
Links to include at the end: {links}

WHAT {company} APPEARS TO HIRE FOR (taken from their own current job ads):

Roles currently advertised: {titles}
Skills that come up repeatedly: {recurring_skills}
Tools and technologies they name: {tools}
Responsibilities these roles involve: {responsibilities}
Experience they ask for: {experience}
Words they use themselves: {terminology}
Notes: {notes}

WHERE THE CANDIDATE GENUINELY OVERLAPS (verified against their real resume —
these are the ONLY skills you may say the candidate has in relation to this company):
{overlap}

Write the email. Requirements:
- Between {min_words} and {max_words} words for the body. Short is the point.
- First line must be "Subject: ..." — under {subject_max_words} words, plain and specific, no colon-heavy
  marketing phrasing. Then a blank line, then the email body.
- Open the body with "Hi there," unless a specific person is named above (none is).
- This is NOT an application for an advertised job. The candidate is introducing themselves and
  asking to be considered for entry-level, junior, internship or support-type opportunities.
  Never say they are applying for a specific advertised position.
- Say something concrete about {company} drawn from the roles/skills listed above — show you
  actually looked at what they're hiring for. Do NOT copy sentences from their job ads; refer to
  what those ads suggest in your own plain words.
- Connect 2-3 of the verified overlap items above to what the candidate has actually built or done,
  using the experience and projects listed. Be specific and brief.
- Be honest that they're a student after a first proper opportunity in the industry. Confident,
  not desperate, not boastful.
- Ask to be considered, or to be pointed to the right person. One clear ask, no pressure.
- Do NOT invent anything about {company} — no praise for their culture, mission, products, size or
  reputation. You only know what their job ads imply.
- Do NOT claim any skill not in the verified overlap list above.
- Do NOT use bracketed placeholders like "[Company]" — this is final text.
- Sign off with the candidate's first name, then the links listed above on their own lines.
- Output ONLY the subject line and email body. No explanation, no markdown, no quotes around it.
"""


@dataclass
class EmailDraft:
    subject: str
    body: str

    @property
    def word_count(self) -> int:
        return len(self.body.split())


@dataclass
class DraftResult:
    """
    Outcome of drafting for one company. `stored` is the thing to check: a
    draft that failed validation is reported with reasons and NOT written to
    the database, rather than being repaired with invented content.
    """

    company_id: int | None = None
    company: str = ""
    draft: EmailDraft | None = None
    message_id: int | None = None
    stored: bool = False
    problems: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.stored and self.message_id is not None


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

def _bullets_text(resume: MasterResume) -> str:
    """The candidate's real history, same shape the cover-letter prompt uses."""
    lines: list[str] = []
    for exp in resume.experience:
        lines.append(f"- {exp.title} at {exp.company}:")
        for bullet in exp.bullets:
            lines.append(f"    - {bullet.text}")
    for proj in resume.projects:
        techs = ", ".join(proj.technologies)
        lines.append(f"- Project: {proj.name}" + (f" ({techs})" if techs else "") + ":")
        for bullet in proj.bullets:
            lines.append(f"    - {bullet.text}")
    return "\n".join(lines) if lines else "(none)"


def _education_text(resume: MasterResume) -> str:
    parts = [
        f"{edu.credential} at {edu.institution}".strip()
        for edu in resume.education
        if edu.credential or edu.institution
    ]
    return "; ".join(parts) or "(not provided)"


def profile_links(resume: MasterResume) -> list[str]:
    """
    Only links that are actually configured. An empty LinkedIn or portfolio
    field must never become a placeholder in a real email.
    """
    candidates = [
        ("LinkedIn", resume.personal.linkedin),
        ("Portfolio", resume.personal.portfolio),
        ("GitHub", resume.personal.github),
    ]
    return [f"{label}: {value.strip()}" for label, value in candidates if value and value.strip()]


def _joined(items: list[str], empty: str = "(none identified)") -> str:
    return ", ".join(items) if items else empty


def as_sentences(text: str) -> str:
    """
    Turn each line into its own sentence for the fabrication guard's benefit
    (see validate_outreach_email). Purely a punctuation change — no words are
    added or removed, so nothing can be hidden from the guard by it.
    """
    return re.sub(r"\s*\n+\s*", ". ", (text or "").strip())


def build_prompt(
    resume: MasterResume, research: CompanyResearch, personalization: PersonalizationResult
) -> str:
    analysis = personalization.analysis
    links = profile_links(resume)
    return _PROMPT_TEMPLATE.format(
        company=research.company,
        full_name=resume.personal.full_name or "(not provided)",
        education=_education_text(resume),
        summary=resume.summary or "(none provided)",
        skills=_joined(resume.skills, "(none listed)"),
        bullets=_bullets_text(resume),
        links=_joined(links, "(none configured — do not mention any links)"),
        titles=_joined(research.titles),
        recurring_skills=_joined(analysis.recurring_skills),
        tools=_joined(analysis.tools_and_technologies),
        responsibilities=_joined(analysis.responsibilities),
        experience=_joined(analysis.experience_requirements),
        terminology=_joined(analysis.terminology),
        notes=analysis.notes or "(none)",
        overlap=_joined(analysis.candidate_overlap, "(no verified overlap)"),
        min_words=settings.outreach_email_min_words,
        max_words=settings.outreach_email_max_words,
        subject_max_words=SUBJECT_MAX_WORDS,
    )


def split_subject_body(text: str) -> EmailDraft:
    """
    Split the model's output into subject and body. A missing subject line
    yields an empty subject, which validation then rejects — better than
    silently inventing one or promoting the first body line.
    """
    lines = (text or "").strip().splitlines()
    subject = ""
    start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _SUBJECT_RE.match(line)
        if match:
            subject = match.group(1).strip().strip('"')
            start = index + 1
        break
    body = "\n".join(lines[start:]).strip()
    return EmailDraft(subject=subject, body=body)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_outreach_email(
    draft: EmailDraft,
    resume: MasterResume,
    research: CompanyResearch,
    personalization: PersonalizationResult,
    *,
    min_words: int | None = None,
    max_words: int | None = None,
) -> list[str]:
    """
    Return a list of problems (empty = valid). Never raises.

    The grounding pool is everything true about the candidate plus the
    company's own posting text — so referring to the company's real
    terminology is fine, while a technology neither the resume nor the
    postings mention is flagged.
    """
    problems: list[str] = []

    if not draft.body.strip():
        return ["Email body is empty"]
    if not draft.subject.strip():
        problems.append("Email has no subject line")
    elif len(draft.subject.split()) > SUBJECT_MAX_WORDS:
        problems.append(
            f"Subject line is too long: {len(draft.subject.split())} words "
            f"(maximum {SUBJECT_MAX_WORDS})"
        )

    lo = min_words if min_words is not None else settings.outreach_email_min_words
    hi = max_words if max_words is not None else settings.outreach_email_max_words
    count = draft.word_count
    if count < lo:
        problems.append(f"Email is too short: {count} words (minimum {lo})")
    if count > hi:
        problems.append(f"Email is too long: {count} words (maximum {hi})")

    combined = f"{draft.subject}\n{draft.body}"
    lowered = combined.lower()
    # The fabrication guard treats only . ! ? as sentence boundaries, since
    # it was built for paragraph prose. An email is line-structured: the
    # greeting, the sign-off and each link sit on their own line and rarely
    # end in a full stop, so without this every "Hi there," and "Test," gets
    # flagged as an unknown capitalized term. Presenting each line as its own
    # sentence restores the guard's intended behaviour without weakening it —
    # it changes which words count as sentence-initial, not which words exist.
    # Proper-noun detection runs on the body only — see the same note in
    # quality_gate.py. Title-cased subject lines otherwise flag ordinary
    # words like "Opportunities" as fabricated proper nouns.
    guard_text = as_sentences(draft.body)

    found_cliches = [phrase for phrase in OUTREACH_CLICHES if phrase in lowered]
    if found_cliches:
        problems.append(f"Contains generic/AI-sounding phrasing: {sorted(found_cliches)}")

    found_application = [phrase for phrase in APPLICATION_PHRASES if phrase in lowered]
    if found_application:
        problems.append(
            f"Reads as an application to a specific advertised job, but this is general "
            f"outreach: {sorted(found_application)}"
        )

    placeholders = PLACEHOLDER_RE.findall(combined)
    if placeholders:
        problems.append(f"Contains unfilled template placeholder(s): {placeholders}")

    # Company-side evidence: only what their real postings actually said.
    evidence = [research.company]
    for posting in research.postings:
        evidence.extend([posting.title, posting.description, posting.location])

    allowed_texts = (
        resume.all_text_fragments()
        + resume.skills
        + resume.identifying_names()
        + profile_links(resume)
        + personalization.analysis.candidate_overlap
        + evidence
    )

    fabricated_numbers = find_fabricated_numbers(as_sentences(combined), allowed_texts)
    if fabricated_numbers:
        problems.append(
            f"Email contains number(s) not traceable to the resume or the company's postings: "
            f"{sorted(fabricated_numbers)} — possible fabricated metric"
        )

    suspicious = find_suspicious_terms(guard_text, allowed_texts, resume.all_skills())
    if suspicious:
        problems.append(
            f"Email mentions term(s) found in neither the resume nor the company's postings: "
            f"{sorted(suspicious)} — possible fabricated skill or claim about the company"
        )

    return problems


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def generate_outreach_email(
    resume: MasterResume,
    research: CompanyResearch,
    personalization: PersonalizationResult,
    *,
    provider: AIProvider | None = None,
    max_retries: int = settings.ollama_max_retries,
) -> EmailDraft:
    """
    Generate and validate one outreach email.

    Raises AIResponseError if no valid, non-fabricating email can be produced
    within max_retries + 1 attempts. Callers must fail safe — an email that
    could not be validated is not stored, and is certainly not sent.
    """
    ai = provider or get_ai_provider()
    base_prompt = build_prompt(resume, research, personalization)
    prompt = base_prompt

    last_problems: list[str] = ["(no attempts made)"]
    for attempt in range(max_retries + 1):
        try:
            raw = ai.generate(prompt, system=SYSTEM_PROMPT, temperature=0.6).strip()
        except AIResponseError as exc:
            last_problems = [str(exc)]
            logger.warning(
                "Outreach email attempt %d/%d failed to generate for %r: %s",
                attempt + 1, max_retries + 1, research.company, exc,
            )
            prompt = base_prompt
            continue

        draft = split_subject_body(raw)
        problems = validate_outreach_email(draft, resume, research, personalization)
        if not problems:
            return draft

        last_problems = problems
        logger.warning(
            "Outreach email attempt %d/%d failed validation for %r: %s",
            attempt + 1, max_retries + 1, research.company, "; ".join(problems),
        )
        # Feed the specific failures back rather than repeating the same
        # prompt — the same self-correcting loop the cover-letter generator
        # uses, which matters most for word count and cliché removal.
        prompt = (
            f"{base_prompt}\n\nYour previous attempt had these problems — fix them this time:\n"
            + "\n".join(f"- {p}" for p in problems)
        )

    raise AIResponseError(
        f"Outreach email generation failed for {research.company!r} after "
        f"{max_retries + 1} attempts: {'; '.join(last_problems)}"
    )


# --------------------------------------------------------------------------
# Orchestration + storage
# --------------------------------------------------------------------------

def draft_outreach_email(
    conn: sqlite3.Connection,
    company: dict | sqlite3.Row,
    research: CompanyResearch,
    resume: MasterResume,
    personalization: PersonalizationResult,
    *,
    provider: AIProvider | None = None,
    max_retries: int = settings.ollama_max_retries,
) -> DraftResult:
    """
    Write an outreach email for one company and store it as a DRAFT.

    Never raises: an AI failure, a validation failure, or a company that
    should not be contacted all come back as a DraftResult with `stored`
    False and a reason. Nothing is sent, and the caller commits.
    """
    company_id = company["id"]
    result = DraftResult(company_id=company_id, company=company["name"])

    if outreach_repo.is_do_not_contact(conn, company_id):
        result.skipped_reason = "company is marked do-not-contact"
        return result
    if outreach_repo.has_pending_message(conn, company_id):
        result.skipped_reason = "company already has a draft or approved message waiting"
        return result
    if not research.has_postings:
        result.skipped_reason = (
            f"no real postings found for this company — nothing to personalize from "
            f"({research.error or 'no readable public job board'})"
        )
        return result
    if not personalization.has_overlap:
        result.skipped_reason = (
            "no verified overlap between this company's postings and the candidate's real skills"
        )
        return result

    try:
        draft = generate_outreach_email(
            resume, research, personalization, provider=provider, max_retries=max_retries
        )
    except AIResponseError as exc:
        # Rejected, not repaired. A draft that could not be validated is
        # never stored with invented content patched in.
        result.problems = [str(exc)]
        result.skipped_reason = "draft failed validation and was rejected"
        logger.warning("Rejected outreach draft for %r: %s", result.company, exc)
        return result

    result.draft = draft
    result.message_id = outreach_repo.insert_message(
        conn,
        {
            "company_id": company_id,
            "recipient_email": _optional(company, "contact_email"),
            "subject": draft.subject,
            "body": draft.body,
            # Kept with the message so the review UI can show why this email
            # says what it says, including what verification threw away.
            "analysis": personalization.to_dict(),
            # The posting text this email was written from, so the quality
            # gate can re-verify its wording against real evidence later
            # without refetching anyone's job board.
            "research_snapshot": research.evidence_snapshot(),
            "status": OutreachStatus.DRAFT,
        },
    )
    # Record what research this draft was written from, so the company's row
    # reflects the evidence actually used.
    outreach_repo.save_company_research(conn, company_id, research.summary())
    result.stored = True
    logger.info(
        "Stored outreach DRAFT %d for %r (%d words).",
        result.message_id, result.company, draft.word_count,
    )
    return result


def _optional(company: dict | sqlite3.Row, key: str) -> str:
    try:
        return company[key] or ""
    except (KeyError, IndexError):
        return ""
