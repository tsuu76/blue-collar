"""
Tests for the master resume schema: validation, bullet-id collection, and
skill aggregation (used later by the tailoring validator and QC stage to
catch fabricated content).
"""
from __future__ import annotations

from src.resume.schema import MasterResume


def make_sample_resume() -> MasterResume:
    return MasterResume.model_validate(
        {
            "personal": {"full_name": "Test User", "email": "test@example.com"},
            "summary": "Entry-level IT candidate.",
            "skills": ["Python", "MySQL"],
            "education": [
                {
                    "id": "edu_01",
                    "institution": "Test University",
                    "credential": "BSc Cybersecurity",
                    "highlights": [
                        {"id": "edu_01_bullet_01", "text": "Studied networking.", "skills": ["networking"]}
                    ],
                }
            ],
            "experience": [],
            "projects": [
                {
                    "id": "project_01",
                    "name": "Test Project",
                    "technologies": ["React"],
                    "bullets": [
                        {"id": "project_01_bullet_01", "text": "Built a thing.", "skills": ["React", "JavaScript"]}
                    ],
                }
            ],
            "certifications": [],
            "additional": [{"id": "extra_01", "text": "Volunteer note.", "skills": ["communication"]}],
        }
    )


class TestMasterResumeValidation:
    def test_minimal_resume_validates(self):
        resume = MasterResume()
        assert resume.summary == ""
        assert resume.skills == []

    def test_full_resume_validates(self):
        resume = make_sample_resume()
        assert resume.personal.full_name == "Test User"
        assert len(resume.projects) == 1


class TestBulletIds:
    def test_all_bullet_ids_collects_from_every_section(self):
        resume = make_sample_resume()
        ids = resume.all_bullet_ids()
        assert ids == {"edu_01_bullet_01", "project_01_bullet_01", "extra_01"}

    def test_empty_resume_has_no_bullet_ids(self):
        resume = MasterResume()
        assert resume.all_bullet_ids() == set()


class TestSkillsAggregation:
    def test_all_skills_combines_top_level_and_bullet_tags(self):
        resume = make_sample_resume()
        skills = resume.all_skills()
        assert "python" in skills
        assert "mysql" in skills
        assert "networking" in skills
        assert "react" in skills
        assert "javascript" in skills
        assert "communication" in skills

    def test_all_skills_is_case_insensitive(self):
        resume = MasterResume.model_validate({"skills": ["PYTHON", "python", "Python"]})
        assert resume.all_skills() == {"python"}

    def test_skill_not_in_resume_is_absent(self):
        resume = make_sample_resume()
        assert "kubernetes" not in resume.all_skills()
