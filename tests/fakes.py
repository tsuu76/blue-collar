"""
A minimal fake AIProvider for tests that need to exercise validation/retry
logic without any real Ollama server. Shared across test modules.
"""
from __future__ import annotations

from src.ai.base import AIProvider, AIResponseError


class FakeAIProvider(AIProvider):
    """
    Returns a scripted sequence of JSON dicts (or exceptions) from
    generate_json, one per call, so tests can assert exact retry behavior.
    """

    def __init__(self, json_responses: list[dict | Exception]):
        self._responses = list(json_responses)
        self.call_count = 0

    def is_available(self) -> bool:
        return True

    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.2) -> str:
        raise NotImplementedError("FakeAIProvider only implements generate_json for these tests")

    def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 2,
    ) -> dict:
        if self.call_count >= len(self._responses):
            raise AIResponseError("FakeAIProvider: no more scripted responses")
        response = self._responses[self.call_count]
        self.call_count += 1
        if isinstance(response, Exception):
            raise response
        return response
