"""
Tests for the employer registry (config loading, adapter dispatch) and the
discovery orchestrator (run_discovery — the integration point with the
EXISTING jobs table + EXISTING pipeline). No real network calls — adapters
are mocked/faked throughout.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.database.db import init_db
from src.database.models import JobStatus
from src.job_discovery.registry import EmployerConfig, discover_from_employer, load_employers
from src.job_discovery.run_discovery import run_discovery
from src.sources.base import NormalizedJob


def make_job(title="IT Support Officer", company="Acme", source="greenhouse", source_job_id="1", url=None) -> NormalizedJob:
    return NormalizedJob(
        source=source,
        url=url or f"https://boards.greenhouse.io/{company.lower()}/jobs/{source_job_id}",
        title=title,
        description="Entry-level service desk role.",
        company=company,
        location="Sydney NSW",
        source_job_id=source_job_id,
    )


class TestLoadEmployers:
    def test_loads_valid_config(self, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text(json.dumps([{"company": "Acme", "platform": "greenhouse", "identifier": "acme", "enabled": True}]))
        employers = load_employers(config)
        assert len(employers) == 1
        assert employers[0].company == "Acme"
        assert employers[0].platform == "greenhouse"

    def test_defaults_enabled_to_true_when_missing(self, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text(json.dumps([{"company": "Acme", "platform": "greenhouse", "identifier": "acme"}]))
        employers = load_employers(config)
        assert employers[0].enabled is True

    def test_skips_malformed_entry_missing_required_field(self, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text(json.dumps([{"company": "Acme"}, {"company": "Beta", "platform": "lever", "identifier": "beta"}]))
        employers = load_employers(config)
        assert len(employers) == 1
        assert employers[0].company == "Beta"

    def test_missing_file_returns_empty_list_not_error(self, tmp_path):
        employers = load_employers(tmp_path / "does_not_exist.json")
        assert employers == []

    def test_malformed_json_returns_empty_list_not_error(self, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text("{not valid json")
        employers = load_employers(config)
        assert employers == []

    def test_ignores_underscore_comment_key(self, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text(
            json.dumps([{"_comment": "example", "company": "Acme", "platform": "greenhouse", "identifier": "acme"}])
        )
        employers = load_employers(config)
        assert len(employers) == 1


class TestDiscoverFromEmployer:
    def test_dispatches_to_correct_adapter(self):
        employer = EmployerConfig(company="Acme", platform="greenhouse", identifier="acme")
        with patch("src.job_discovery.registry.ADAPTERS", {"greenhouse": MagicMock(discover=MagicMock(return_value=[make_job()]))}):
            jobs = discover_from_employer(employer)
        assert len(jobs) == 1

    def test_unknown_platform_returns_empty_list(self):
        employer = EmployerConfig(company="Acme", platform="totallymadeup", identifier="acme")
        jobs = discover_from_employer(employer)
        assert jobs == []

    def test_adapter_exception_returns_empty_list_not_raise(self):
        employer = EmployerConfig(company="Acme", platform="greenhouse", identifier="acme")
        broken_adapter = MagicMock()
        broken_adapter.discover.side_effect = RuntimeError("board renamed")
        with patch("src.job_discovery.registry.ADAPTERS", {"greenhouse": broken_adapter}):
            jobs = discover_from_employer(employer)
        assert jobs == []


class TestRunDiscovery:
    @pytest.fixture()
    def db_path(self, tmp_path):
        path = tmp_path / "test.db"
        init_db(path)
        return path

    def _config_with_one_employer(self, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text(json.dumps([{"company": "Acme", "platform": "greenhouse", "identifier": "acme", "enabled": True}]))
        return config

    def test_inserts_discovered_jobs(self, db_path, tmp_path):
        config = self._config_with_one_employer(tmp_path)
        with patch("src.job_discovery.run_discovery.discover_from_employer", return_value=[make_job()]):
            result = run_discovery(db_path=db_path, process=False, config_path=config)

        assert result["found"] == 1
        assert result["inserted"] == 1
        assert result["duplicates"] == 0

        from src.database.db import get_connection

        conn = get_connection(db_path)
        row = conn.execute("SELECT title, status FROM jobs").fetchone()
        conn.close()
        assert row["title"] == "IT Support Officer"
        assert row["status"] == JobStatus.NEW

    def test_disabled_employers_are_skipped(self, db_path, tmp_path):
        config = tmp_path / "employers.json"
        config.write_text(json.dumps([{"company": "Acme", "platform": "greenhouse", "identifier": "acme", "enabled": False}]))

        with patch("src.job_discovery.run_discovery.discover_from_employer") as mock_discover:
            result = run_discovery(db_path=db_path, process=False, config_path=config)

        mock_discover.assert_not_called()
        assert result["employers_checked"] == 0

    def test_duplicate_jobs_are_not_reinserted(self, db_path, tmp_path):
        config = self._config_with_one_employer(tmp_path)
        job = make_job()
        with patch("src.job_discovery.run_discovery.discover_from_employer", return_value=[job]):
            run_discovery(db_path=db_path, process=False, config_path=config)
            result2 = run_discovery(db_path=db_path, process=False, config_path=config)

        assert result2["found"] == 1
        assert result2["inserted"] == 0
        assert result2["duplicates"] == 1

        from src.database.db import get_connection

        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 1  # still just one row, not two

    def test_process_true_calls_existing_pipeline(self, db_path, tmp_path):
        config = self._config_with_one_employer(tmp_path)
        with patch("src.job_discovery.run_discovery.discover_from_employer", return_value=[make_job()]):
            with patch("src.job_discovery.run_discovery.process_new_jobs") as mock_process:
                mock_process.return_value = {"processed": 1}
                result = run_discovery(db_path=db_path, process=True, config_path=config)

        mock_process.assert_called_once_with(db_path=db_path)
        assert result["pipeline"] == {"processed": 1}

    def test_process_false_does_not_call_pipeline(self, db_path, tmp_path):
        config = self._config_with_one_employer(tmp_path)
        with patch("src.job_discovery.run_discovery.discover_from_employer", return_value=[make_job()]):
            with patch("src.job_discovery.run_discovery.process_new_jobs") as mock_process:
                result = run_discovery(db_path=db_path, process=False, config_path=config)

        mock_process.assert_not_called()
        assert "pipeline" not in result
