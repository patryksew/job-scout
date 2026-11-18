"""Unified Pydantic v2 schemas for job offers, evaluations, and rejections."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

WorkplaceType = Literal["remote", "hybrid", "onsite"]


class SkillItem(BaseModel):
    """Normalized representation of a required or nice-to-have skill."""

    name: str
    level: str | int | None = None


class JobOffer(BaseModel):
    """Unified job offer representation normalized across portals."""

    external_id: str
    source: str
    url: str
    title: str
    company: str
    locations: list[str] = Field(default_factory=list)
    workplace_type: WorkplaceType = "onsite"
    experience_level: str | None = None
    salary_min_pln: int | None = None
    salary_max_pln: int | None = None
    skills: list[SkillItem] = Field(default_factory=list)
    description: str = ""
    published_at: str | None = None
    metadata_rejected_reason: str | None = Field(default=None, exclude=True)

    def compute_fingerprint(self) -> str:
        """Return sha256(lowercase(company) + '|' + lowercase(title)) for cross-portal dedup."""
        raw = f"{self.company.strip().lower()}|{self.title.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def skill_names(self) -> list[str]:
        """Return a flat list of skill names."""
        return [s.name for s in self.skills if s.name]


class JobEvaluation(BaseModel):
    """Structured LLM evaluation response (text fields written in Polish)."""

    match_score: int = Field(..., ge=1, le=10)
    is_recommended: bool
    one_line_pitch: str
    red_flags: list[str] = Field(default_factory=list)
    cv_focus_tips: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None


class RejectionRecord(BaseModel):
    """Calibration audit log record written to data/rejected_log.jsonl."""

    timestamp: str
    source: str
    external_id: str = ""
    title: str
    company: str
    url: str
    stage: Literal["rule", "llm"]
    match_score: int | None = None
    rejection_reason: str


class EvaluatedOffer(BaseModel):
    """Pair of normalized job offer and its Stage 2 LLM evaluation."""

    offer: JobOffer
    evaluation: JobEvaluation
