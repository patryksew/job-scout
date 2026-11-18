"""CLI entrypoint for job-scout (run, dry-run, stats)."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from rich.console import Console

from job_scout.config import Settings, load_cv, load_profile
from job_scout.database import DatabaseManager
from job_scout.evaluator import LLMEvaluator
from job_scout.filter import Stage1Filter
from job_scout.models import EvaluatedOffer, JobEvaluation, JobOffer, RejectionRecord
from job_scout.reporter import (
    RunMetrics,
    generate_html_report,
    generate_markdown_report,
    print_run_summary,
    print_stats,
)
from job_scout.scrapers.justjoin import JustJoinScraper
from job_scout.scrapers.solid_jobs import SolidJobsScraper


def _unique_run_stamp(reports_dir: Path, now: datetime) -> str:
    """Return a unique YYYY-MM-DD_HH-MM timestamped prefix that avoids overwriting earlier runs."""
    base = now.strftime("%Y-%m-%d_%H-%M")
    if not (
        (reports_dir / f"{base}_report.md").exists()
        or (reports_dir / f"{base}_report.html").exists()
    ):
        return base

    sec_stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    if not (
        (reports_dir / f"{sec_stamp}_report.md").exists()
        or (reports_dir / f"{sec_stamp}_report.html").exists()
    ):
        return sec_stamp

    idx = 2
    while True:
        candidate = f"{sec_stamp}_{idx}"
        if not (
            (reports_dir / f"{candidate}_report.md").exists()
            or (reports_dir / f"{candidate}_report.html").exists()
        ):
            return candidate
        idx += 1


def write_daily_master_reports(
    db: DatabaseManager,
    reports_dir: Path,
    report_date: date,
) -> tuple[Path, Path, int]:
    """Build cumulative YYYY-MM-DD_master.{md,html} for the given date from SQLite & logs."""
    date_str = report_date.isoformat()
    shortlisted = db.get_shortlisted_offers(report_date=report_date)
    records = [
        rec
        for rec in db.get_rejection_log_records()
        if rec.timestamp[:10] == date_str
    ]

    llm_rej = [r for r in records if r.stage == "llm"]
    rule_rej = [r for r in records if r.stage == "rule"]
    metrics = RunMetrics(
        fetched=len(shortlisted) + len(llm_rej) + len(rule_rej),
        duplicates_skipped=0,
        rule_rejected=len(rule_rej),
        llm_rejected=len(llm_rej),
        shortlisted=len(shortlisted),
    )
    master_md = reports_dir / f"{date_str}_master.md"
    master_html = reports_dir / f"{date_str}_master.html"
    generate_markdown_report(
        report_date,
        metrics,
        shortlisted,
        llm_rej,
        rule_rej,
        master_md,
        write_step_summary=False,
    )
    generate_html_report(
        report_date,
        metrics,
        shortlisted,
        llm_rej,
        rule_rej,
        master_html,
    )
    return master_md, master_html, len(shortlisted)


async def execute_run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    settings: Settings | None = None,
    console: Console | None = None,
) -> RunMetrics:
    """Run the full job-scout pipeline: fetch -> dedup -> filter -> evaluate -> report."""
    cfg = settings or Settings()
    out = console or Console()
    profile = load_profile(cfg.profile_path)
    base_cv = load_cv(cfg.cv_path)

    if not dry_run:
        cfg.require_api_key()

    db = DatabaseManager(cfg.db_path, cfg.rejected_log_path, dry_run=dry_run)
    stage1 = Stage1Filter(profile)
    metrics = RunMetrics(dry_run=dry_run)

    seen_fingerprints: set[str] = set()
    survivors: list[JobOffer] = []
    rule_rejected_records: list[RejectionRecord] = []
    llm_rejected_records: list[RejectionRecord] = []
    shortlisted: list[EvaluatedOffer] = []

    try:
        # 1. Fetch SOLID.Jobs
        async with SolidJobsScraper() as solid_scraper:
            try:
                solid_offers = await solid_scraper.fetch_offers()
            except Exception as exc:
                out.print(f"[yellow]Warning: SOLID.Jobs fetch failed: {exc}[/yellow]")
                solid_offers = []

        metrics.fetched += len(solid_offers)
        for offer in solid_offers:
            is_dup, _ = db.is_duplicate(offer, seen_fingerprints=seen_fingerprints)
            if is_dup:
                metrics.duplicates_skipped += 1
                continue
            seen_fingerprints.add(offer.compute_fingerprint())

            reason = stage1.filter_offer(offer)
            if reason is not None:
                metrics.rule_rejected += 1
                rule_rejected_records.append(db.record_rule_rejection(offer, reason))
            else:
                survivors.append(offer)

        # 2. Fetch Just Join IT with lightweight pre-filtering before detail fetches
        async with JustJoinScraper() as jj_scraper:
            try:
                max_details = max(0, limit - len(survivors)) * 5 if limit is not None else None
                jj_offers = await jj_scraper.fetch_offers(
                    stage1_filter=stage1,
                    db=db,
                    seen_fingerprints=seen_fingerprints,
                    max_detail_fetches=max_details,
                )
                metrics.fetched += jj_scraper.last_total_fetched
                metrics.duplicates_skipped += jj_scraper.last_duplicates_skipped
            except Exception as exc:
                out.print(f"[yellow]Warning: Just Join IT fetch failed: {exc}[/yellow]")
                jj_offers = []

        for offer in jj_offers:
            is_dup, _ = db.is_duplicate(offer, seen_fingerprints=seen_fingerprints)
            if is_dup:
                metrics.duplicates_skipped += 1
                continue
            seen_fingerprints.add(offer.compute_fingerprint())

            reason = offer.metadata_rejected_reason or stage1.filter_offer(offer)
            if reason is not None:
                metrics.rule_rejected += 1
                rule_rejected_records.append(db.record_rule_rejection(offer, reason))
            else:
                survivors.append(offer)

        # 3. Stage 2 LLM evaluation (concurrent, skipped in --dry-run)
        if not dry_run and survivors:
            to_evaluate = survivors[:limit] if limit is not None else survivors
            evaluator = LLMEvaluator(cfg, profile, base_cv)
            sem = asyncio.Semaphore(max(1, cfg.llm_concurrency))

            async def _eval_one(
                target: JobOffer,
            ) -> tuple[JobOffer, JobEvaluation | None, Exception | None]:
                async with sem:
                    try:
                        result = await evaluator.evaluate_offer(target)
                        return target, result, None
                    except Exception as exc:
                        return target, None, exc

            eval_results = await asyncio.gather(*(_eval_one(offer) for offer in to_evaluate))
            for offer, evaluation, err in eval_results:
                if err is not None or evaluation is None:
                    out.print(
                        f"[yellow]Warning: LLM evaluation failed for '{offer.title}' ({offer.company}): {err}[/yellow]"
                    )
                    continue
                rej_record = db.record_llm_result(offer, evaluation)
                if rej_record is not None:
                    metrics.llm_rejected += 1
                    llm_rejected_records.append(rej_record)
                else:
                    metrics.shortlisted += 1
                    shortlisted.append(EvaluatedOffer(offer=offer, evaluation=evaluation))

        # 4. Reporting
        print_run_summary(
            out,
            metrics,
            shortlisted,
            survivors_preview=survivors if dry_run else None,
        )

        if not dry_run:
            now = datetime.now(timezone.utc)
            today = now.date()
            stamp = _unique_run_stamp(cfg.reports_dir, now)
            md_path = cfg.reports_dir / f"{stamp}_report.md"
            html_path = cfg.reports_dir / f"{stamp}_report.html"
            generate_markdown_report(
                today,
                metrics,
                shortlisted,
                llm_rejected_records,
                rule_rejected_records,
                md_path,
                write_step_summary=True,
            )
            generate_html_report(
                today,
                metrics,
                shortlisted,
                llm_rejected_records,
                rule_rejected_records,
                html_path,
            )
            master_md, master_html, master_count = write_daily_master_reports(
                db, cfg.reports_dir, today
            )
            out.print(f"[green]Run reports written:[/green] {md_path} & {html_path}")
            out.print(
                f"[green]Daily master reports updated ({master_count} shortlisted):[/green] "
                f"{master_md} & {master_html}"
            )
    finally:
        db.close()

    return metrics


def execute_stats(settings: Settings | None = None, console: Console | None = None) -> None:
    """Display database stats and top rejection reasons from rejected_log.jsonl."""
    cfg = settings or Settings()
    out = console or Console()
    db = DatabaseManager(cfg.db_path, cfg.rejected_log_path, dry_run=True)
    try:
        db_stats = db.get_db_stats()
        records = db.get_rejection_log_records()
    finally:
        db.close()
    print_stats(out, db_stats, records)


def execute_report(
    *,
    report_date: date | None = None,
    settings: Settings | None = None,
    console: Console | None = None,
) -> tuple[Path, Path]:
    """Regenerate daily YYYY-MM-DD_master.{md,html} from SQLite and rejection logs."""
    cfg = settings or Settings()
    out = console or Console()
    target_date = report_date or datetime.now(timezone.utc).date()
    db = DatabaseManager(cfg.db_path, cfg.rejected_log_path, dry_run=True)
    try:
        master_md, master_html, count = write_daily_master_reports(
            db, cfg.reports_dir, target_date
        )
    finally:
        db.close()
    out.print(
        f"[green]Daily master reports regenerated ({count} shortlisted):[/green] "
        f"{master_md} & {master_html}"
    )
    return master_md, master_html


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="job-scout",
        description="Automated IT job aggregator, filter, and LLM evaluator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Fetch offers, deduplicate, filter, evaluate via LLM, and generate reports",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and run Stage 1 filter without calling the LLM or mutating DB",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of Stage 1 survivors to evaluate with the LLM",
    )

    subparsers.add_parser(
        "stats",
        help="Display database statistics and top rejection reasons from rejected_log.jsonl",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Regenerate daily YYYY-MM-DD_master.md and YYYY-MM-DD_master.html from jobs.db",
    )
    report_parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (defaults to today UTC)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()

    if args.command == "run":
        asyncio.run(execute_run(dry_run=args.dry_run, limit=args.limit, console=console))
        return 0
    if args.command == "stats":
        execute_stats(console=console)
        return 0
    if args.command == "report":
        target_date = date.fromisoformat(args.date) if args.date else None
        execute_report(report_date=target_date, console=console)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
