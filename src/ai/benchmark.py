"""
Benchmark locally-installed Ollama models for suitability as the IT Job Hunter
analysis model.

We care about two things, in priority order:
  1. STRICT JSON compliance — the job-analysis, resume-tailoring, and QC
     stages all depend on the model returning parseable, correctly-shaped
     JSON. A model that "sounds smart" but can't reliably do this is a
     liability, because every invalid response triggers a retry and
     eventually a fail-safe (job left unanalyzed) per the spec.
  2. Speed — this runs on a laptop, not a server, and may process dozens of
     jobs per run.

Usage:
    python -m src.ai.benchmark
    python -m src.ai.benchmark --models qwen3:8b llama3:latest mistral:latest
"""
from __future__ import annotations

import argparse
import json
import time

from src.ai.ollama_provider import OllamaProvider
from src.config import settings

SAMPLE_JOB_TITLE = "IT Support Officer"
SAMPLE_JOB_DESCRIPTION = """
We are seeking an IT Support Officer to join our Sydney service desk team.
You will provide Level 1 technical support to staff, troubleshoot hardware
and software issues, manage tickets in our helpdesk system, and assist with
onboarding new employees' IT equipment. No prior professional experience
required — this is an entry-level / graduate-friendly role. Familiarity with
Windows, Active Directory, and basic networking is a plus. Some exposure to
scripting (Python or PowerShell) is highly regarded but not essential.
"""

ANALYSIS_PROMPT_TEMPLATE = """You are analyzing a job listing for an entry-level IT job seeker in Australia.

Job title: {title}
Job description:
{description}

Return a JSON object with EXACTLY this shape:
{{
  "category": "ENTRY_LEVEL_IT" | "MID_SENIOR_IT" | "NON_IT" | "UNCLEAR",
  "fit_score": <integer 0-100>,
  "experience_required": "<short string like '0-1 years'>",
  "desk_based": <true|false>,
  "recommendation": "APPLY" | "SKIP" | "REVIEW",
  "matched_skills": [<strings>],
  "missing_skills": [<strings>],
  "relevant_keywords": [<strings>],
  "reasons": [<strings>],
  "concerns": [<strings>]
}}
"""

REQUIRED_KEYS = {
    "category",
    "fit_score",
    "experience_required",
    "desk_based",
    "recommendation",
    "matched_skills",
    "missing_skills",
    "relevant_keywords",
    "reasons",
    "concerns",
}


def validate_shape(data: dict) -> list[str]:
    """Return a list of shape problems (empty list = valid)."""
    problems = []
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        problems.append(f"missing keys: {sorted(missing)}")
    if "fit_score" in data and not isinstance(data["fit_score"], (int, float)):
        problems.append("fit_score is not numeric")
    if "desk_based" in data and not isinstance(data["desk_based"], bool):
        problems.append("desk_based is not boolean")
    for list_field in ("matched_skills", "missing_skills", "relevant_keywords", "reasons", "concerns"):
        if list_field in data and not isinstance(data[list_field], list):
            problems.append(f"{list_field} is not a list")
    return problems


def benchmark_model(model: str, base_url: str, runs: int = 2) -> dict:
    provider = OllamaProvider(base_url=base_url, model=model, timeout=settings.ollama_timeout_seconds)
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(title=SAMPLE_JOB_TITLE, description=SAMPLE_JOB_DESCRIPTION)

    result = {
        "model": model,
        "runs": [],
        "json_success_count": 0,
        "shape_valid_count": 0,
        "avg_seconds": None,
        "error": None,
    }

    for i in range(runs):
        start = time.monotonic()
        try:
            data = provider.generate_json(prompt, max_retries=0)
            elapsed = time.monotonic() - start
            problems = validate_shape(data)
            result["runs"].append(
                {
                    "seconds": round(elapsed, 2),
                    "valid_json": True,
                    "shape_problems": problems,
                    "fit_score": data.get("fit_score"),
                    "recommendation": data.get("recommendation"),
                }
            )
            result["json_success_count"] += 1
            if not problems:
                result["shape_valid_count"] += 1
        except Exception as exc:  # noqa: BLE001 — benchmark must not crash on a bad model
            elapsed = time.monotonic() - start
            result["runs"].append({"seconds": round(elapsed, 2), "valid_json": False, "error": str(exc)})

    successful_times = [r["seconds"] for r in result["runs"] if r.get("valid_json")]
    if successful_times:
        result["avg_seconds"] = round(sum(successful_times) / len(successful_times), 2)

    return result


def recommend(results: list[dict]) -> str | None:
    """Pick the model with the most shape-valid JSON responses, tie-broken by speed."""
    candidates = [r for r in results if r["shape_valid_count"] > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (-r["shape_valid_count"], r["avg_seconds"] or float("inf")))
    return candidates[0]["model"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local Ollama models for IT Job Hunter")
    parser.add_argument("--models", nargs="*", default=None, help="Models to test (default: all pulled models)")
    parser.add_argument("--runs", type=int, default=2, help="Runs per model")
    parser.add_argument("--base-url", default=settings.ollama_base_url)
    args = parser.parse_args()

    import requests

    models = args.models
    if not models:
        try:
            resp = requests.get(f"{args.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
        except requests.RequestException as exc:
            print(f"ERROR: could not reach Ollama at {args.base_url}: {exc}")
            print("Is Ollama running? Try: ollama serve")
            return

    if not models:
        print("No Ollama models found. Pull one first, e.g.: ollama pull qwen3:8b")
        return

    print(f"Benchmarking {len(models)} model(s) against {args.base_url}: {models}\n")

    results = []
    for model in models:
        print(f"--- {model} ---")
        result = benchmark_model(model, args.base_url, runs=args.runs)
        results.append(result)
        for i, run in enumerate(result["runs"], 1):
            if run.get("valid_json"):
                print(
                    f"  run {i}: {run['seconds']}s, JSON OK, "
                    f"shape_problems={run['shape_problems'] or 'none'}, "
                    f"fit_score={run.get('fit_score')}, rec={run.get('recommendation')}"
                )
            else:
                print(f"  run {i}: {run['seconds']}s, FAILED — {run.get('error')}")
        print(
            f"  => {result['json_success_count']}/{len(result['runs'])} valid JSON, "
            f"{result['shape_valid_count']}/{len(result['runs'])} correct shape, "
            f"avg {result['avg_seconds']}s\n"
        )

    best = recommend(results)
    print("=" * 50)
    if best:
        print(f"RECOMMENDED MODEL: {best}")
        print(f"Set OLLAMA_MODEL={best} in your .env")
    else:
        print("No model produced valid, correctly-shaped JSON. Consider pulling a different model,")
        print("e.g.: ollama pull qwen2.5:7b-instruct")
    print("=" * 50)

    with open("data/benchmark_results.json", "w") as f:
        json.dump({"results": results, "recommended": best}, f, indent=2)
    print("Full results written to data/benchmark_results.json")


if __name__ == "__main__":
    main()
