"""Application settings (.env) and candidate profile (config/profile.yaml) loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-flash-latest"
    llm_rpm_delay: float = 0.0
    llm_reasoning_effort: str = "low"
    llm_concurrency: int = 10

    profile_path: Path = Path("config/profile.yaml")
    cv_path: Path = Path("config/base_cv.md")
    db_path: Path = Path("data/jobs.db")
    rejected_log_path: Path = Path("data/rejected_log.jsonl")
    reports_dir: Path = Path("reports")

    def require_api_key(self) -> str:
        """Validate that LLM_API_KEY is configured before running Stage 2 evaluation."""
        key = self.llm_api_key.strip()
        if not key:
            raise ValueError(
                "LLM_API_KEY is required for Stage 2 evaluation. "
                "Set it in .env, environment variables, or run with --dry-run."
            )
        return key


class ProfileConfig(BaseModel):
    """Candidate filtering rules, scoring threshold, and expectations."""

    min_match_score: int = Field(default=7, ge=1, le=10)
    must_have_any_tech: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    allowed_workplace_types: list[str] = Field(default_factory=lambda: ["remote", "hybrid"])
    allowed_cities: list[str] = Field(default_factory=list)
    min_salary_pln: int | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    free_text_expectations: str = ""


def load_profile(path: Path) -> ProfileConfig:
    """Load and validate candidate profile from flat or nested YAML."""
    if not path.exists():
        raise FileNotFoundError(f"Profile configuration not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Profile configuration must be a YAML mapping: {path}")

    data = dict(raw)
    candidate = data.pop("candidate", None)
    if isinstance(candidate, dict):
        if "min_match_score" in candidate and "min_match_score" not in data:
            data["min_match_score"] = candidate["min_match_score"]
        if "target_levels" in candidate:
            prefs = dict(data.get("preferences") or {})
            prefs.setdefault("target_levels", candidate["target_levels"])
            data["preferences"] = prefs

    hard_filters = data.pop("hard_filters", None)
    if isinstance(hard_filters, dict):
        for key, value in hard_filters.items():
            data.setdefault(key, value)

    return ProfileConfig.model_validate(data)


def load_cv(path: Path) -> str:
    """Load candidate baseline Markdown CV."""
    if not path.exists():
        raise FileNotFoundError(f"Baseline CV file not found: {path}")
    return path.read_text(encoding="utf-8")
