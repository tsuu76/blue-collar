"""
Tests for Phase 10 resume tailoring. The most important behavior here is
negative: the AI must never be able to smuggle a fabricated employer,
project, certification, skill, or metric into a tailored resume, no matter
what it outputs. Every test in TestFabricationPrevention exists to prove one
specific fabrication vector is actually blocked.
"""
from __future__ import annotations

import pytest

from src.ai.base import AIResponseError
from src.resume.schema import MasterResume
from src.resume.tailor import apply_tailoring, generate_tailoring_instructions, validate_tailoring_instructions
from src.resume.tailor_schema import BulletChange, ReorderOrSelect, SummaryChange, TailoringInstructions
from tests.fakes import FakeAIProvider


def make_sample_resume() -> MasterResume:
    return MasterResume.model_validate(
        {
            "personal": {"full_name": "Test User", "email": "test@example.com"},
            "summary": "Entry-level IT candidate with hands-on project experience.",
            "skills": ["Python", "SQL", "Git"],
            "education": [
                {
                    "id": "edu_01",
                    "institution": "Test University",
                    "credential": "BSc Cybersecurity",
                    "highlights": [
                        {"id": "edu_01_bullet_01", "text": "Studied networking fundamentals.", "skills": ["networking"]}
                    ],
                }
            ],
            "experience": [
                {
                    "id": "exp_01",
                    "company": "Tipaload",
                    "title": "QA Tester",
                    "bullets": [
                        {"id": "exp_01_bullet_01", "text": "Troubleshot two apps in staging and production.", "skills": ["QA testing"]}
                    ],
                },
                {
                    "id": "exp_02",
                    "company": "Olympic Hotel",
                    "title": "Kitchen Assistant",
                    "bullets": [],
                },
            ],
            "projects": [
                {
                    "id": "project_01",
                    "name": "CashFlo",
                    "technologies": ["React", "SQLite"],
                    "bullets": [
                        {"id": "project_01_bullet_01", "text": "Built a full-stack finance app.", "skills": ["React", "SQLite"]}
                    ],
                }
            ],
            "certifications": [],
            "additional": [{"id": "extra_01", "text": "Volunteer note.", "skills": []}],
        }
    )


class TestValidateStructural:
    def test_empty_instructions_are_valid(self):
        resume = make_sample_resume()
        problems = validate_tailoring_instructions(resume, TailoringInstructions())
        assert problems == []

    def test_summary_rewrite_without_text_is_invalid(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(summary=SummaryChange(action="rewrite", text=""))
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("summary" in p for p in problems)

    def test_valid_summary_rewrite(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            summary=SummaryChange(action="rewrite", text="Entry-level IT candidate seeking a service desk role.")
        )
        assert validate_tailoring_instructions(resume, instructions) == []


class TestFabricationPrevention:
    """Each test proves one specific way the AI must NOT be able to invent content."""

    def test_cannot_invent_a_new_project_id(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            projects=ReorderOrSelect(action="reorder", order=["project_99_fabricated"])
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("unknown project id" in p for p in problems)

    def test_cannot_invent_a_new_experience_entry(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            experience=ReorderOrSelect(action="reorder", order=["exp_99_fake_google_job"])
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("unknown experience id" in p for p in problems)

    def test_cannot_invent_a_new_bullet_id(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            bullet_changes=[BulletChange(id="exp_01_bullet_99_fake", action="rewrite", text="Led a team of 5 engineers.")]
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("unknown bullet id" in p for p in problems)

    def test_cannot_add_a_new_skill_via_skills_reorder(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            skills=ReorderOrSelect(action="reorder", order=["Python", "SQL", "Git", "Kubernetes"])
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("must be a reordering of exactly the existing skills" in p for p in problems)

    def test_cannot_silently_drop_a_skill_via_reorder(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(skills=ReorderOrSelect(action="reorder", order=["Python", "SQL"]))
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("must be a reordering of exactly the existing skills" in p for p in problems)

    def test_cannot_fabricate_a_metric_in_a_rewrite(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            bullet_changes=[
                BulletChange(
                    id="exp_01_bullet_01",
                    action="rewrite",
                    text="Troubleshot apps in staging and production, reducing bugs by 40%.",
                )
            ]
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("possible fabricated metric" in p for p in problems)

    def test_cannot_fabricate_a_new_technology_in_a_rewrite(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            bullet_changes=[
                BulletChange(
                    id="project_01_bullet_01",
                    action="rewrite",
                    text="Built a full-stack finance app using Kubernetes and AWS Lambda.",
                )
            ]
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("possible fabricated skill" in p for p in problems)

    def test_single_word_from_an_existing_multiword_skill_is_not_flagged(self):
        # Regression: "QA testing" exists as a skill tag elsewhere in the
        # resume, but as a two-word phrase. A rewrite that uses the bare
        # word "QA" must not be flagged as fabricated just because "QA"
        # alone was never an exact skill-list entry.
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            bullet_changes=[
                BulletChange(
                    id="exp_01_bullet_01",
                    action="rewrite",
                    text="Performed QA troubleshooting across staging and production.",
                )
            ]
        )
        assert validate_tailoring_instructions(resume, instructions) == []

    def test_legitimate_reword_without_new_facts_is_valid(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            bullet_changes=[
                BulletChange(
                    id="exp_01_bullet_01",
                    action="rewrite",
                    text="Diagnosed and resolved issues across two applications in both staging and production.",
                )
            ]
        )
        assert validate_tailoring_instructions(resume, instructions) == []

    def test_project_selection_can_omit_but_not_invent(self):
        # Selecting a subset of REAL projects is fine (spec allows "select
        # most relevant projects"); this just confirms a valid subset passes.
        resume = make_sample_resume()
        instructions = TailoringInstructions(projects=ReorderOrSelect(action="reorder", order=["project_01"]))
        assert validate_tailoring_instructions(resume, instructions) == []

    def test_duplicate_bullet_change_ids_rejected(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(
            bullet_changes=[
                BulletChange(id="exp_01_bullet_01", action="omit"),
                BulletChange(id="exp_01_bullet_01", action="keep"),
            ]
        )
        problems = validate_tailoring_instructions(resume, instructions)
        assert any("more than once" in p for p in problems)


class TestApplyTailoring:
    def test_omit_removes_bullet_from_output(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(bullet_changes=[BulletChange(id="exp_01_bullet_01", action="omit")])
        tailored = apply_tailoring(resume, instructions)
        exp = next(e for e in tailored.experience if e.id == "exp_01")
        assert exp.bullets == []

    def test_rewrite_changes_bullet_text_only(self):
        resume = make_sample_resume()
        new_text = "Diagnosed issues across two applications in staging and production."
        instructions = TailoringInstructions(
            bullet_changes=[BulletChange(id="exp_01_bullet_01", action="rewrite", text=new_text)]
        )
        tailored = apply_tailoring(resume, instructions)
        exp = next(e for e in tailored.experience if e.id == "exp_01")
        assert exp.bullets[0].text == new_text
        assert exp.bullets[0].id == "exp_01_bullet_01"  # id preserved

    def test_project_selection_filters_and_orders(self):
        resume = make_sample_resume()
        # Only one project exists in the fixture; verify selection mechanism
        # doesn't crash and preserves the selected one.
        instructions = TailoringInstructions(projects=ReorderOrSelect(action="reorder", order=["project_01"]))
        tailored = apply_tailoring(resume, instructions)
        assert [p.id for p in tailored.projects] == ["project_01"]

    def test_experience_selection_can_drop_irrelevant_job(self):
        # This mirrors the real use case: an IT-tailored resume dropping the
        # Kitchen Assistant role while keeping it truthfully present in the
        # master resume itself.
        resume = make_sample_resume()
        instructions = TailoringInstructions(experience=ReorderOrSelect(action="reorder", order=["exp_01"]))
        tailored = apply_tailoring(resume, instructions)
        assert [e.id for e in tailored.experience] == ["exp_01"]
        # Master resume itself is untouched.
        assert [e.id for e in resume.experience] == ["exp_01", "exp_02"]

    def test_skills_reorder_applied(self):
        resume = make_sample_resume()
        instructions = TailoringInstructions(skills=ReorderOrSelect(action="reorder", order=["Git", "Python", "SQL"]))
        tailored = apply_tailoring(resume, instructions)
        assert tailored.skills == ["Git", "Python", "SQL"]

    def test_summary_rewrite_applied(self):
        resume = make_sample_resume()
        new_summary = "Entry-level candidate targeting IT support roles."
        instructions = TailoringInstructions(summary=SummaryChange(action="rewrite", text=new_summary))
        tailored = apply_tailoring(resume, instructions)
        assert tailored.summary == new_summary

    def test_master_resume_object_never_mutated(self):
        resume = make_sample_resume()
        original_summary = resume.summary
        instructions = TailoringInstructions(summary=SummaryChange(action="rewrite", text="Something completely different."))
        apply_tailoring(resume, instructions)
        assert resume.summary == original_summary

    def test_keep_action_is_a_full_noop(self):
        resume = make_sample_resume()
        tailored = apply_tailoring(resume, TailoringInstructions())
        assert tailored.summary == resume.summary
        assert tailored.skills == resume.skills
        assert [p.id for p in tailored.projects] == [p.id for p in resume.projects]
        assert [e.id for e in tailored.experience] == [e.id for e in resume.experience]


VALID_INSTRUCTIONS_JSON = {
    "summary": {"action": "keep"},
    "skills": {"action": "keep"},
    "projects": {"action": "keep"},
    "experience": {"action": "keep"},
    "bullet_changes": [],
}


class TestGenerateTailoringInstructionsRetry:
    def test_succeeds_on_first_valid_response(self):
        resume = make_sample_resume()
        provider = FakeAIProvider([VALID_INSTRUCTIONS_JSON])
        result = generate_tailoring_instructions(resume, title="IT Support", description="desc", provider=provider)
        assert isinstance(result, TailoringInstructions)

    def test_retries_after_fabricated_project_id_then_succeeds(self):
        resume = make_sample_resume()
        fabricated = dict(VALID_INSTRUCTIONS_JSON, projects={"action": "reorder", "order": ["fake_project_id"]})
        provider = FakeAIProvider([fabricated, VALID_INSTRUCTIONS_JSON])
        result = generate_tailoring_instructions(
            resume, title="IT Support", description="desc", provider=provider, max_retries=2
        )
        assert isinstance(result, TailoringInstructions)
        assert provider.call_count == 2

    def test_fails_safe_when_ai_keeps_fabricating(self):
        resume = make_sample_resume()
        fabricated = dict(VALID_INSTRUCTIONS_JSON, projects={"action": "reorder", "order": ["fake_project_id"]})
        provider = FakeAIProvider([fabricated, fabricated, fabricated])
        with pytest.raises(AIResponseError):
            generate_tailoring_instructions(
                resume, title="IT Support", description="desc", provider=provider, max_retries=2
            )
