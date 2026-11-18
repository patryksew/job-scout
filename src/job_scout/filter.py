"""Stage 1 deterministic rule-based pre-filter for job offers."""

from __future__ import annotations

import re

from job_scout.config import ProfileConfig
from job_scout.models import JobOffer


def _compile_term_pattern(term: str) -> re.Pattern[str]:
    """Compile a case-insensitive regex respecting word boundaries for alphanumeric edges."""
    escaped = re.escape(term.strip())
    prefix = r"(?<!\w)" if term.strip()[0].isalnum() else ""
    suffix = r"(?!\w)" if term.strip()[-1].isalnum() else ""
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


class Stage1Filter:
    """Applies deterministic rules from profile.yaml before Stage 2 LLM calls."""

    def __init__(self, profile: ProfileConfig) -> None:
        self.profile = profile
        self._allowed_workplaces = {w.strip().lower() for w in profile.allowed_workplace_types}
        self._allowed_cities = [c.strip().lower() for c in profile.allowed_cities if c.strip()]
        self._must_have_patterns = [
            (tech, _compile_term_pattern(tech))
            for tech in profile.must_have_any_tech
            if tech.strip()
        ]
        self._exclude_patterns = [
            (kw, _compile_term_pattern(kw))
            for kw in profile.exclude_keywords
            if kw.strip()
        ]

    def check_workplace_and_location(self, offer: JobOffer) -> str | None:
        """Validate workplace_type and allowed_cities for hybrid/onsite offers."""
        workplace = offer.workplace_type.lower()
        loc_display = ", ".join(offer.locations)

        if self._allowed_workplaces and workplace not in self._allowed_workplaces:
            if loc_display:
                return f"Workplace mismatch: {workplace} in {loc_display}"
            return f"Workplace mismatch: {workplace}"

        if workplace in {"hybrid", "onsite"} and self._allowed_cities:
            if not offer.locations:
                return f"Workplace mismatch: {workplace} with unspecified city"
            matched = any(
                allowed in loc.lower()
                for loc in offer.locations
                for allowed in self._allowed_cities
            )
            if not matched:
                return f"Workplace mismatch: {workplace} in {loc_display}"

        return None

    def check_salary(self, offer: JobOffer) -> str | None:
        """Reject only when max salary is disclosed and strictly below min_salary_pln."""
        min_sal = self.profile.min_salary_pln
        if min_sal is None or offer.salary_max_pln is None:
            return None
        if offer.salary_max_pln < min_sal:
            return f"Salary too low: max {offer.salary_max_pln} PLN < min {min_sal} PLN"
        return None

    def check_excluded_keywords(self, text_sources: list[str]) -> str | None:
        """Return rejection reason if any deal-breaker keyword matches."""
        combined = "\n".join(part for part in text_sources if part)
        if not combined:
            return None
        for raw_kw, pattern in self._exclude_patterns:
            if pattern.search(combined):
                return f"Excluded keyword matched: '{raw_kw}'"
        return None

    def check_must_have_tech(self, text_sources: list[str]) -> str | None:
        """Ensure at least one required technology matches in title, skills, or description."""
        if not self._must_have_patterns:
            return None
        combined = "\n".join(part for part in text_sources if part)
        if not combined:
            return f"Missing required tech: none of {self.profile.must_have_any_tech} found"
        for _, pattern in self._must_have_patterns:
            if pattern.search(combined):
                return None
        return f"Missing required tech: none of {self.profile.must_have_any_tech} found"

    def filter_lightweight(self, offer: JobOffer) -> str | None:
        """Run lightweight Stage 1 checks on list-level metadata before fetching full detail."""
        if reason := self.check_workplace_and_location(offer):
            return reason
        if reason := self.check_salary(offer):
            return reason

        text_sources = [offer.title, offer.experience_level or "", *offer.skill_names()]
        if reason := self.check_excluded_keywords(text_sources):
            return reason

        # Only enforce must_have_any_tech on list metadata when skills/tags were returned
        if offer.skills:
            if reason := self.check_must_have_tech([offer.title, *offer.skill_names()]):
                return reason

        return None

    def filter_offer(self, offer: JobOffer) -> str | None:
        """Run full Stage 1 deterministic rules on a complete JobOffer."""
        if reason := self.check_workplace_and_location(offer):
            return reason
        if reason := self.check_salary(offer):
            return reason

        text_sources = [
            offer.title,
            offer.experience_level or "",
            *offer.skill_names(),
            offer.description,
        ]
        if reason := self.check_excluded_keywords(text_sources):
            return reason
        if reason := self.check_must_have_tech([offer.title, *offer.skill_names(), offer.description]):
            return reason

        return None
