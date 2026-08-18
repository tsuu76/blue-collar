"""
Load/save the master resume JSON file. Thin wrapper so every other module
goes through validation (pydantic) rather than reading raw JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.config import PROJECT_ROOT

from .schema import MasterResume

DEFAULT_PATH = PROJECT_ROOT / "data" / "master_resume.json"


def load_master_resume(path: str | Path | None = None) -> MasterResume:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"Master resume not found at {p}. Create it first — see "
            f"templates/master_resume.example.json for the structure. "
            f"This file must contain only real, factual information; nothing "
            f"here is ever auto-generated."
        )
    data = json.loads(p.read_text())
    return MasterResume.model_validate(data)


def save_master_resume(resume: MasterResume, path: str | Path | None = None) -> None:
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(resume.model_dump(), indent=2))
