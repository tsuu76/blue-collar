"""
Unit tests for the Ollama AI provider. These use mocked HTTP responses only —
no real Ollama server or network access is required to run this test file.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.ai.base import AIResponseError
from src.ai.ollama_provider import OllamaProvider, _extract_json, _strip_thinking


def _mock_response(json_body: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception("HTTP error")
    return resp


class TestStripThinking:
    def test_removes_think_block(self):
        text = "<think>internal reasoning</think>{\"a\": 1}"
        assert _strip_thinking(text) == '{"a": 1}'

    def test_no_think_block_unchanged(self):
        text = '{"a": 1}'
        assert _strip_thinking(text) == text


class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == '{"a": 1}'

    def test_json_in_markdown_fence(self):
        text = '```json\n{"a": 1}\n```'
        assert _extract_json(text) == '{"a": 1}'

    def test_json_with_surrounding_text(self):
        text = 'Here is the result:\n{"a": 1}\nHope that helps!'
        assert _extract_json(text) == '{"a": 1}'

    def test_json_with_thinking_block(self):
        text = '<think>let me consider...</think>```json\n{"a": 1}\n```'
        assert _extract_json(text) == '{"a": 1}'


class TestOllamaProviderGenerate:
    def test_generate_success(self):
        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        with patch("src.ai.ollama_provider.requests.post") as mock_post:
            mock_post.return_value = _mock_response({"response": "hello world"})
            result = provider.generate("say hi")
            assert result == "hello world"

    def test_generate_empty_response_raises(self):
        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        with patch("src.ai.ollama_provider.requests.post") as mock_post:
            mock_post.return_value = _mock_response({"response": ""})
            with pytest.raises(AIResponseError):
                provider.generate("say hi")

    def test_generate_network_error_raises(self):
        import requests

        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        with patch("src.ai.ollama_provider.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("connection refused")
            with pytest.raises(AIResponseError):
                provider.generate("say hi")


class TestOllamaProviderGenerateJson:
    def test_valid_json_first_try(self):
        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        payload = {"fit_score": 88, "recommendation": "APPLY"}
        with patch("src.ai.ollama_provider.requests.post") as mock_post:
            mock_post.return_value = _mock_response({"response": json.dumps(payload)})
            result = provider.generate_json("analyze this job")
            assert result == payload

    def test_retries_then_succeeds(self):
        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        good_payload = {"fit_score": 50}
        with patch("src.ai.ollama_provider.requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_response({"response": "not json at all"}),
                _mock_response({"response": json.dumps(good_payload)}),
            ]
            result = provider.generate_json("analyze this job", max_retries=2)
            assert result == good_payload

    def test_fails_safe_after_exhausting_retries(self):
        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        with patch("src.ai.ollama_provider.requests.post") as mock_post:
            mock_post.return_value = _mock_response({"response": "still not json"})
            with pytest.raises(AIResponseError):
                provider.generate_json("analyze this job", max_retries=1)

    def test_is_available_true(self):
        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        with patch("src.ai.ollama_provider.requests.get") as mock_get:
            mock_get.return_value = _mock_response({"models": []})
            assert provider.is_available() is True

    def test_is_available_false_on_connection_error(self):
        import requests

        provider = OllamaProvider(base_url="http://localhost:11434", model="test-model")
        with patch("src.ai.ollama_provider.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("refused")
            assert provider.is_available() is False
