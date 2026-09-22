"""
Pydantic schemas that validate the *shape* of AI JSON output. The provider
layer (OllamaProvider.generate_json) already guarantees parseable JSON; this
module is the second gate — guaranteeing the JSON has the fields, types, and
value ranges every downstream stage expects. An LLM response that parses as
JSON but has the wrong shape (missing key, fit_score as a string, etc.) must
never silently propagate — it should fail loudly here and trigger a retry.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

JobCategory = str  # "ENTRY_LEVEL_IT" | "MID_SENIOR_IT" | "NON_IT" | "UNCLEAR"
Recommendation = str  # "APPLY" | "SKIP" | "REVIEW"

_VALID_CATEGORIES = {"ENTRY_LEVEL_IT", "MID_SENIOR_IT", "NON_IT", "UNCLEAR"}
_VALID_RECOMMENDATIONS = {"APPLY", "SKIP", "REVIEW"}


class JobAnalysis(BaseModel):
    """Strict schema for the Phase 8 AI job-analysis response (spec section 15)."""

    category: str
    fit_score: int = Field(ge=0, le=100)
    experience_required: str
    desk_based: bool
    recommendation: str
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    relevant_keywords: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)

    @field_validator("category")
    @classmethod
    def _validate_category(cls, v: str) -> str:
        v_upper = v.strip().upper()
        if v_upper not in _VALID_CATEGORIES:
            raise ValueError(f"category must be one of {_VALID_CATEGORIES}, got {v!r}")
        return v_upper

    @field_validator("recommendation")
    @classmethod
    def _validate_recommendation(cls, v: str) -> str:
        v_upper = v.strip().upper()
        if v_upper not in _VALID_RECOMMENDATIONS:
            raise ValueError(f"recommendation must be one of {_VALID_RECOMMENDATIONS}, got {v!r}")
        return v_upper


class QualityControlResult(BaseModel):
    """Strict schema for the Phase 12 quality-control response (spec section 19)."""

    passed: bool
    issues: list[str] = Field(default_factory=list)


class OutreachAnalysis(BaseModel):
    """
    Strict schema for the outreach pathway's personalization analysis: what a
    company appears to hire for, read off its OWN real current postings.

    Shape validation only, as everywhere else in this module. The truthfulness
    of the contents is not taken on trust — src/outreach/personalization.py
    filters every list here against the actual posting text (and, for
    candidate_overlap, against the master resume) after validation, so a model
    that invents a technology or a skill the candidate lacks has it dropped
    rather than carried into an email.
    """

    recurring_skills: list[str] = Field(default_factory=list)
    tools_and_technologies: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    experience_requirements: list[str] = Field(default_factory=list)
    terminology: list[str] = Field(default_factory=list)
    candidate_overlap: list[str] = Field(default_factory=list)
    notes: str = ""
