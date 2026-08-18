"""
AI provider abstraction.

Every AI call in this project (job analysis, resume tailoring, cover letter
generation, quality control) goes through this interface rather than calling
a specific backend directly. That keeps a hard boundary in one place: today
the only implementation is Ollama (local, free). If you ever want to swap
models or runtimes later, you add a new class here — the rest of the
codebase never needs to change, and no other module is allowed to import
`requests`/an HTTP client to talk to an LLM directly.

Do NOT add an OpenAI/Anthropic/Gemini implementation here without an explicit,
separate conversation about cost — see COST PROTECTION in the project README.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class AIResponseError(Exception):
    """Raised when the AI backend fails or returns output that cannot be used."""


class AIProvider(ABC):
    """Minimal interface every local AI backend must implement."""

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.2) -> str:
        """Return raw text completion for a prompt. May raise AIResponseError."""
        raise NotImplementedError

    @abstractmethod
    def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 2,
    ) -> dict:
        """
        Return a parsed JSON object from the model.

        Implementations must validate that the response is syntactically valid
        JSON before returning it, retrying on failure, and must raise
        AIResponseError (never return partial/garbage data) if all retries are
        exhausted. Callers must still validate the JSON *shape* themselves
        (see src/ai/schemas.py) — this method only guarantees parseable JSON.
        """
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend is reachable right now."""
        raise NotImplementedError
