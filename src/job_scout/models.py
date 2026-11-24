"""Unified Pydantic v2 schemas for job offers, evaluations, and rejections."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

WorkplaceType = Literal["remote", "hybrid", "onsite"]

_POLISH_TRANSLATION = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)

# Common legal entity forms stripped from company names during cross-portal dedup
_COMPANY_LEGAL_RE = re.compile(
    r"(?<!\w)(?:"
    r"spolka\s+z\s+ograniczona\s+odpowiedzialnoscia"
    r"|spolka\s+z\s+o\.?\s*o\.?"
    r"|spolka\s+akcyjna"
    r"|spolka\s+komandytowa"
    r"|spolka\s+jawna"
    r"|spolka\s+partnerska"
    r"|sp\.?\s*z\.?\s*o\.?\s*o\.?"
    r"|sp\.?\s*k\.?"
    r"|sp\.?\s*j\.?"
    r"|sp\.?\s*p\.?"
    r"|s\.?\s*k\.?\s*a\.?"
    r"|p\.?\s*s\.?\s*a\.?"
    r"|s\.\s*a\.?"
    r"|s\.a"
    r"|ltd\.?"
    r"|llc\.?"
    r"|inc\.?"
    r"|gmbh"
    r"|corp\.?"
    r"|plc"
    r"|b\.?\s*v\.?"
    r"|s\.?\s*r\.?\s*o\.?"
    r")(?=\W|$)",
    re.IGNORECASE,
)
_COMPANY_TRAILING_SA_RE = re.compile(r"(?:^|\s)sa\s*$", re.IGNORECASE)
_COMPANY_PAREN_NOISE_RE = re.compile(
    r"[\(\[]\s*(?:warszawa|warsaw|krakow|cracow|wroclaw|gdansk|poznan|lodz|katowice|poland|polska|remote|zdalnie|zdalna)\s*[\)\]]",
    re.IGNORECASE,
)
_COMPANY_TRAILING_LOCATION_RE = re.compile(
    r"(?:[\s,\-/]+(?:warszawa|warsaw|remote|zdalnie|zdalna))+\s*$",
    re.IGNORECASE,
)

# Tech symbol tokens preserved semantically before stripping punctuation
_TECH_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\w)c\+\+(?!\w)", re.IGNORECASE), "cpp"),
    (re.compile(r"(?<!\w)c#(?!\w)", re.IGNORECASE), "csharp"),
    (re.compile(r"(?<!\w)f#(?!\w)", re.IGNORECASE), "fsharp"),
    (re.compile(r"(?<!\w)\.net(?!\w)", re.IGNORECASE), "dotnet"),
    (re.compile(r"(?<!\w)node\.js(?!\w)", re.IGNORECASE), "nodejs"),
    (re.compile(r"(?<!\w)vue\.js(?!\w)", re.IGNORECASE), "vuejs"),
    (re.compile(r"(?<!\w)next\.js(?!\w)", re.IGNORECASE), "nextjs"),
    (re.compile(r"(?<!\w)react\.js(?!\w)", re.IGNORECASE), "reactjs"),
)

# Work-mode, location, contract, and gender-tag noise stripped from job titles
_TITLE_NOISE_PHRASES_RE = re.compile(
    r"(?<!\w)(?:"
    r"100\s*%?\s*remote"
    r"|fully\s+remote"
    r"|praca\s+zdalna"
    r"|praca\s+hybrydowa"
    r"|praca\s+stacjonarna"
    r"|on[\s\-]?site"
    r"|f\s*/\s*m\s*/\s*d"
    r"|m\s*/\s*f\s*/\s*d"
    r"|k\s*/\s*m\s*/\s*[xn]"
    r"|m\s*/\s*k\s*/\s*[xn]"
    r"|m\s*/\s*k"
    r"|k\s*/\s*m"
    r"|b2b\s*/\s*uop"
    r"|uop\s*/\s*b2b"
    r")(?=\W|$)",
    re.IGNORECASE,
)
_TITLE_NOISE_WORDS_RE = re.compile(
    r"(?<!\w)(?:"
    r"remote|zdalnie|zdalna|zdalny|hybrid|hybrydowo|hybrydowa|hybrydowy"
    r"|stacjonarnie|stacjonarna|stacjonarny|onsite"
    r"|warszawa|warsaw|krakow|cracow|wroclaw|gdansk|poznan|lodz|katowice|poland|polska"
    r"|b2b|uop"
    r")(?=\W|$)",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _fold_ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_POLISH_TRANSLATION).lower()


def normalize_company(company: str) -> str:
    """Normalize company name for cross-portal deduplication."""
    base = _fold_ascii_lower(company).strip()
    if not base:
        return ""
    cleaned = _COMPANY_PAREN_NOISE_RE.sub(" ", base)
    cleaned = _COMPANY_LEGAL_RE.sub(" ", cleaned)
    cleaned = _COMPANY_TRAILING_LOCATION_RE.sub(" ", cleaned)
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned).strip()
    cleaned = _COMPANY_TRAILING_SA_RE.sub("", cleaned).strip()
    if not cleaned:
        cleaned = _NON_ALNUM_RE.sub(" ", base).strip()
    return cleaned


def normalize_title(title: str) -> str:
    """Normalize job title for cross-portal deduplication."""
    base = _fold_ascii_lower(title).strip()
    if not base:
        return ""
    cleaned = base
    for pattern, repl in _TECH_REPLACEMENTS:
        cleaned = pattern.sub(repl, cleaned)
    cleaned = _TITLE_NOISE_PHRASES_RE.sub(" ", cleaned)
    cleaned = _TITLE_NOISE_WORDS_RE.sub(" ", cleaned)
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned).strip()
    if not cleaned:
        fallback = base
        for pattern, repl in _TECH_REPLACEMENTS:
            fallback = pattern.sub(repl, fallback)
        cleaned = _NON_ALNUM_RE.sub(" ", fallback).strip()
    return cleaned


def compute_normalized_fingerprint(company: str, title: str) -> str:
    """Return sha256(normalized_company + '|' + normalized_title) for cross-portal dedup."""
    raw = f"{normalize_company(company)}|{normalize_title(title)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_legacy_fingerprint(company: str, title: str) -> str:
    """Return legacy sha256(lowercase(company) + '|' + lowercase(title))."""
    raw = f"{company.strip().lower()}|{title.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
        """Return sha256(normalized_company + '|' + normalized_title) for cross-portal dedup."""
        return compute_normalized_fingerprint(self.company, self.title)

    def compute_legacy_fingerprint(self) -> str:
        """Return legacy fingerprint for backward compatibility with unmigrated SQLite rows."""
        return compute_legacy_fingerprint(self.company, self.title)

    def skill_names(self) -> list[str]:
        """Return a flat list of skill names."""
        return [s.name for s in self.skills if s.name]


class SourceRunStats(BaseModel):
    """Per-portal execution health and funnel counters for daily reporting."""

    source: str
    fetched: int = 0
    passed_prefilter: int = 0
    duplicates_skipped: int = 0
    rule_rejected: int = 0
    llm_evaluated: int = 0
    llm_rejected: int = 0
    shortlisted: int = 0
    status: str = "OK"
    error: str | None = None

    @property
    def status_display(self) -> str:
        """Formatted status string for terminal, Markdown, and HTML tables."""
        if self.error:
            return f"BŁĄD: {self.error}"
        return self.status


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
