"""
Master resume schema.

This is the single source of truth for the user's real, factual work
history, education, skills, and projects. Every bullet that matters carries
a stable `id` so that:
  - tailoring instructions can reference existing content precisely
    (reorder/rewrite/omit by id) instead of the AI regenerating text, and
  - the quality-control stage can trace every claim in a generated resume
    back to something that actually exists here.

The AI is only ever allowed to produce *modifications* to this structure
(see src/resume/tailor.py, built in a later phase) — reordering, rewriting
wording, or omitting entries. It must never be asked to invent new
employment, projects, certifications, or metrics; those additions can only
come from the user editing this file directly.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Personal(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Bullet(BaseModel):
    id: str
    text: str
    skills: list[str] = Field(default_factory=list)


class Education(BaseModel):
    id: str
    institution: str
    credential: str
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""
    location: str = ""
    highlights: list[Bullet] = Field(default_factory=list)


class Experience(BaseModel):
    id: str
    company: str
    title: str
    start_date: str = ""
    end_date: str = ""  # "" or "Present"
    location: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    url: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Certification(BaseModel):
    id: str
    name: str
    issuer: str = ""
    date: str = ""
    credential_id: str = ""
    in_progress: bool = False


class MasterResume(BaseModel):
    """
    Structured, ID-tagged master resume. This file (data/master_resume.json)
    is edited by the user (directly, or by the assistant only when the user
    explicitly supplies new factual content) — it is never rewritten by the
    per-job tailoring pipeline.
    """

    personal: Personal = Field(default_factory=Personal)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    additional: list[Bullet] = Field(default_factory=list)

    def all_bullet_ids(self) -> set[str]:
        """Every bullet id that exists anywhere in the resume — used by the
        tailoring validator to reject any id an LLM invents that doesn't
        actually exist in the master resume."""
        ids: set[str] = set()
        for edu in self.education:
            ids.update(b.id for b in edu.highlights)
        for exp in self.experience:
            ids.update(b.id for b in exp.bullets)
        for proj in self.projects:
            ids.update(b.id for b in proj.bullets)
        ids.update(b.id for b in self.additional)
        return ids

    def all_skills(self) -> set[str]:
        """Every skill string the user has actually claimed, anywhere in the
        resume (top-level skills list + any skill tagged on a bullet).
        Used by cover-letter generation and QC to catch fabricated skills."""
        skills = {s.strip().lower() for s in self.skills}
        for edu in self.education:
            for b in edu.highlights:
                skills.update(s.strip().lower() for s in b.skills)
        for exp in self.experience:
            for b in exp.bullets:
                skills.update(s.strip().lower() for s in b.skills)
        for proj in self.projects:
            skills.update(t.strip().lower() for t in proj.technologies)
            for b in proj.bullets:
                skills.update(s.strip().lower() for s in b.skills)
        for b in self.additional:
            skills.update(s.strip().lower() for s in b.skills)
        return skills

    def all_text_fragments(self) -> list[str]:
        """
        Every piece of free text in the resume (summary + every bullet).
        Used as the "ground truth" pool for fabrication checks elsewhere
        (e.g. cover letter generation) — anything a generated document
        claims should be traceable back to one of these fragments.
        """
        fragments = [self.summary] if self.summary else []
        for edu in self.education:
            fragments.extend(b.text for b in edu.highlights)
        for exp in self.experience:
            fragments.extend(b.text for b in exp.bullets)
        for proj in self.projects:
            fragments.extend(b.text for b in proj.bullets)
        fragments.extend(b.text for b in self.additional)
        return fragments

    def identifying_names(self) -> list[str]:
        """
        Proper nouns the candidate is entitled to reference by name (company
        names, project names, institutions, credentials, job titles held).
        These rarely appear inside bullet prose itself, so callers grounding
        generated text (e.g. a cover letter naming "Tipaload" or "CashFlo")
        need this in addition to all_text_fragments() — otherwise a
        perfectly legitimate reference to the candidate's own history looks
        indistinguishable from a fabricated new name.
        """
        names: list[str] = []
        if self.personal.full_name:
            names.append(self.personal.full_name)
        for exp in self.experience:
            names.extend([exp.company, exp.title])
        for proj in self.projects:
            names.append(proj.name)
        for edu in self.education:
            names.extend([edu.institution, edu.credential, edu.field_of_study])
        for cert in self.certifications:
            names.extend([cert.name, cert.issuer])
        return [n for n in names if n]
