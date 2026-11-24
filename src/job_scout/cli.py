"""CLI entrypoint for job-scout (run, dry-run, stats, report)."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from rich.console import Console

from job_scout.config import Settings, load_cv, load_profile
from job_scout.database import DatabaseManager
from job_scout.evaluator import LLMEvaluator
from job_scout.filter import Stage1Filter
from job_scout.models import (
    EvaluatedOffer,
    JobEvaluation,
    JobOffer,
    RejectionRecord,
    SourceRunStats,
)
from job_scout.reporter import (
    RunMetrics,
    generate_html_report,
    generate_markdown_report,
    print_run_summary,
    print_stats,
)
from job_scout.scrapers.base import BaseScraper
from job_scout.scrapers.registry import get_scraper_registry, list_sources, resolve_source_name

logger = logging.getLogger(__name__)


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


def _aggregate_master_source_stats(
    shortlisted: list[EvaluatedOffer],
    llm_rej: list[RejectionRecord],
    rule_rej: list[RejectionRecord],
) -> list[SourceRunStats]:
    """Build per-portal stats from stored daily shortlisted and rejection records."""
    by_source: OrderedDict[str, SourceRunStats] = OrderedDict()

    def _get(src: str) -> SourceRunStats:
        if src not in by_source:
            by_source[src] = SourceRunStats(source=src, status="OK")
        return by_source[src]

    for item in shortlisted:
        st = _get(item.offer.source)
        st.fetched += 1
        st.passed_prefilter += 1
        st.llm_evaluated += 1
        st.shortlisted += 1
    for rec in llm_rej:
        st = _get(rec.source)
        st.fetched += 1
        st.passed_prefilter += 1
        st.llm_evaluated += 1
        st.llm_rejected += 1
    for rec in rule_rej:
        st = _get(rec.source)
        st.fetched += 1
        st.rule_rejected += 1

    return list(by_source.values())


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
    source_stats = _aggregate_master_source_stats(shortlisted, llm_rej, rule_rej)
    metrics = RunMetrics(
        fetched=len(shortlisted) + len(llm_rej) + len(rule_rej),
        duplicates_skipped=0,
        rule_rejected=len(rule_rej),
        llm_rejected=len(llm_rej),
        shortlisted=len(shortlisted),
        source_stats=source_stats,
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


def _parse_requested_sources(
    raw_sources: Sequence[str] | None,
    registry: dict[str, type[BaseScraper]],
) -> list[str] | None:
    """Resolve and deduplicate CLI --source flags (supports repeated or comma-separated values)."""
    if not raw_sources:
        return None
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in raw_sources:
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            canonical = resolve_source_name(token, registry=registry)
            if canonical not in seen:
                seen.add(canonical)
                resolved.append(canonical)
    return resolved or None


async def execute_run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    sources: Sequence[str] | None = None,
    settings: Settings | None = None,
    console: Console | None = None,
    scraper_registry: dict[str, type[BaseScraper]] | None = None,
) -> RunMetrics:
    """Run the full job-scout pipeline: fetch -> dedup -> filter -> evaluate -> report."""
    cfg = settings or Settings()
    out = console or Console()
    profile = load_profile(cfg.profile_path)
    base_cv = ""
    if not dry_run:
        base_cv = load_cv(cfg.cv_path)
        cfg.require_api_key()
    elif cfg.cv_path.exists():
        base_cv = load_cv(cfg.cv_path)

    registry = scraper_registry if scraper_registry is not None else get_scraper_registry()
    requested_sources = _parse_requested_sources(sources, registry)
    target_source_names = requested_sources if requested_sources is not None else list(registry.keys())

    db = DatabaseManager(cfg.db_path, cfg.rejected_log_path, dry_run=dry_run)
    stage1 = Stage1Filter(profile)
    metrics = RunMetrics(dry_run=dry_run)

    seen_fingerprints: set[str] = set()
    survivors: list[JobOffer] = []
    sample_offers: list[JobOffer] = []
    rule_rejected_records: list[RejectionRecord] = []
    llm_rejected_records: list[RejectionRecord] = []
    shortlisted: list[EvaluatedOffer] = []
    source_stats_map: OrderedDict[str, SourceRunStats] = OrderedDict()

    try:
        # 1. Fail-soft fetch across enabled/selected sources
        for source_name in target_source_names:
            scraper_cls = registry[source_name]
            src_cfg = profile.get_source_config(
                source_name,
                aliases=getattr(scraper_cls, "aliases", ()),
                default_max_pages=scraper_cls.default_max_pages,
                default_request_delay=scraper_cls.default_request_delay,
                default_timeout=scraper_cls.default_timeout,
            )
            stat = SourceRunStats(source=source_name)
            source_stats_map[source_name] = stat

            if requested_sources is None and not src_cfg.enabled:
                stat.status = "Wyłączony"
                continue

            max_details = src_cfg.max_detail_fetches
            if limit is not None:
                budget = max(0, limit - len(survivors)) * 5
                max_details = budget if max_details is None else min(max_details, budget)

            fetched_offers: list[JobOffer] = []
            try:
                async with scraper_cls(
                    request_delay=src_cfg.request_delay,
                    max_pages=src_cfg.max_pages,
                ) as scraper:
                    fetched_offers = await asyncio.wait_for(
                        scraper.fetch_offers(
                            profile=profile,
                            stage1_filter=stage1,
                            db=db,
                            seen_fingerprints=seen_fingerprints,
                            max_pages=src_cfg.max_pages,
                            max_detail_fetches=max_details,
                        ),
                        timeout=src_cfg.timeout,
                    )
                    stat.fetched = max(scraper.last_total_fetched, len(fetched_offers))
                    stat.duplicates_skipped += scraper.last_duplicates_skipped
                    metrics.fetched += stat.fetched
                    metrics.duplicates_skipped += scraper.last_duplicates_skipped
            except asyncio.TimeoutError:
                err_msg = f"Timeout po {src_cfg.timeout:.0f}s"
                stat.status = "BŁĄD"
                stat.error = err_msg
                logger.error("Scraper '%s' timed out: %s", source_name, err_msg)
                out.print(f"[yellow]Warning: {source_name} fetch failed: {err_msg}[/yellow]")
                continue
            except Exception as exc:
                err_msg = str(exc).strip() or type(exc).__name__
                if len(err_msg) > 160:
                    err_msg = err_msg[:157] + "..."
                stat.status = "BŁĄD"
                stat.error = err_msg
                logger.exception("Scraper '%s' failed: %s", source_name, err_msg)
                out.print(f"[yellow]Warning: {source_name} fetch failed: {err_msg}[/yellow]")
                continue

            for offer in fetched_offers:
                if len(sample_offers) < 10:
                    sample_offers.append(offer)

                is_dup, _ = db.is_duplicate(offer, seen_fingerprints=seen_fingerprints)
                if is_dup:
                    stat.duplicates_skipped += 1
                    metrics.duplicates_skipped += 1
                    continue
                seen_fingerprints.add(offer.compute_fingerprint())

                reason = offer.metadata_rejected_reason or stage1.filter_offer(offer)
                if reason is not None:
                    stat.rule_rejected += 1
                    metrics.rule_rejected += 1
                    rule_rejected_records.append(db.record_rule_rejection(offer, reason))
                else:
                    stat.passed_prefilter += 1
                    survivors.append(offer)

        # 2. Explicit pre-LLM cross-portal deduplication guard
        deduped_survivors: list[JobOffer] = []
        llm_stage_fps: set[str] = set()
        for offer in survivors:
            fp = offer.compute_fingerprint()
            if fp in llm_stage_fps:
                metrics.duplicates_skipped += 1
                if offer.source in source_stats_map:
                    source_stats_map[offer.source].duplicates_skipped += 1
                    source_stats_map[offer.source].passed_prefilter = max(
                        0, source_stats_map[offer.source].passed_prefilter - 1
                    )
                continue
            llm_stage_fps.add(fp)
            deduped_survivors.append(offer)
        survivors = deduped_survivors

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
                if offer.source in source_stats_map:
                    source_stats_map[offer.source].llm_evaluated += 1
                rej_record = db.record_llm_result(offer, evaluation)
                if rej_record is not None:
                    metrics.llm_rejected += 1
                    if offer.source in source_stats_map:
                        source_stats_map[offer.source].llm_rejected += 1
                    llm_rejected_records.append(rej_record)
                else:
                    metrics.shortlisted += 1
                    if offer.source in source_stats_map:
                        source_stats_map[offer.source].shortlisted += 1
                    shortlisted.append(EvaluatedOffer(offer=offer, evaluation=evaluation))

        metrics.source_stats = list(source_stats_map.values())

        # 4. Reporting
        print_run_summary(
            out,
            metrics,
            shortlisted,
            survivors_preview=survivors if dry_run else None,
            sample_offers_preview=sample_offers if dry_run else None,
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
        help="Fetch, pre-filter, and deduplicate without writing to SQLite or calling the LLM",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of Stage 1 survivors to evaluate with the LLM",
    )
    run_parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        default=None,
        help=(
            "Run only selected portal scraper(s); repeatable or comma-separated. "
            f"Available: {', '.join(list_sources())}"
        ),
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
        try:
            asyncio.run(
                execute_run(
                    dry_run=args.dry_run,
                    limit=args.limit,
                    sources=args.sources,
                    console=console,
                )
            )
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            return 2
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
