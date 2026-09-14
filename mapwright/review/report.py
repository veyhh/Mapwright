"""Render an analysis as a Markdown report and a machine-readable JSON file.

The Markdown is written for a person deciding what to change next, so every
finding shows its measurement and its recommended change. The JSON carries the
same content for tooling, with no prose-only facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from mapwright.core.issues import Category, Issue, Severity, filter_category
from mapwright.design.correction import CorrectionPlan
from mapwright.review.structural import review_structural

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mapwright.pipeline import ImprovementResult, SceneAnalysis


#: Report sections, in the order the spec fixes them.
SECTION_ORDER = (
    Category.SPATIAL,
    Category.FLOW,
    Category.NAVIGATION,
    Category.PACING,
    Category.ENCOUNTER,
    Category.FAIRNESS,
    Category.VISUAL,
)

#: Analyzer reports that back each section, for the detail blocks.
SECTION_ANALYZERS = {
    Category.SPATIAL: ("repetition", "spacing", "density", "landmarks"),
    Category.NAVIGATION: ("navigation",),
    Category.FLOW: ("flow",),
    Category.PACING: ("pacing",),
    Category.ENCOUNTER: ("encounter",),
    Category.FAIRNESS: ("fairness",),
    Category.VISUAL: ("sightlines", "visual"),
}


@dataclass(frozen=True)
class Report:
    """A rendered report in both human and machine form."""

    markdown: str
    data: dict[str, Any]

    def write(self, directory: str | Path, stem: str = "level_report") -> tuple[Path, Path]:
        """Write both forms into a directory and return their paths."""
        target = Path(directory).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        markdown_path = target / f"{stem}.md"
        json_path = target / f"{stem}.json"
        markdown_path.write_text(self.markdown, encoding="utf-8")
        json_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return markdown_path, json_path


def build_report(
    analysis: SceneAnalysis,
    plan: CorrectionPlan | None = None,
    improvement: ImprovementResult | None = None,
) -> Report:
    """Render a full report for one analysis, with optional correction results."""
    structural = review_structural(
        analysis.context, analysis.issues, dict(analysis.applicable)
    )
    lines: list[str] = [
        f"# Mapwright report: {analysis.scene.scene}",
        "",
        f"**{_verdict(analysis)}**",
        "",
        *_summary_section(analysis, structural),
        "",
        *_score_section(analysis, improvement),
    ]
    for category in SECTION_ORDER:
        lines.extend(_category_section(analysis, category))
    lines.extend(_corrections_section(plan, improvement))

    data: dict[str, Any] = analysis.to_dict()
    data["verdict"] = _verdict(analysis)
    data["structural"] = structural.to_dict()
    if plan is not None:
        data["corrections"] = plan.to_dict()
    if improvement is not None:
        data["improvement"] = improvement.to_dict()
    return Report(markdown="\n".join(lines).rstrip() + "\n", data=data)


def _verdict(analysis: SceneAnalysis) -> str:
    """Return the one-line judgement that heads the report."""
    counts = {severity: 0 for severity in Severity}
    for issue in analysis.issues:
        counts[issue.severity] += 1
    score = round(analysis.overall)
    if counts[Severity.CRITICAL]:
        return f"BLOCKING ISSUES — score {score}/100, {counts[Severity.CRITICAL]} critical"
    if counts[Severity.ERROR]:
        return f"NEEDS WORK — score {score}/100, {counts[Severity.ERROR]} error-level finding(s)"
    if counts[Severity.WARNING]:
        return f"REVIEW — score {score}/100, {counts[Severity.WARNING]} warning(s)"
    return f"CLEAN — score {score}/100, no findings above informational"


def _summary_section(analysis: SceneAnalysis, structural) -> list[str]:
    """Return the Summary section lines."""
    scene = analysis.scene
    lines = [
        "## Summary",
        "",
        f"- **Scene**: `{scene.scene}` from {scene.source_engine}"
        + (f" (`{scene.source_path}`)" if scene.source_path else ""),
        f"- **Profile**: {analysis.config.profile} — {analysis.config.profile_description.strip()}",
        f"- **Objects**: {structural.object_count} "
        f"({structural.measured_count} with measurable extents)",
        f"- **Zones**: {structural.zone_count} | **Markers**: {structural.marker_count}"
        + (f" | **Teams**: {', '.join(structural.teams)}" if structural.teams else ""),
    ]
    if structural.ground_size:
        area = (
            f", {structural.walkable_area:.0f} m² walkable"
            if structural.walkable_area is not None
            else ""
        )
        lines.append(
            f"- **Playable area**: {structural.ground_size[0]:.1f} × "
            f"{structural.ground_size[1]:.1f} m{area}"
        )
    lines.append(
        f"- **Route graph**: {structural.node_count} places, {structural.edge_count} routes"
        + (
            " (topology inferred from walkable space, not declared)"
            if structural.graph_derived
            else ""
        )
    )
    counts = structural.severity_counts
    tally = ", ".join(
        f"{counts[severity]} {severity.value.lower()}"
        for severity in Severity
        if counts[severity]
    )
    lines.append(f"- **Findings**: {tally or 'none'}")
    skipped = [item for item in structural.coverage if not item.applicable]
    if skipped:
        lines.append("")
        lines.append("Checks that did not apply to this scene:")
        lines.extend(
            f"- {item.category.title}: {item.reason}" for item in skipped
        )
    if structural.unmeasured:
        lines.append("")
        lines.append(
            f"{len(structural.unmeasured)} object(s) had no usable extents and were "
            "excluded from spatial measurement: "
            + ", ".join(f"`{name}`" for name in structural.unmeasured[:8])
            + (" …" if len(structural.unmeasured) > 8 else "")
        )
    return lines


def _score_section(
    analysis: SceneAnalysis, improvement: ImprovementResult | None
) -> list[str]:
    """Return the score table, with before/after columns when available."""
    lines = ["## Score", "", "| Category | Score | Weight | Findings |", "|---|---:|---:|---:|"]
    for item in analysis.scorecard.categories:
        if not item.applicable:
            lines.append(f"| {item.category.title} | n/a | — | {item.reason} |")
            continue
        lines.append(
            f"| {item.category.title} | {item.score:.0f} | {item.weight:g} | {item.issue_count} |"
        )
    lines.append(f"| **Overall** | **{analysis.overall:.0f}** | | |")
    if improvement is not None and improvement.iterations:
        lines.extend(
            [
                "",
                "### Before and after correction",
                "",
                "```",
                improvement.to_dict()["comparison"],
                "```",
            ]
        )
    return lines


def _category_section(analysis: SceneAnalysis, category: Category) -> list[str]:
    """Return one report section: its findings and the measurements behind them."""
    issues = filter_category(analysis.issues, category)
    coverage = next(
        (item for item in analysis.scorecard.categories if item.category is category),
        None,
    )
    lines = ["", f"## {category.title}", ""]
    if coverage is not None and not coverage.applicable:
        lines.append(f"Not applicable: {coverage.reason}.")
        return lines
    if not issues:
        lines.append("No findings.")
    else:
        lines.extend(_issue_block(issues))
    details = [
        name for name in SECTION_ANALYZERS.get(category, ()) if name in analysis.reports
    ]
    if details:
        lines.extend(["", "<details><summary>Measurements</summary>", "", "```"])
        for name in details:
            lines.append(analysis.format_section(name))
            lines.append("")
        lines.extend(["```", "", "</details>"])
    return lines


def _issue_block(issues: Sequence[Issue]) -> list[str]:
    """Return one Markdown block per finding, evidence first."""
    lines: list[str] = []
    for issue in issues:
        heading = f"**{issue.severity.value}** · `{issue.code}`"
        if issue.zone:
            heading += f" · zone `{issue.zone}`"
        if issue.view:
            heading += f" · view `{issue.view}`"
        if issue.advisory:
            heading += " · _review note_"
        lines.extend([heading, "", issue.evidence])
        if issue.explanation:
            lines.extend(["", issue.explanation])
        lines.extend(["", f"*Recommendation:* {issue.recommendation}", ""])
    return lines


def _corrections_section(
    plan: CorrectionPlan | None, improvement: ImprovementResult | None
) -> list[str]:
    """Return the Corrections section, covering both proposed and applied edits."""
    lines = ["", "## Corrections", ""]
    if plan is None and improvement is None:
        lines.append("No correction pass was run.")
        return lines
    applied = [
        item for iteration in (improvement.iterations if improvement else ()) for item in iteration.applied
    ]
    if applied:
        lines.extend(["### Applied", ""])
        lines.extend(f"- {item.correction.describe()}" for item in applied)
        lines.append("")
    source = plan if plan is not None else (
        improvement.iterations[-1].plan if improvement and improvement.iterations else None
    )
    if source is not None and source.manual:
        lines.extend(["### Needs a design decision", ""])
        lines.extend(
            f"- `{item.issue_code}` — {item.describe()}" for item in source.manual
        )
        lines.append("")
    if source is not None and source.skipped:
        lines.extend(["### No correction proposed", ""])
        lines.extend(f"- `{code}`: {reason}" for code, reason in source.skipped)
        lines.append("")
    if not applied and (source is None or not source.corrections):
        lines.append("Nothing to correct.")
    return lines
