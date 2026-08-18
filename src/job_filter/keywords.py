"""
Configurable keyword lists for the deterministic ("cheap") filter.

These run BEFORE any Ollama call, per the spec: obvious senior/non-IT jobs
should never reach the local LLM. Everything here is plain data so it can be
tuned in one place (or later moved into the `settings` table and edited from
the dashboard) without touching filter logic.
"""
from __future__ import annotations

# Titles/phrases that indicate a job IS a target entry-level IT desk role.
# Matched case-insensitively as substrings against title + first part of description.
POSITIVE_TITLE_KEYWORDS: list[str] = [
    "it support officer",
    "it support technician",
    "it support",
    "service desk analyst",
    "service desk officer",
    "service desk",
    "help desk technician",
    "help desk",
    "helpdesk",
    "desktop support technician",
    "desktop support",
    "level 1 support",
    "level 1 service desk",
    "l1 support",
    "junior it support",
    "junior systems support",
    "junior application support",
    "application support",
    "technical support officer",
    "technical support",
    "it technician",
    "ict support officer",
    "ict support technician",
    "ict support",
    "graduate it",
    "it graduate",
    "graduate technology",
    "technology graduate",
    "junior technical analyst",
    "junior qa",
    "junior test analyst",
    "junior systems administrator",
    "junior data analyst",
    "junior business analyst",
    "technical customer support",
    "saas technical support",
    "noc technician",
    "noc l1",
]

# Hard-reject phrases. If any of these appear, the job is rejected outright
# regardless of skills match — a high skill score must never override a
# seniority rejection (see spec section 16).
NEGATIVE_SENIORITY_KEYWORDS: list[str] = [
    "senior",
    "senior-level",
    "mid-level",
    "mid level",
    "lead",
    "team lead",
    "manager",
    "it manager",
    "principal",
    "architect",
    "solutions architect",
    "senior engineer",
    "senior analyst",
    "l2",
    "level 2",
    "l3",
    "level 3",
    "extensive professional experience",
    "significant professional experience",
    "management responsibilities",
    "head of",
    "director",
]

# Experience-requirement phrases treated as hard rejects (configurable via
# MAX_EXPERIENCE_YEARS — see experience.py for the numeric-years check that
# generalizes beyond this fixed phrase list).
NEGATIVE_EXPERIENCE_KEYWORDS: list[str] = [
    "3+ years",
    "3 + years",
    "4+ years",
    "4 + years",
    "5+ years",
    "5 + years",
    "minimum 3 years",
    "minimum 4 years",
    "minimum 5 years",
    "at least 3 years",
    "at least 4 years",
    "at least 5 years",
]

# Phrases that indicate favorable entry-level framing — used as a positive
# signal in scoring, not a hard-accept.
POSITIVE_EXPERIENCE_KEYWORDS: list[str] = [
    "no experience required",
    "no experience necessary",
    "entry level",
    "entry-level",
    "junior",
    "graduate",
    "graduate program",
    "trainee",
    "0-1 years",
    "0 to 1 year",
    "1 year",
    "1-2 years",
    "1 to 2 years",
]

# Non-IT roles that might otherwise slip past the seniority filter (e.g. a
# junior *sales* manager). Reject on category grounds regardless of level.
NEGATIVE_NON_IT_KEYWORDS: list[str] = [
    "sales representative",
    "real estate",
    "hospitality",
    "retail assistant",
    "forklift",
    "warehouse",
    "truck driver",
    "registered nurse",
    "childcare",
    "construction labourer",
]
