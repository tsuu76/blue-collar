"""
Central configuration loader for IT Job Hunter.

Reads settings from environment variables (populated from `.env` via
python-dotenv). This is the single place that knows about `.env` — every
other module should import `settings` from here rather than reading
`os.environ` directly, so that all defaults stay safe (no paid services,
automation off) in one auditable spot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if not val:
        return default
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "IT-Job-Hunter")

    # --- AI provider (local only — enforced by AI_PROVIDER choices in src/ai) ---
    ai_provider: str = os.getenv("AI_PROVIDER", "ollama")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    ollama_qc_model: str = os.getenv("OLLAMA_QC_MODEL", "qwen3:8b")
    ollama_timeout_seconds: int = _int("OLLAMA_TIMEOUT_SECONDS", 120)
    ollama_max_retries: int = _int("OLLAMA_MAX_RETRIES", 2)

    # --- Targeting ---
    target_domain: str = os.getenv("TARGET_DOMAIN", "IT")
    target_level: str = os.getenv("TARGET_LEVEL", "ENTRY")
    target_locations: list[str] = field(
        default_factory=lambda: _list(
            "TARGET_LOCATIONS",
            ["Sydney", "Greater Sydney", "NSW", "Remote Australia", "Hybrid Sydney"],
        )
    )

    # --- Experience filter ---
    max_experience_years: int = _int("MAX_EXPERIENCE_YEARS", 2)
    experience_soft_cap_years: int = _int("EXPERIENCE_SOFT_CAP_YEARS", 2)

    # --- Scoring ---
    min_fit_score: int = _int("MIN_FIT_SCORE", 75)
    weight_entry_level: float = _float("WEIGHT_ENTRY_LEVEL", 0.30)
    weight_it_relevance: float = _float("WEIGHT_IT_RELEVANCE", 0.25)
    weight_skills_match: float = _float("WEIGHT_SKILLS_MATCH", 0.20)
    weight_experience_match: float = _float("WEIGHT_EXPERIENCE_MATCH", 0.15)
    weight_location: float = _float("WEIGHT_LOCATION", 0.10)

    # --- Automation safety: all dangerous/irreversible actions default OFF ---
    auto_generate_applications: bool = _bool("AUTO_GENERATE_APPLICATIONS", True)
    auto_submit_applications: bool = _bool("AUTO_SUBMIT_APPLICATIONS", False)
    allow_browser_automation: bool = _bool("ALLOW_BROWSER_AUTOMATION", False)

    # --- Cover letter ---
    cover_letter_min_words: int = _int("COVER_LETTER_MIN_WORDS", 250)
    cover_letter_max_words: int = _int("COVER_LETTER_MAX_WORDS", 400)

    # --- Database ---
    database_path: str = os.getenv("DATABASE_PATH", "./data/jobs.db")

    # --- PDF ---
    pdf_engine: str = os.getenv("PDF_ENGINE", "playwright")

    # --- Notifications ---
    notify_desktop: bool = _bool("NOTIFY_DESKTOP", True)
    notify_telegram: bool = _bool("NOTIFY_TELEGRAM", False)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    def database_abspath(self) -> Path:
        p = Path(self.database_path)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


settings = Settings()
