"""
Local Ollama backend. Talks only to http://localhost:11434 (or whatever
OLLAMA_BASE_URL is set to) — never a remote host by default. No API key is
ever used or required, because Ollama runs entirely on this machine.
"""
from __future__ import annotations

import json
import logging
import re

import requests

from .base import AIProvider, AIResponseError

logger = logging.getLogger("job_hunter.ai.ollama")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks some models (e.g. qwen3) emit."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> str:
    """Best-effort extraction of a JSON object from a model's raw text output."""
    text = _strip_thinking(text)
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()
    # Fall back to the substring between the first '{' and the last '}'.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    if start != -1:
        # An object that was opened but never closed. Local models do this
        # routinely — llama3 finishes a well-formed response and simply
        # omits the final brace, reporting done_reason="stop" as though it
        # were complete. Without repair the whole response is discarded and
        # every retry fails the same way, so a perfectly good answer is lost
        # to one missing character. Only ever ADDS the closing brackets the
        # text is short of; it never edits content, so a genuinely mangled
        # response still fails to parse in the caller as it should.
        return _close_unbalanced(text[start:].strip())
    return text.strip()


def _close_unbalanced(fragment: str) -> str:
    """
    Append whatever closing brackets a truncated JSON fragment is missing,
    ignoring brackets that appear inside string literals.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in fragment:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif not in_string:
            if char in "{[":
                stack.append(char)
            elif char in "}]" and stack:
                stack.pop()

    if in_string:
        fragment += '"'
    return fragment + "".join("}" if opener == "{" else "]" for opener in reversed(stack))


class OllamaProvider(AIProvider):
    def __init__(self, base_url: str, model: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.2) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system or "",
            "stream": False,
            "options": {"temperature": temperature},
        }
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise AIResponseError(f"Ollama request failed: {exc}") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise AIResponseError(f"Ollama returned non-JSON HTTP body: {exc}") from exc

        text = data.get("response", "")
        if not text:
            raise AIResponseError("Ollama returned an empty response")
        return _strip_thinking(text)

    def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 2,
    ) -> dict:
        json_instruction = (
            "\n\nRespond with ONLY a single valid JSON object. "
            "No markdown fences, no explanation, no text before or after the JSON."
        )
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                raw = self.generate(
                    prompt + json_instruction, system=system, temperature=temperature
                )
                candidate = _extract_json(raw)
                return json.loads(candidate)
            except (AIResponseError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "generate_json attempt %d/%d failed: %s",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
        raise AIResponseError(
            f"Ollama did not return valid JSON after {max_retries + 1} attempts: {last_error}"
        )
