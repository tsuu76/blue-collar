"""
Factory that returns the configured AI provider. This is the only place in
the codebase that branches on AI_PROVIDER, so the cost-protection rule
("local AI only") has exactly one enforcement point.
"""
from __future__ import annotations

from src.config import settings

from .base import AIProvider

_SUPPORTED_PROVIDERS = {"ollama"}


def get_ai_provider(model: str | None = None) -> AIProvider:
    provider = settings.ai_provider.lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            f"AI_PROVIDER={provider!r} is not supported. This project only ever "
            f"implements local providers ({sorted(_SUPPORTED_PROVIDERS)}) to keep "
            f"runtime cost at $0 — see COST PROTECTION in README.md before adding "
            f"a cloud provider."
        )
    from .ollama_provider import OllamaProvider

    return OllamaProvider(
        base_url=settings.ollama_base_url,
        model=model or settings.ollama_model,
        timeout=settings.ollama_timeout_seconds,
    )
