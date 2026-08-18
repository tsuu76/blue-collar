"""
Shared "did the AI add something not actually in the source material?"
heuristics, used by both resume tailoring (Phase 10) and cover letter
generation (Phase 11). These are best-effort, first-pass guards that run on
every generation attempt and catch obvious cases immediately — they are NOT
a substitute for Phase 12's dedicated AI quality-control pass, which does
proper contextual review with a second model call.
"""
from __future__ import annotations

import re

# Generic words that are capitalized but not fabrication risks. "i" covers
# the pronoun "I", which is always capitalized regardless of position and
# carries no fabrication signal.
_TOKEN_STOPWORDS = {"the", "this", "that", "these", "those", "with", "and", "for", "across", "i"}
_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9+#.\-]*\b")
_NUMBER_RE = re.compile(r"\d+%?")
_SENTENCE_END_CHARS = {".", "!", "?"}


def extract_tech_tokens(text: str, *, exclude_sentence_initial: bool = True) -> set[str]:
    """
    Capitalized/technical-looking tokens.

    `exclude_sentence_initial` (default True) skips words at the start of a
    sentence, since sentence-initial capitalization is just English grammar
    (every sentence starts with a capital letter) and carries no signal
    about whether a term is a genuine technology/proper-noun mention —
    flagging it produces constant false positives (e.g. "Diagnosed
    issues..." would otherwise flag the harmless verb "Diagnosed"). This
    should stay True for CANDIDATE text being checked.

    Callers building the ALLOWED/reference pool should pass
    exclude_sentence_initial=False: a short reference string like a bare
    company name ("Tipaload") or project name ("CashFlo") is, by
    definition, entirely "sentence-initial" — excluding it would wrongly
    strip legitimate known names out of the allowed set.
    """
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(text):
        word = match.group(0)
        if word.lower() in _TOKEN_STOPWORDS:
            continue
        if exclude_sentence_initial:
            prefix = text[: match.start()].rstrip()
            is_sentence_initial = not prefix or prefix[-1] in _SENTENCE_END_CHARS
            if is_sentence_initial:
                continue
        tokens.add(word.lower())
    return tokens


def extract_numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text))


def skill_words(skills: set[str]) -> set[str]:
    """
    Break multi-word/slashed skill phrases (e.g. "QA testing", "TCP/IP
    basics") into individual lowercase words. Needed because callers compare
    single extracted tokens (e.g. "QA") against known skills — without this,
    "QA" would be wrongly flagged as unknown even though "QA testing"
    already exists as a skill, just never as that exact standalone phrase.
    """
    words: set[str] = set()
    for skill in skills:
        for word in re.split(r"[\s/,\-]+", skill):
            if word:
                words.add(word.lower())
    return words


def find_fabricated_numbers(candidate_text: str, allowed_texts: list[str]) -> set[str]:
    """Numbers in candidate_text that don't appear anywhere in allowed_texts."""
    candidate_numbers = extract_numbers(candidate_text)
    allowed_numbers: set[str] = set()
    for t in allowed_texts:
        allowed_numbers.update(extract_numbers(t))
    return candidate_numbers - allowed_numbers


def _singularize(word: str) -> str:
    """
    Crude English pluralization strip — good enough to equate "APIs"/"API"
    or "UIs"/"UI" without needing a real NLP dependency. Only used to widen
    what counts as "already known", so over-stripping just makes the guard
    slightly more lenient, never stricter.
    """
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("es") and len(word) > 2:
        return word[:-2]
    if word.endswith("s") and len(word) > 1:
        return word[:-1]
    return word


def _token_variants(token: str) -> set[str]:
    """
    Morphological variants of a token to widen matching without weakening
    it: the token itself, its singular form, and — for hyphenated compounds
    like "SQLite-based" or "Python-driven" — each individual hyphen-part
    and its singular. A genuinely new fabricated term (e.g. "Kubernetes")
    won't share any part with the resume's real vocabulary, so this only
    ever reduces false positives, not detection of real fabrications.
    """
    variants = {token, _singularize(token)}
    if "-" in token:
        for part in token.split("-"):
            if part:
                variants.add(part)
                variants.add(_singularize(part))
    return variants


def find_suspicious_terms(candidate_text: str, allowed_texts: list[str], known_skills: set[str]) -> set[str]:
    """Capitalized/technical terms in candidate_text absent from allowed_texts and known_skills."""
    candidate_tokens = extract_tech_tokens(candidate_text)
    allowed_tokens: set[str] = set()
    for t in allowed_texts:
        # exclude_sentence_initial=False: reference/allowed strings (e.g. a
        # bare company or project name) must count fully, not be filtered
        # out just because they happen to start at position 0.
        allowed_tokens |= extract_tech_tokens(t, exclude_sentence_initial=False)
    allowed_tokens |= known_skills | skill_words(known_skills)

    allowed_expanded: set[str] = set()
    for a in allowed_tokens:
        allowed_expanded |= _token_variants(a)

    return {t for t in candidate_tokens if not (_token_variants(t) & allowed_expanded)}
