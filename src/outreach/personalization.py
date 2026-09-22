"""
Personalization analysis — ask Ollama what a company actually hires for,
reading its REAL current postings, then verify the answer before using it.

Input is a CompanyResearch (src/outreach/research.py), which only ever
contains postings pulled live from the company's own public ATS board. The
model is asked to characterise those postings; it is never asked to recall
anything about the company from its own training data.

The model's answer is then filtered, not trusted:

  - Company-side term lists (recurring skills, tools, terminology) are kept
    only if the words actually appear in the posting text that was fetched.
    A model that names a technology the company never mentioned has it
    dropped here.
  - candidate_overlap is intersected against the master resume and rewritten
    in the resume's own spelling. A skill the candidate does not have cannot
    survive this step, whatever the model claimed — the same structural
    guarantee the rest of the project relies on.

Phrase-like lists (responsibilities, experience requirements) are kept as the
model wrote them: they exist to give the email prompt context, and anything
ungrounded that leaks from them into the email itself is caught by the
fabrication guard in email_draft.py, which grounds against this same evidence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import ValidationError

from src.ai.base import AIProvider, AIResponseError
from src.ai.factory import get_ai_provider
from src.ai.schemas import OutreachAnalysis
from src.config import settings
from src.job_filter.cheap_filter import run_cheap_filter
from src.resume.fabrication_guard import skill_words
from src.resume.schema import MasterResume

from .research import CompanyResearch

logger = logging.getLogger("job_hunter.outreach.personalization")

SYSTEM_PROMPT = (
    "You are analysing a company's own current job postings to work out what that company "
    "actually hires for. Use ONLY the postings given to you. Never mention a technology, tool, "
    "responsibility, or requirement that is not written in them, and never use anything you "
    "think you know about this company from elsewhere. Do not praise the company, describe its "
    "culture, or guess at its products. If the postings do not support something, leave that "
    "field empty rather than filling it in."
)

_PROMPT_TEMPLATE = """These are {count} real, currently-advertised job postings from {company}.

{postings}

THE CANDIDATE'S ACTUAL SKILLS (they have these and nothing more):
{candidate_skills}

Analyse the postings above and return a JSON object with EXACTLY this shape
(no extra keys, no missing keys):
{{
  "recurring_skills": [<technical skills mentioned across these postings>],
  "tools_and_technologies": [<specific tools, platforms and technologies named in them>],
  "responsibilities": [<day-to-day responsibilities these roles describe>],
  "experience_requirements": [<what experience level these postings ask for>],
  "terminology": [<distinctive words or phrases this company repeatedly uses>],
  "candidate_overlap": [<items from the candidate's skill list above that these postings genuinely ask for — copy them exactly as spelled in that list, and leave this empty if there is no real overlap>],
  "notes": "<ONE short factual sentence on what this company appears to hire for, based only on these postings>"
}}

Formatting rules for the JSON, which matter as much as the content:
- Do NOT use double quotation marks anywhere INSIDE a string value. Not around role
  titles, not around product names, not for emphasis. A stray quote inside a string
  makes the whole response unparseable and it will be discarded.
- Keep "notes" under 30 words and on a single line. No line breaks inside any string.
- Output the JSON object only. No preamble, no explanation, no markdown fence.
"""

# Words too generic to prove a term came from the postings.
_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "for", "with", "in", "on", "to", "at",
    "is", "are", "be", "as", "by", "from", "experience", "skills", "knowledge",
}

# How much of each posting the model sees. Enough to characterise the role
# without a five-posting prompt blowing the context window.
_POSTING_CHARS = 1800

# How MANY postings the model sees. This cap is not cosmetic: a large
# employer can advertise a hundred roles at once (Xero had 110), and
# including them all produced a ~52,000-token prompt against Ollama's
# 2048-token context window. The model then had no room left to finish its
# answer and returned JSON truncated mid-object — every attempt failed with
# a parse error that looked like bad formatting but was really overflow.
#
# A dozen postings is plenty to see what a company repeatedly hires for, and
# entry-level-relevant ones are chosen first so the sample is the part of
# their hiring this candidate could actually do.
MAX_POSTINGS_IN_PROMPT = 12


@dataclass
class PersonalizationResult:
    """
    The verified analysis plus a record of what verification removed, so a
    later step (or a human reading the draft) can see that the filtering
    happened and what it caught.
    """

    analysis: OutreachAnalysis
    dropped_company_terms: list[str]
    dropped_candidate_claims: list[str]

    @property
    def has_overlap(self) -> bool:
        return bool(self.analysis.candidate_overlap)

    def to_dict(self) -> dict:
        return {
            "analysis": self.analysis.model_dump(),
            "dropped_company_terms": self.dropped_company_terms,
            "dropped_candidate_claims": self.dropped_candidate_claims,
        }


def select_postings(research: CompanyResearch) -> list:
    """
    The sample of postings the model is shown, capped at
    MAX_POSTINGS_IN_PROMPT. Postings that pass the project's existing
    entry-level filter come first; the rest fill any remaining slots so a
    company whose ads are all senior still gets characterised.
    """
    relevant, other = [], []
    for posting in research.postings:
        target = (
            relevant
            if run_cheap_filter(posting.title, posting.description or "").passed
            else other
        )
        target.append(posting)
    return (relevant + other)[:MAX_POSTINGS_IN_PROMPT]


def build_prompt(research: CompanyResearch, resume: MasterResume) -> str:
    blocks = []
    for i, posting in enumerate(select_postings(research), start=1):
        body = (posting.description or "").strip()[:_POSTING_CHARS]
        location = f" ({posting.location})" if posting.location else ""
        blocks.append(f"--- POSTING {i} ---\nTITLE: {posting.title}{location}\n{body}")
    return _PROMPT_TEMPLATE.format(
        count=len(blocks),
        company=research.company,
        postings="\n\n".join(blocks),
        candidate_skills=", ".join(resume.skills) or "(none listed)",
    )


def _meaningful_words(text: str) -> list[str]:
    words = [w for w in re.split(r"[^a-z0-9+#.]+", (text or "").lower()) if w]
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


def _evidence_vocabulary(research: CompanyResearch) -> set[str]:
    """Every word appearing in the postings that were actually fetched."""
    parts = [research.company]
    for posting in research.postings:
        parts.extend([posting.title, posting.description, posting.location])
    return set(_meaningful_words(" ".join(p for p in parts if p)))


def _ground_terms(items: list[str], vocabulary: set[str]) -> tuple[list[str], list[str]]:
    """
    Keep only terms whose every meaningful word appears in the postings.
    Returns (kept, dropped). Order and spelling are preserved for kept items.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for item in items:
        words = _meaningful_words(item)
        if words and all(word in vocabulary for word in words):
            if item not in kept:
                kept.append(item)
        else:
            dropped.append(item)
    return kept, dropped


def _ground_candidate_overlap(items: list[str], resume: MasterResume) -> tuple[list[str], list[str]]:
    """
    Map each claimed overlap back to a real resume skill, in the resume's own
    spelling. Anything that maps to nothing is dropped.

    This is the hard guarantee that an outreach email cannot claim a skill
    the candidate does not have: the output is built from the resume, not
    from the model's answer.
    """
    canonical: dict[str, str] = {}
    for skill in resume.skills:
        cleaned = skill.strip()
        if cleaned:
            canonical.setdefault(cleaned.lower(), cleaned)
    for skill in resume.all_skills():
        canonical.setdefault(skill, skill)

    kept: list[str] = []
    dropped: list[str] = []
    for item in items:
        lowered = (item or "").strip().lower()
        match = canonical.get(lowered)
        if match is None:
            # The model may have written "Python scripting" for the resume's
            # "Python" — accept it only if a real skill is fully contained in
            # what it wrote, and then use the resume's wording, not the
            # model's.
            item_words = set(_meaningful_words(item))
            for skill_lower, display in canonical.items():
                needed = {w for w in skill_words({skill_lower}) if len(w) > 2}
                if needed and needed <= item_words:
                    match = display
                    break
        if match is None:
            dropped.append(item)
        elif match not in kept:
            kept.append(match)
    return kept, dropped


def verify_analysis(
    analysis: OutreachAnalysis, research: CompanyResearch, resume: MasterResume
) -> PersonalizationResult:
    """
    Filter a model-produced analysis down to what the evidence supports.
    Pure function, no AI calls — safe to test directly and cheap to run.
    """
    vocabulary = _evidence_vocabulary(research)
    dropped_company: list[str] = []

    analysis.recurring_skills, dropped = _ground_terms(analysis.recurring_skills, vocabulary)
    dropped_company.extend(dropped)
    analysis.tools_and_technologies, dropped = _ground_terms(analysis.tools_and_technologies, vocabulary)
    dropped_company.extend(dropped)
    analysis.terminology, dropped = _ground_terms(analysis.terminology, vocabulary)
    dropped_company.extend(dropped)

    analysis.candidate_overlap, dropped_candidate = _ground_candidate_overlap(
        analysis.candidate_overlap, resume
    )

    if dropped_company:
        logger.info(
            "Dropped %d company term(s) not found in %r's postings: %s",
            len(dropped_company), research.company, dropped_company,
        )
    if dropped_candidate:
        logger.warning(
            "Dropped %d claimed skill(s) the candidate does not actually have: %s",
            len(dropped_candidate), dropped_candidate,
        )

    return PersonalizationResult(
        analysis=analysis,
        dropped_company_terms=dropped_company,
        dropped_candidate_claims=dropped_candidate,
    )


def analyze_company(
    research: CompanyResearch,
    resume: MasterResume,
    *,
    provider: AIProvider | None = None,
    max_retries: int = settings.ollama_max_retries,
) -> PersonalizationResult:
    """
    Run the personalization analysis for one company and verify the result.

    Raises AIResponseError if the model cannot produce a correctly-shaped
    response within max_retries + 1 attempts, and ValueError if there are no
    postings to analyse — there is nothing honest to say about a company
    whose current hiring we could not read, so the caller must not proceed
    to writing an email.
    """
    if not research.has_postings:
        raise ValueError(
            f"No postings found for {research.company!r} — nothing to personalize from. "
            f"{research.error or 'The company has no readable public job board.'}"
        )

    ai = provider or get_ai_provider()
    prompt = build_prompt(research, resume)

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = ai.generate_json(prompt, system=SYSTEM_PROMPT, max_retries=0)
            analysis = OutreachAnalysis.model_validate(raw)
            return verify_analysis(analysis, research, resume)
        except (AIResponseError, ValidationError) as exc:
            last_error = exc
            logger.warning(
                "Personalization analysis attempt %d/%d failed for %r: %s",
                attempt + 1, max_retries + 1, research.company, exc,
            )

    raise AIResponseError(
        f"Personalization analysis failed for {research.company!r} after "
        f"{max_retries + 1} attempts: {last_error}"
    )
