"""SQLite storage, cross-portal deduplication, and calibration rejection logging."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from job_scout.models import EvaluatedOffer, JobEvaluation, JobOffer, RejectionRecord, SkillItem


class DatabaseManager:
    """Manages jobs.db SQLite persistence and rejected_log.jsonl audit logs."""

    def __init__(self, db_path: Path, rejected_log_path: Path, dry_run: bool = False) -> None:
        self.db_path = db_path
        self.rejected_log_path = rejected_log_path
        self.dry_run = dry_run
        self._conn = self._connect()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self.dry_run:
            if self.db_path.exists():
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            else:
                conn = sqlite3.connect(":memory:")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        if self.dry_run and self.db_path.exists():
            return
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                url TEXT NOT NULL,
                locations_json TEXT NOT NULL DEFAULT '[]',
                workplace_type TEXT NOT NULL DEFAULT 'onsite',
                experience_level TEXT,
                salary_min_pln INTEGER,
                salary_max_pln INTEGER,
                skills_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                match_score INTEGER,
                rejection_reason TEXT,
                evaluation_json TEXT,
                published_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(source, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_offers_fingerprint ON offers(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_offers_created_at ON offers(created_at);
            CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
            """
        )
        cols = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(offers)").fetchall()
        }
        if "published_at" not in cols:
            self._conn.execute("ALTER TABLE offers ADD COLUMN published_at TEXT")
        self._conn.commit()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()

    def is_duplicate(
        self,
        offer: JobOffer,
        seen_fingerprints: set[str] | None = None,
    ) -> tuple[bool, str | None]:
        """Check whether an offer was already seen by (source, external_id) or 30-day fingerprint."""
        try:
            row = self._conn.execute(
                "SELECT 1 FROM offers WHERE source = ? AND external_id = ? LIMIT 1",
                (offer.source, offer.external_id),
            ).fetchone()
            if row is not None:
                return True, "already_seen_source_id"

            fp = offer.compute_fingerprint()
            if seen_fingerprints is not None and fp in seen_fingerprints:
                return True, "cross_portal_duplicate"

            fp_row = self._conn.execute(
                """
                SELECT 1 FROM offers
                WHERE fingerprint = ?
                  AND created_at >= datetime('now', '-30 days')
                LIMIT 1
                """,
                (fp,),
            ).fetchone()
            if fp_row is not None:
                return True, "cross_portal_duplicate_30d"
        except sqlite3.OperationalError:
            # Handle empty pre-schema read-only file gracefully in dry-run
            return False, None

        return False, None

    def _upsert_offer(
        self,
        offer: JobOffer,
        *,
        status: str,
        match_score: int | None = None,
        rejection_reason: str | None = None,
        evaluation_json: str | None = None,
    ) -> None:
        if self.dry_run:
            return

        now_sql = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            """
            INSERT INTO offers (
                source, external_id, fingerprint, title, company, url,
                locations_json, workplace_type, experience_level,
                salary_min_pln, salary_max_pln, skills_json,
                status, match_score, rejection_reason, evaluation_json, published_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                title = excluded.title,
                company = excluded.company,
                url = excluded.url,
                locations_json = excluded.locations_json,
                workplace_type = excluded.workplace_type,
                experience_level = excluded.experience_level,
                salary_min_pln = excluded.salary_min_pln,
                salary_max_pln = excluded.salary_max_pln,
                skills_json = excluded.skills_json,
                status = excluded.status,
                match_score = excluded.match_score,
                rejection_reason = excluded.rejection_reason,
                evaluation_json = excluded.evaluation_json,
                published_at = COALESCE(excluded.published_at, offers.published_at)
            """,
            (
                offer.source,
                offer.external_id,
                offer.compute_fingerprint(),
                offer.title,
                offer.company,
                offer.url,
                json.dumps(offer.locations, ensure_ascii=False),
                offer.workplace_type,
                offer.experience_level,
                offer.salary_min_pln,
                offer.salary_max_pln,
                json.dumps([s.model_dump() for s in offer.skills], ensure_ascii=False),
                status,
                match_score,
                rejection_reason,
                evaluation_json,
                offer.published_at,
                now_sql,
            ),
        )
        self._conn.commit()

    def _append_rejection_log(self, record: RejectionRecord) -> None:
        if self.dry_run:
            return
        self.rejected_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.rejected_log_path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")

    def record_rule_rejection(self, offer: JobOffer, reason: str) -> RejectionRecord:
        """Persist a Stage 1 rule-based rejection to SQLite and rejected_log.jsonl."""
        record = RejectionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=offer.source,
            external_id=offer.external_id,
            title=offer.title,
            company=offer.company,
            url=offer.url,
            stage="rule",
            match_score=None,
            rejection_reason=reason,
        )
        self._upsert_offer(
            offer,
            status="rejected_rule",
            match_score=None,
            rejection_reason=reason,
        )
        self._append_rejection_log(record)
        return record

    def record_llm_result(
        self, offer: JobOffer, evaluation: JobEvaluation
    ) -> RejectionRecord | None:
        """Persist Stage 2 LLM evaluation; return RejectionRecord if not recommended."""
        eval_json = evaluation.model_dump_json()
        if evaluation.is_recommended:
            self._upsert_offer(
                offer,
                status="shortlisted",
                match_score=evaluation.match_score,
                rejection_reason=None,
                evaluation_json=eval_json,
            )
            return None

        reason = evaluation.rejection_reason or "Ocena dopasowania poniżej progu."
        record = RejectionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=offer.source,
            external_id=offer.external_id,
            title=offer.title,
            company=offer.company,
            url=offer.url,
            stage="llm",
            match_score=evaluation.match_score,
            rejection_reason=reason,
        )
        self._upsert_offer(
            offer,
            status="rejected_llm",
            match_score=evaluation.match_score,
            rejection_reason=reason,
            evaluation_json=eval_json,
        )
        self._append_rejection_log(record)
        return record

    def get_db_stats(self) -> dict[str, Any]:
        """Return aggregate statistics from SQLite offers table."""
        stats: dict[str, Any] = {
            "total": 0,
            "by_status": {},
            "by_source": {},
            "avg_shortlisted_score": None,
        }
        try:
            total_row = self._conn.execute("SELECT COUNT(*) AS cnt FROM offers").fetchone()
            stats["total"] = int(total_row["cnt"]) if total_row else 0

            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM offers GROUP BY status"
            ):
                stats["by_status"][row["status"]] = int(row["cnt"])

            for row in self._conn.execute(
                "SELECT source, COUNT(*) AS cnt FROM offers GROUP BY source"
            ):
                stats["by_source"][row["source"]] = int(row["cnt"])

            avg_row = self._conn.execute(
                "SELECT AVG(match_score) AS avg_score FROM offers WHERE status = 'shortlisted'"
            ).fetchone()
            if avg_row and avg_row["avg_score"] is not None:
                stats["avg_shortlisted_score"] = round(float(avg_row["avg_score"]), 2)
        except sqlite3.OperationalError:
            pass
        return stats

    def get_rejection_log_records(self) -> list[RejectionRecord]:
        """Load all calibration rejection records from rejected_log.jsonl."""
        if not self.rejected_log_path.exists():
            return []
        records: list[RejectionRecord] = []
        for line in self.rejected_log_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(RejectionRecord.model_validate_json(stripped))
            except Exception:
                continue
        return records

    def get_shortlisted_offers(
        self,
        *,
        report_date: date | None = None,
        max_age_days: int | None = None,
    ) -> list[EvaluatedOffer]:
        """Reconstruct stored shortlisted EvaluatedOffer items from SQLite."""
        results: list[EvaluatedOffer] = []
        where_clauses = ["status = 'shortlisted'", "evaluation_json IS NOT NULL"]
        params: list[Any] = []
        if report_date is not None:
            where_clauses.append("date(created_at) = ?")
            params.append(report_date.isoformat())
        if max_age_days is not None:
            where_clauses.append("created_at >= datetime('now', ?)")
            params.append(f"-{int(max_age_days)} days")
        where_sql = " AND ".join(where_clauses)
        try:
            rows = self._conn.execute(
                f"""
                SELECT source, external_id, title, company, url, locations_json,
                       workplace_type, experience_level, salary_min_pln, salary_max_pln,
                       skills_json, evaluation_json, published_at, created_at
                FROM offers
                WHERE {where_sql}
                ORDER BY match_score DESC, COALESCE(published_at, created_at) DESC
                """,
                tuple(params),
            ).fetchall()
        except sqlite3.OperationalError:
            return results

        for row in rows:
            try:
                locations = json.loads(row["locations_json"] or "[]")
                raw_skills = json.loads(row["skills_json"] or "[]")
                skills = [SkillItem.model_validate(s) for s in raw_skills]
                pub = row["published_at"]
                if not pub and row["created_at"]:
                    pub = str(row["created_at"]).replace(" ", "T") + "Z"
                offer = JobOffer(
                    external_id=row["external_id"],
                    source=row["source"],
                    url=row["url"],
                    title=row["title"],
                    company=row["company"],
                    locations=locations,
                    workplace_type=row["workplace_type"],
                    experience_level=row["experience_level"],
                    salary_min_pln=row["salary_min_pln"],
                    salary_max_pln=row["salary_max_pln"],
                    skills=skills,
                    published_at=pub,
                )
                evaluation = JobEvaluation.model_validate_json(row["evaluation_json"])
                results.append(EvaluatedOffer(offer=offer, evaluation=evaluation))
            except Exception:
                continue
        return results

    @staticmethod
    def summarize_rejection_reasons(
        records: list[RejectionRecord], top_n: int = 10
    ) -> list[tuple[str, str, int]]:
        """Return top (stage, rejection_reason, count) tuples."""
        counter: Counter[tuple[str, str]] = Counter(
            (rec.stage, rec.rejection_reason) for rec in records
        )
        return [(stage, reason, count) for (stage, reason), count in counter.most_common(top_n)]
