"""
Schema for AI-generated resume tailoring instructions (spec section 7).

Critically, this is a *diff* against the master resume, not a new resume.
The AI is only ever allowed to reorder/select/rewrite/omit things that
already exist in data/master_resume.json — every reference here is
validated against the master resume's own bullet IDs and project/experience
IDs (see tailor.py), so an LLM cannot smuggle in a fabricated entry just by
inventing a plausible-looking id or text.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SummaryChange(BaseModel):
    action: Literal["rewrite", "keep"] = "keep"
    text: str | None = None


class ReorderOrSelect(BaseModel):
    """
    Used for skills/projects/experience. `order` is the list of ids (or, for
    skills, the exact skill strings) in the desired final order.

    For projects/experience, `order` may be a SUBSET of the master resume's
    ids — anything omitted is left out of the tailored resume (spec's
    "select the most relevant projects"). For skills, `order` must contain
    every existing skill (reorder only, no silent removal of a truthful
    skill) — see validate_tailoring_instructions for the enforcement.
    """

    action: Literal["reorder", "keep"] = "keep"
    order: list[str] = Field(default_factory=list)


class BulletChange(BaseModel):
    id: str
    action: Literal["rewrite", "omit", "keep"]
    text: str | None = None


class TailoringInstructions(BaseModel):
    summary: SummaryChange = Field(default_factory=SummaryChange)
    skills: ReorderOrSelect = Field(default_factory=ReorderOrSelect)
    projects: ReorderOrSelect = Field(default_factory=ReorderOrSelect)
    experience: ReorderOrSelect = Field(default_factory=ReorderOrSelect)
    bullet_changes: list[BulletChange] = Field(default_factory=list)
