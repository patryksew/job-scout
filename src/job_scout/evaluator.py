"""Stage 2 cloud LLM semantic evaluator using OpenAI-compatible Structured Outputs."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import ValidationError

from job_scout.config import ProfileConfig, Settings
from job_scout.models import JobEvaluation, JobOffer

SYSTEM_PROMPT_TEMPLATE = """Jesteś doświadczonym doradcą kariery IT i rekruterem technicznym w Polsce.
Twoim zadaniem jest ocena dopasowania oferty pracy do profilu i CV kandydata.

WAŻNE ZASADY:
1. Wszystkie pola tekstowe w odpowiedzi JSON (`one_line_pitch`, `red_flags`, `cv_focus_tips`, `rejection_reason`) MUSZĄ być napisane poprawnym językiem polskim.
2. Skala `match_score` to liczby całkowite od 1 do 10:
   - 9–10: idealne dopasowanie poziomu (Junior/Mid), technologii i charakteru pracy (realna inżynieria/AI/backend), brak istotnych red flag.
   - 7–8: dobre, warte aplikacji dopasowanie z drobnymi lukami do zaadresowania w CV.
   - 4–6: częściowe dopasowanie, zbyt wysokie wymagania (np. ukryty Senior 5+ lat), praca w utrzymaniu legacy lub mało rozwojowa.
   - 1–3: wyraźne niedopasowanie stacku, roli lub poziomu doświadczenia.
3. Ustaw `is_recommended` na `true` wtedy i tylko wtedy, gdy `match_score >= {min_match_score}`.
4. Jeśli `is_recommended` to `false`, pole `rejection_reason` jest OBOWIĄZKOWE i musi jasno tłumaczyć powód niskiej oceny.
5. `red_flags`: wypisz konkretne ryzyka (np. ukryte wymagania seniorskie mimo tagu Mid, utrzymanie legacy, brak testów/CI, niespójne widełki, niejasny zakres obowiązków). Jeśli brak, zwróć pustą listę.
6. `cv_focus_tips`: podaj 2–3 konkretne wskazówki, które projekty i technologie z bazowego CV kandydat powinien wyeksponować, aplikując na tę ofertę.
"""


class LLMEvaluator:
    """Evaluates Stage 1 survivor offers against base CV and expectations via cloud LLM API."""

    def __init__(
        self,
        settings: Settings,
        profile: ProfileConfig,
        base_cv: str,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.base_cv = base_cv
        self._last_request_ts: float = 0.0
        if client is not None:
            self.client = client
        else:
            api_key = settings.require_api_key()
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=settings.llm_base_url,
                max_retries=0,
                timeout=30.0,
            )

    def _build_messages(self, offer: JobOffer, *, include_schema: bool = False) -> list[dict[str, str]]:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            min_match_score=self.profile.min_match_score
        )
        if include_schema:
            schema_json = json.dumps(JobEvaluation.model_json_schema(), ensure_ascii=False, indent=2)
            system_prompt += (
                "\nZwróć wyłącznie poprawny obiekt JSON zgodny z poniższym schematem Pydantic:\n"
                f"```json\n{schema_json}\n```"
            )

        skills_text = ", ".join(
            f"{s.name} ({s.level})" if s.level is not None else s.name
            for s in offer.skills
        ) or "Brak wyszczególnionych tagów"

        salary_text = "Nieujawnione"
        if offer.salary_min_pln is not None or offer.salary_max_pln is not None:
            lo = offer.salary_min_pln if offer.salary_min_pln is not None else "?"
            hi = offer.salary_max_pln if offer.salary_max_pln is not None else "?"
            salary_text = f"{lo} – {hi} PLN"

        prefs_json = json.dumps(self.profile.preferences, ensure_ascii=False, indent=2)
        user_prompt = (
            f"## BAZOWE CV KANDYDATA\n{self.base_cv}\n\n"
            f"## PREFERENCJE I OCZEKIWANIA KANDYDATA\n"
            f"Preferencje strukturalne:\n{prefs_json}\n\n"
            f"Oczekiwania opisowe:\n{self.profile.free_text_expectations}\n\n"
            f"## OFERTA PRACY DO OCENY\n"
            f"- Portal: {offer.source}\n"
            f"- Stanowisko: {offer.title}\n"
            f"- Firma: {offer.company}\n"
            f"- Tryb pracy: {offer.workplace_type}\n"
            f"- Lokalizacje: {', '.join(offer.locations) or 'Nie podano'}\n"
            f"- Poziom (z tagu): {offer.experience_level or 'Nie podano'}\n"
            f"- Wynagrodzenie (PLN/mies.): {salary_text}\n"
            f"- Technologie / Umiejętności: {skills_text}\n"
            f"- URL: {offer.url}\n\n"
            f"### Pełny opis oferty:\n{offer.description or 'Brak szczegółowego opisu.'}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def _respect_rpm_delay(self) -> None:
        delay = max(0.0, self.settings.llm_rpm_delay)
        if delay <= 0 or self._last_request_ts <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)

    async def _call_once(self, offer: JobOffer) -> JobEvaluation:
        """Attempt Structured Outputs via .parse(), falling back to json_object mode."""
        messages = self._build_messages(offer, include_schema=False)
        use_fallback = False
        extra_kwargs: dict[str, Any] = {}
        effort = self.settings.llm_reasoning_effort.strip()
        if effort:
            extra_kwargs["reasoning_effort"] = effort

        try:
            completion = await self.client.beta.chat.completions.parse(
                model=self.settings.llm_model,
                messages=messages,  # type: ignore[arg-type]
                response_format=JobEvaluation,
                temperature=0.2,
                **extra_kwargs,
            )
            msg = completion.choices[0].message
            if getattr(msg, "parsed", None) is not None:
                parsed: JobEvaluation = msg.parsed
                return parsed
            if msg.content:
                return JobEvaluation.model_validate_json(msg.content)
            use_fallback = True
        except (
            AttributeError,
            NotImplementedError,
            openai.BadRequestError,
            openai.NotFoundError,
        ):
            use_fallback = True

        if use_fallback:
            fallback_messages = self._build_messages(offer, include_schema=True)
            completion = await self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=fallback_messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
                temperature=0.2,
                **extra_kwargs,
            )
            content = completion.choices[0].message.content or ""
            return JobEvaluation.model_validate_json(content)

        raise ValueError("LLM response contained neither parsed object nor JSON content.")

    async def evaluate_offer(self, offer: JobOffer, max_attempts: int = 3) -> JobEvaluation:
        """Evaluate a single offer with rate-limit delay and exponential backoff retries."""
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            await self._respect_rpm_delay()
            self._last_request_ts = time.monotonic()

            try:
                evaluation = await self._call_once(offer)
                return self._normalize_evaluation(evaluation)
            except (
                openai.RateLimitError,
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.InternalServerError,
                openai.APIStatusError,
                ValidationError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                last_exc = exc
                if isinstance(exc, openai.APIStatusError) and exc.status_code not in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise
                if attempt == max_attempts - 1:
                    raise
                await asyncio.sleep(2**attempt)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unexpected termination of LLM evaluation retry loop")

    def _normalize_evaluation(self, evaluation: JobEvaluation) -> JobEvaluation:
        """Enforce threshold consistency and non-empty rejection reason in Polish."""
        recommended = evaluation.match_score >= self.profile.min_match_score
        rejection_reason = evaluation.rejection_reason
        if not recommended and not (rejection_reason and rejection_reason.strip()):
            rejection_reason = (
                f"Ocena dopasowania ({evaluation.match_score}/10) "
                f"jest niższa niż wymagany próg ({self.profile.min_match_score}/10)."
            )
        elif recommended:
            rejection_reason = None

        return evaluation.model_copy(
            update={
                "is_recommended": recommended,
                "rejection_reason": rejection_reason,
            }
        )
