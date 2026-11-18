"""Terminal Rich output plus Markdown and standalone HTML report generators."""

from __future__ import annotations

import html
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from job_scout.database import DatabaseManager
from job_scout.models import EvaluatedOffer, JobOffer, RejectionRecord


class RunMetrics(BaseModel):
    """Pipeline execution counters for terminal and file reports."""

    fetched: int = 0
    duplicates_skipped: int = 0
    rule_rejected: int = 0
    llm_rejected: int = 0
    shortlisted: int = 0
    dry_run: bool = False


_CITY_ALIASES = {
    "warsaw": "Warszawa",
    "warzawa": "Warszawa",
    "warszawa": "Warszawa",
    "krakow": "Kraków",
    "cracow": "Kraków",
    "kraków": "Kraków",
    "wroclaw": "Wrocław",
    "wrocław": "Wrocław",
    "gdansk": "Gdańsk",
    "gdańsk": "Gdańsk",
    "lodz": "Łódź",
    "łódź": "Łódź",
    "poznan": "Poznań",
    "poznań": "Poznań",
}


def _normalize_offer_cities(offer: JobOffer) -> list[str]:
    """Normalize offer cities across spelling variants while preserving actual locations."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_loc in offer.locations:
        cleaned = raw_loc.strip()
        if not cleaned:
            continue
        canonical = _CITY_ALIASES.get(cleaned.lower(), cleaned)
        low = canonical.lower()
        if low not in seen:
            seen.add(low)
            normalized.append(canonical)
    return normalized or ["Nie podano"]


def _offer_published_iso(offer: JobOffer, report_date: date) -> tuple[str, str]:
    """Return (ISO timestamp for sorting, YYYY-MM-DD label) for freshness sorting."""
    if offer.published_at and offer.published_at.strip():
        iso = offer.published_at.strip()
        return iso, iso[:10]
    fallback = f"{report_date.isoformat()}T00:00:00Z"
    return fallback, report_date.isoformat()


def _format_salary(offer: JobOffer) -> str:
    if offer.salary_min_pln is None and offer.salary_max_pln is None:
        return "Nieujawnione"
    lo = f"{offer.salary_min_pln:,}".replace(",", " ") if offer.salary_min_pln is not None else "?"
    hi = f"{offer.salary_max_pln:,}".replace(",", " ") if offer.salary_max_pln is not None else "?"
    return f"{lo} – {hi} PLN"


def print_run_summary(
    console: Console,
    metrics: RunMetrics,
    shortlisted: list[EvaluatedOffer],
    survivors_preview: list[JobOffer] | None = None,
) -> None:
    """Render summary metrics and shortlisted offers (or dry-run survivors) to terminal."""
    mode_label = " [yellow](DRY-RUN)[/yellow]" if metrics.dry_run else ""
    summary_table = Table(title=f"Job-Scout Run Summary{mode_label}", show_header=True)
    summary_table.add_column("Metric", style="bold cyan")
    summary_table.add_column("Count", justify="right", style="bold white")

    summary_table.add_row("Fetched", str(metrics.fetched))
    summary_table.add_row("Duplicates Skipped", str(metrics.duplicates_skipped))
    summary_table.add_row("Rule-Rejected (Stage 1)", str(metrics.rule_rejected))
    summary_table.add_row("LLM-Rejected (Stage 2)", str(metrics.llm_rejected))
    summary_table.add_row("Shortlisted", f"[green]{metrics.shortlisted}[/green]")
    console.print(summary_table)

    sorted_shortlist = sorted(
        shortlisted, key=lambda item: item.evaluation.match_score, reverse=True
    )
    if sorted_shortlist:
        offers_table = Table(
            title="Shortlisted Offers (Stage 2 Recommendations)",
            show_lines=True,
        )
        offers_table.add_column("Score", justify="center", style="bold green", width=7)
        offers_table.add_column("Role & Company", style="bold")
        offers_table.add_column("Mode / Salary", style="cyan")
        offers_table.add_column("Podsumowanie & Red Flags & CV Tips")

        for item in sorted_shortlist:
            offer = item.offer
            ev = item.evaluation
            locs = ", ".join(offer.locations) if offer.locations else "Remote"
            mode_sal = f"{offer.workplace_type} ({locs})\n{_format_salary(offer)}"
            flags = "\n".join(f"• {f}" for f in ev.red_flags) if ev.red_flags else "Brak"
            tips = "\n".join(f"• {t}" for t in ev.cv_focus_tips) if ev.cv_focus_tips else "Brak"
            details = (
                f"[bold]{ev.one_line_pitch}[/bold]\n"
                f"[red]Red flags:[/red]\n{flags}\n"
                f"[blue]CV tips:[/blue]\n{tips}\n"
                f"[dim]{offer.url}[/dim]"
            )
            offers_table.add_row(
                f"{ev.match_score}/10",
                f"{offer.title}\n[dim]{offer.company} ({offer.source})[/dim]",
                mode_sal,
                details,
            )
        console.print(offers_table)
    elif metrics.dry_run and survivors_preview:
        preview_table = Table(
            title=f"Stage 1 Survivors Preview (showing up to {min(15, len(survivors_preview))} of {len(survivors_preview)})",
            show_lines=True,
        )
        preview_table.add_column("Portal", style="magenta")
        preview_table.add_column("Role & Company", style="bold")
        preview_table.add_column("Mode / Location", style="cyan")
        preview_table.add_column("Salary (PLN)")
        for offer in survivors_preview[:15]:
            locs = ", ".join(offer.locations) if offer.locations else "-"
            preview_table.add_row(
                offer.source,
                f"{offer.title}\n[dim]{offer.company}[/dim]",
                f"{offer.workplace_type} ({locs})",
                _format_salary(offer),
            )
        console.print(preview_table)
    elif not metrics.dry_run:
        console.print(Panel("Brak rekomendowanych ofert w tym przebiegu.", style="yellow"))


def print_stats(
    console: Console,
    db_stats: dict[str, Any],
    rejection_records: list[RejectionRecord],
) -> None:
    """Render database statistics and top calibration rejection reasons."""
    db_table = Table(title="Job-Scout Database Statistics", show_header=True)
    db_table.add_column("Category", style="bold cyan")
    db_table.add_column("Value", justify="right", style="bold white")

    db_table.add_row("Total Stored Offers", str(db_stats.get("total", 0)))
    by_status = db_stats.get("by_status", {})
    db_table.add_row("Shortlisted", f"[green]{by_status.get('shortlisted', 0)}[/green]")
    db_table.add_row("Rule-Rejected", f"[yellow]{by_status.get('rejected_rule', 0)}[/yellow]")
    db_table.add_row("LLM-Rejected", f"[red]{by_status.get('rejected_llm', 0)}[/red]")

    avg_score = db_stats.get("avg_shortlisted_score")
    db_table.add_row(
        "Avg Shortlisted Score",
        f"{avg_score}/10" if avg_score is not None else "-",
    )
    for src, cnt in sorted(db_stats.get("by_source", {}).items()):
        db_table.add_row(f"Source: {src}", str(cnt))
    console.print(db_table)

    top_reasons = DatabaseManager.summarize_rejection_reasons(rejection_records, top_n=15)
    rej_table = Table(title="Top Rejection Reasons (rejected_log.jsonl)", show_lines=True)
    rej_table.add_column("Stage", style="magenta", width=8)
    rej_table.add_column("Rejection Reason")
    rej_table.add_column("Count", justify="right", style="bold", width=7)

    if top_reasons:
        for stage, reason, count in top_reasons:
            rej_table.add_row(stage, reason, str(count))
    else:
        rej_table.add_row("-", "Brak zapisanych odrzuceń.", "0")
    console.print(rej_table)


def generate_markdown_report(
    report_date: date,
    metrics: RunMetrics,
    shortlisted: list[EvaluatedOffer],
    llm_rejected: list[RejectionRecord],
    rule_rejected: list[RejectionRecord],
    output_path: Path,
    *,
    write_step_summary: bool = True,
) -> str:
    """Create the daily Markdown report and write to GITHUB_STEP_SUMMARY if set."""
    sorted_shortlist = sorted(
        shortlisted, key=lambda item: item.evaluation.match_score, reverse=True
    )
    lines: list[str] = [
        f"# Job-Scout Raport Dzienny — {report_date.isoformat()}",
        "",
        "| Metryka | Liczba |",
        "| --- | ---: |",
        f"| Pobrane oferty | {metrics.fetched} |",
        f"| Pominięte duplikaty | {metrics.duplicates_skipped} |",
        f"| Odrzucone przez reguły (Stage 1) | {metrics.rule_rejected} |",
        f"| Odrzucone przez LLM (Stage 2) | {metrics.llm_rejected} |",
        f"| Rekomendowane (Shortlist) | {metrics.shortlisted} |",
        "",
        "## Rekomendowane oferty",
        "",
    ]

    if not sorted_shortlist:
        lines.append("_Brak ofert spełniających próg rekomendacji w tym przebiegu._")
        lines.append("")
    else:
        for item in sorted_shortlist:
            offer = item.offer
            ev = item.evaluation
            locs = ", ".join(offer.locations) if offer.locations else "Zdalnie"
            salary_str = _format_salary(offer)
            lines.extend(
                [
                    f"### [{offer.title} — {offer.company}]({offer.url})",
                    f"- **Ocena:** `{ev.match_score}/10` | **Portal:** `{offer.source}` | **Tryb:** `{offer.workplace_type}` ({locs}) | **Widełki:** {salary_str}",
                    f"- **Podsumowanie:** {ev.one_line_pitch}",
                ]
            )
            if ev.red_flags:
                lines.append("- **Red flags:**")
                for flag in ev.red_flags:
                    lines.append(f"  - {flag}")
            else:
                lines.append("- **Red flags:** Brak istotnych ryzyk")

            if ev.cv_focus_tips:
                lines.append("- **Wskazówki pod CV:**")
                for tip in ev.cv_focus_tips:
                    lines.append(f"  - {tip}")
            lines.append("")

    lines.extend(["## Podsumowanie odrzuconych ofert (Kalibracja)", ""])

    lines.append("### Odrzucone przez LLM (Stage 2)")
    lines.append("")
    if not llm_rejected:
        lines.append("_Brak odrzuceń na etapie oceny LLM._")
    else:
        for rec in llm_rejected:
            score_txt = f"{rec.match_score}/10" if rec.match_score is not None else "-"
            lines.append(
                f"- **[{rec.title} — {rec.company}]({rec.url})** (`{rec.source}`, ocena `{score_txt}`): {rec.rejection_reason}"
            )
    lines.append("")

    lines.append("### Odrzucone przez reguły (Stage 1)")
    lines.append("")
    if not rule_rejected:
        lines.append("_Brak odrzuceń przez reguły w tym przebiegu._")
    else:
        reason_counts = Counter(rec.rejection_reason for rec in rule_rejected)
        lines.append("| Powód odrzucenia | Liczba |")
        lines.append("| --- | ---: |")
        for reason, cnt in reason_counts.most_common():
            lines.append(f"| {reason} | {cnt} |")
        lines.append("")
        lines.append("**Lista odrzuconych ofert (do audytu false negatives):**")
        for rec in rule_rejected:
            lines.append(
                f"- [{rec.title} — {rec.company}]({rec.url}) (`{rec.source}`): {rec.rejection_reason}"
            )
    lines.append("")

    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    if write_step_summary:
        step_summary_env = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
        if step_summary_env:
            summary_path = Path(step_summary_env)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            with summary_path.open("a", encoding="utf-8") as fh:
                fh.write(content + "\n")

    return content


def generate_html_report(
    report_date: date,
    metrics: RunMetrics,
    shortlisted: list[EvaluatedOffer],
    llm_rejected: list[RejectionRecord],
    rule_rejected: list[RejectionRecord],
    output_path: Path,
) -> str:
    """Create a standalone responsive HTML report with interactive filters and sorting."""
    sorted_shortlist = sorted(
        shortlisted,
        key=lambda item: (
            item.evaluation.match_score,
            _offer_published_iso(item.offer, report_date)[0],
        ),
        reverse=True,
    )

    all_cities_set: set[str] = set()
    cards_html: list[str] = []
    if not sorted_shortlist:
        cards_html.append(
            '<div class="empty-state">Brak rekomendowanych ofert spełniających próg dopasowania w tym przebiegu.</div>'
        )
    else:
        for idx, item in enumerate(sorted_shortlist):
            offer = item.offer
            ev = item.evaluation
            score_cls = "score-high" if ev.match_score >= 8 else "score-mid"
            norm_cities = _normalize_offer_cities(offer)
            for c in norm_cities:
                if c != "Nie podano":
                    all_cities_set.add(c)
            locs = html.escape(", ".join(norm_cities))
            pub_iso, pub_date_str = _offer_published_iso(offer, report_date)
            salary = html.escape(_format_salary(offer))
            flags_html = (
                "".join(f'<span class="badge badge-flag">{html.escape(f)}</span>' for f in ev.red_flags)
                if ev.red_flags
                else '<span class="badge badge-ok">Brak red flags</span>'
            )
            tips_html = "".join(f"<li>{html.escape(t)}</li>" for t in ev.cv_focus_tips)

            cards_html.append(
                f"""
                <article class="offer-card"
                         data-score="{ev.match_score}"
                         data-workplace="{html.escape(offer.workplace_type)}"
                         data-cities="{html.escape('|'.join(norm_cities))}"
                         data-published="{html.escape(pub_iso)}"
                         data-order="{idx}">
                  <div class="card-header">
                    <div>
                      <a class="offer-title" href="{html.escape(offer.url)}" target="_blank" rel="noopener noreferrer">{html.escape(offer.title)}</a>
                      <div class="offer-meta">{html.escape(offer.company)} &middot; {html.escape(offer.source)} &middot; {html.escape(offer.workplace_type)} ({locs}) &middot; <strong>{salary}</strong> &middot; Dodano: {html.escape(pub_date_str)}</div>
                    </div>
                    <span class="score-badge {score_cls}">{ev.match_score}/10</span>
                  </div>
                  <p class="pitch">{html.escape(ev.one_line_pitch)}</p>
                  <div class="flags-row">{flags_html}</div>
                  {f'<div class="tips"><strong>Wskazówki pod CV:</strong><ul>{tips_html}</ul></div>' if tips_html else ''}
                </article>
                """
            )

    sorted_cities = sorted(all_cities_set, key=lambda c: (0 if c == "Warszawa" else 1, c.lower()))
    city_options_html = "".join(
        f'<option value="{html.escape(city)}">{html.escape(city)}</option>'
        for city in sorted_cities
    )

    llm_rej_items = "".join(
        f'<li><a href="{html.escape(r.url)}" target="_blank" rel="noopener noreferrer">{html.escape(r.title)} — {html.escape(r.company)}</a> '
        f'<code>{html.escape(r.source)}</code> (ocena: <strong>{r.match_score if r.match_score is not None else "-"}/10</strong>): '
        f'{html.escape(r.rejection_reason)}</li>'
        for r in llm_rejected
    ) or "<li>Brak odrzuceń LLM.</li>"

    rule_rej_items = "".join(
        f'<li><a href="{html.escape(r.url)}" target="_blank" rel="noopener noreferrer">{html.escape(r.title)} — {html.escape(r.company)}</a> '
        f'<code>{html.escape(r.source)}</code>: {html.escape(r.rejection_reason)}</li>'
        for r in rule_rejected
    ) or "<li>Brak odrzuceń przez reguły.</li>"

    total_shortlist = len(sorted_shortlist)
    html_doc = f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Job-Scout Raport — {report_date.isoformat()}</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --surface: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --border: #e2e8f0;
      --accent: #2563eb;
      --ok-bg: #dcfce7;
      --ok-fg: #166534;
      --warn-bg: #fef3c7;
      --warn-fg: #92400e;
      --flag-bg: #fee2e2;
      --flag-fg: #991b1b;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0b1120;
        --surface: #111827;
        --text: #f1f5f9;
        --muted: #94a3b8;
        --border: #1e293b;
        --accent: #60a5fa;
        --ok-bg: #052e16;
        --ok-fg: #86efac;
        --warn-bg: #451a03;
        --warn-fg: #fcd34d;
        --flag-bg: #450a0a;
        --flag-fg: #fca5a5;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 2rem 1rem;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    header {{ margin-bottom: 1.5rem; }}
    h1 {{ margin: 0 0 0.25rem 0; font-size: 1.75rem; }}
    .subtitle {{ color: var(--muted); font-size: 0.95rem; }}
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 0.75rem;
      margin-bottom: 2rem;
    }}
    .metric-box {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.9rem 1rem;
    }}
    .metric-val {{ font-size: 1.5rem; font-weight: 700; }}
    .metric-lbl {{ font-size: 0.8rem; color: var(--muted); }}
    .controls-bar {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem;
      margin-bottom: 1.25rem;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
      gap: 0.75rem;
      align-items: end;
    }}
    .control-group {{ display: flex; flex-direction: column; gap: 0.3rem; }}
    .control-group label {{ font-size: 0.78rem; font-weight: 600; color: var(--muted); }}
    .control-group select, .reset-btn {{
      padding: 0.45rem 0.65rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 0.9rem;
    }}
    .control-actions {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.5rem;
      grid-column: 1 / -1;
      padding-top: 0.25rem;
      border-top: 1px solid var(--border);
      font-size: 0.88rem;
      color: var(--muted);
    }}
    .reset-btn {{ cursor: pointer; font-weight: 600; }}
    .reset-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
    .offer-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.25rem;
      margin-bottom: 1rem;
    }}
    .offer-card.is-hidden {{ display: none; }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
    }}
    .offer-title {{
      font-size: 1.15rem;
      font-weight: 700;
      color: var(--accent);
      text-decoration: none;
    }}
    .offer-title:hover {{ text-decoration: underline; }}
    .offer-meta {{ font-size: 0.88rem; color: var(--muted); margin-top: 0.2rem; }}
    .score-badge {{
      padding: 0.3rem 0.7rem;
      border-radius: 999px;
      font-weight: 700;
      font-size: 0.9rem;
      white-space: nowrap;
    }}
    .score-high {{ background: var(--ok-bg); color: var(--ok-fg); }}
    .score-mid {{ background: var(--warn-bg); color: var(--warn-fg); }}
    .pitch {{ margin: 0.8rem 0; }}
    .flags-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.75rem; }}
    .badge {{
      font-size: 0.78rem;
      padding: 0.2rem 0.55rem;
      border-radius: 6px;
      font-weight: 600;
    }}
    .badge-flag {{ background: var(--flag-bg); color: var(--flag-fg); }}
    .badge-ok {{ background: var(--ok-bg); color: var(--ok-fg); }}
    .tips ul {{ margin: 0.35rem 0 0 1.2rem; padding: 0; }}
    .empty-state {{
      background: var(--surface);
      border: 1px dashed var(--border);
      padding: 1.5rem;
      border-radius: 10px;
      color: var(--muted);
    }}
    .is-hidden {{ display: none; }}
    details.calibration {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem 1.25rem;
      margin-top: 2rem;
    }}
    details.calibration summary {{
      cursor: pointer;
      font-weight: 700;
      font-size: 1.05rem;
    }}
    details.calibration ul {{ padding-left: 1.25rem; }}
    details.calibration a {{ color: var(--accent); }}
    code {{
      background: var(--border);
      padding: 0.1rem 0.35rem;
      border-radius: 4px;
      font-size: 0.82rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Job-Scout — Raport Dzienny ({report_date.isoformat()})</h1>
      <div class="subtitle">SOLID.Jobs &amp; Just Join IT &middot; Dwustopniowe filtrowanie i ocena dopasowania</div>
    </header>

    <section class="metrics-grid">
      <div class="metric-box"><div class="metric-val">{metrics.fetched}</div><div class="metric-lbl">Pobrane oferty</div></div>
      <div class="metric-box"><div class="metric-val">{metrics.duplicates_skipped}</div><div class="metric-lbl">Duplikaty</div></div>
      <div class="metric-box"><div class="metric-val">{metrics.rule_rejected}</div><div class="metric-lbl">Odrzucone (Reguły)</div></div>
      <div class="metric-box"><div class="metric-val">{metrics.llm_rejected}</div><div class="metric-lbl">Odrzucone (LLM)</div></div>
      <div class="metric-box"><div class="metric-val">{metrics.shortlisted}</div><div class="metric-lbl">Rekomendowane</div></div>
    </section>

    <section>
      <h2>Rekomendowane oferty</h2>
      <div class="controls-bar" id="controls-bar">
        <div class="control-group">
          <label for="filter-score">Poziom dopasowania</label>
          <select id="filter-score">
            <option value="all">Wszystkie</option>
            <option value="min:10">10/10</option>
            <option value="min:9">&ge; 9/10</option>
            <option value="min:8">&ge; 8/10</option>
            <option value="min:7">&ge; 7/10</option>
            <option value="min:6">&ge; 6/10</option>
            <option value="min:5">&ge; 5/10</option>
            <option value="eq:9">= 9/10</option>
            <option value="eq:8">= 8/10</option>
            <option value="eq:7">= 7/10</option>
            <option value="eq:6">= 6/10</option>
            <option value="eq:5">= 5/10</option>
          </select>
        </div>
        <div class="control-group">
          <label for="filter-workplace">Tryb pracy</label>
          <select id="filter-workplace">
            <option value="all">Wszystkie</option>
            <option value="remote">remote</option>
            <option value="hybrid">hybrid</option>
            <option value="onsite">onsite</option>
          </select>
        </div>
        <div class="control-group">
          <label for="filter-city">Miasto</label>
          <select id="filter-city">
            <option value="all">Wszystkie</option>
            {city_options_html}
          </select>
        </div>
        <div class="control-group">
          <label for="sort-by">Sortowanie</label>
          <select id="sort-by">
            <option value="score_desc">Poziom dopasowania (najwyższy)</option>
            <option value="fresh_desc">Najnowsze (freshest first)</option>
          </select>
        </div>
        <div class="control-actions">
          <span id="visible-count">Wyświetlane: {total_shortlist} / {total_shortlist}</span>
          <button type="button" class="reset-btn" id="reset-filters">Resetuj filtry</button>
        </div>
      </div>

      <div id="offers-list">
        {"".join(cards_html)}
      </div>
      <div class="empty-state is-hidden" id="no-matches-msg">
        Brak ofert spełniających wybrane filtry.
      </div>
    </section>

    <details class="calibration">
      <summary>Podsumowanie odrzuconych ofert — Kalibracja (LLM: {len(llm_rejected)}, Reguły: {len(rule_rejected)})</summary>
      <h3>Odrzucone przez LLM (Stage 2)</h3>
      <ul>{llm_rej_items}</ul>
      <h3>Odrzucone przez reguły (Stage 1)</h3>
      <ul>{rule_rej_items}</ul>
    </details>
  </div>

  <script>
    (function () {{
      const scoreEl = document.getElementById("filter-score");
      const wpEl = document.getElementById("filter-workplace");
      const cityEl = document.getElementById("filter-city");
      const sortEl = document.getElementById("sort-by");
      const resetBtn = document.getElementById("reset-filters");
      const listEl = document.getElementById("offers-list");
      const countEl = document.getElementById("visible-count");
      const emptyEl = document.getElementById("no-matches-msg");
      const cards = Array.from(listEl.querySelectorAll(".offer-card"));
      const total = cards.length;

      function parsePublishedTs(card) {{
        const ts = Date.parse(card.dataset.published || "");
        return Number.isNaN(ts) ? 0 : ts;
      }}

      function matchesScore(score, filterVal) {{
        if (filterVal === "all") return true;
        const [op, rawNum] = filterVal.split(":");
        const target = Number(rawNum);
        if (op === "min") return score >= target;
        if (op === "eq") return score === target;
        return true;
      }}

      function applyFiltersAndSort() {{
        const scoreVal = scoreEl.value;
        const wpVal = wpEl.value;
        const cityVal = cityEl.value;
        const sortVal = sortEl.value;

        const sorted = cards.slice().sort((a, b) => {{
          const scoreA = Number(a.dataset.score || 0);
          const scoreB = Number(b.dataset.score || 0);
          const tsA = parsePublishedTs(a);
          const tsB = parsePublishedTs(b);
          if (sortVal === "fresh_desc") {{
            if (tsB !== tsA) return tsB - tsA;
            if (scoreB !== scoreA) return scoreB - scoreA;
          }} else {{
            if (scoreB !== scoreA) return scoreB - scoreA;
            if (tsB !== tsA) return tsB - tsA;
          }}
          return Number(a.dataset.order || 0) - Number(b.dataset.order || 0);
        }});

        let visible = 0;
        for (const card of sorted) {{
          const score = Number(card.dataset.score || 0);
          const wp = card.dataset.workplace || "";
          const cities = (card.dataset.cities || "").split("|").filter(Boolean);
          const show =
            matchesScore(score, scoreVal) &&
            (wpVal === "all" || wp === wpVal) &&
            (cityVal === "all" || cities.includes(cityVal));
          card.classList.toggle("is-hidden", !show);
          if (show) visible += 1;
          listEl.appendChild(card);
        }}

        if (countEl) {{
          countEl.textContent = "Wyświetlane: " + visible + " / " + total;
        }}
        if (emptyEl) {{
          emptyEl.classList.toggle("is-hidden", !(total > 0 && visible === 0));
        }}
      }}

      [scoreEl, wpEl, cityEl, sortEl].forEach((el) => {{
        if (el) el.addEventListener("change", applyFiltersAndSort);
      }});
      if (resetBtn) {{
        resetBtn.addEventListener("click", () => {{
          scoreEl.value = "all";
          wpEl.value = "all";
          cityEl.value = "all";
          sortEl.value = "score_desc";
          applyFiltersAndSort();
        }});
      }}
      applyFiltersAndSort();
    }})();
  </script>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_doc, encoding="utf-8")
    return html_doc
