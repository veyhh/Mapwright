"""Asset repetition: how much of a level is one asset placed over and over.

Repetition is measured as each asset's share of all placements, because the
absolute count of a prop says nothing without the size of the level it sits in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.core.scene_ir import SceneObject


@dataclass(frozen=True)
class AssetUsage:
    """One asset family and the share of the scene it occupies."""

    key: str
    label: str
    asset: str | None
    count: int
    percentage: float
    flagged: bool
    subjects: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "key": self.key,
            "label": self.label,
            "asset": self.asset,
            "count": self.count,
            "percentage": self.percentage,
            "flagged": self.flagged,
            "subjects": list(self.subjects),
        }


@dataclass(frozen=True)
class RepetitionReport:
    """Per-asset placement shares for one scene."""

    issues: tuple[Issue, ...]
    scene: str
    threshold: float
    total_placements: int
    assets: tuple[AssetUsage, ...]

    @property
    def flagged_assets(self) -> tuple[AssetUsage, ...]:
        """Return the assets whose share exceeds the repetition line."""
        return tuple(asset for asset in self.assets if asset.flagged)

    @property
    def distinct_assets(self) -> int:
        """Return how many distinct asset families the scene places."""
        return len(self.assets)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "threshold": self.threshold,
            "total_placements": self.total_placements,
            "distinct_assets": self.distinct_assets,
            "assets": [asset.to_dict() for asset in self.assets],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_repetition(context: AnalysisContext) -> RepetitionReport:
    """Measure each asset's share of all placements and flag overuse."""
    scene = context.scene
    threshold = context.config.thresholds.repetition_percent
    placements = scene.props
    total = len(placements)

    groups: dict[tuple[str, str], list[SceneObject]] = {}
    for obj in placements:
        key = ("asset", obj.asset) if obj.asset is not None else ("name", obj.name)
        groups.setdefault(key, []).append(obj)

    assets = tuple(
        sorted(
            (
                AssetUsage(
                    key=identity,
                    label=_label(kind, identity, members),
                    asset=identity if kind == "asset" else None,
                    count=len(members),
                    percentage=len(members) / total * 100.0 if total else 0.0,
                    flagged=(len(members) / total * 100.0 if total else 0.0) > threshold,
                    subjects=tuple(sorted(obj.id for obj in members)),
                )
                for (kind, identity), members in groups.items()
            ),
            key=lambda asset: (-asset.count, asset.label.casefold(), asset.key),
        )
    )

    collector = IssueCollector(Category.SPATIAL)
    for asset in assets:
        if not asset.flagged:
            continue
        allowed = _allowance(total, threshold)
        surplus = max(1, asset.count - allowed)
        collector.add(
            "asset_overuse",
            context.config.severity_for("asset_overuse", Severity.WARNING),
            f"'{asset.label}' fills {asset.percentage:.1f}% of the scene",
            evidence=(
                f"'{asset.label}' is placed {asset.count} "
                f"{'time' if asset.count == 1 else 'times'} out of {total} "
                f"placements ({asset.percentage:.1f}%), above the "
                f"{threshold:g}% repetition line."
            ),
            explanation=(
                "One asset repeated this often flattens a level: separate areas "
                "start reading as the same place, and a player stops using the "
                "scenery to tell where they are."
            ),
            recommendation=(
                (
                    f"With only {total} placements in the scene, a single use is "
                    f"already more than {threshold:g}% of the level, so this says "
                    f"more about the scene being sparse than about '{asset.label}': "
                    "add content or raise the repetition threshold for a scene "
                    "this small."
                )
                if allowed < 1
                else (
                    f"Replace {surplus} of the {asset.count} placements with other "
                    f"assets, or spread them across more zones, to bring the share "
                    f"to {threshold:g}% or below."
                )
            ),
            subjects=asset.subjects,
            metrics=(
                ("placements", float(asset.count)),
                ("share_percent", asset.percentage),
                ("threshold_percent", threshold),
                ("total_placements", float(total)),
                ("surplus_placements", float(surplus)),
            ),
        )

    return RepetitionReport(
        issues=collector.result(),
        scene=scene.scene,
        threshold=threshold,
        total_placements=total,
        assets=assets,
    )


def format_report(report: RepetitionReport) -> str:
    """Format the per-asset usage table for a terminal."""
    rows = [
        (
            asset.label,
            str(asset.count),
            f"{asset.percentage:.2f}%",
            "FLAGGED" if asset.flagged else "-",
            asset.asset or "(no asset, grouped by name)",
        )
        for asset in report.assets
    ]
    lines = [
        f"Mapwright repetition report: {report.scene}",
        (
            f"Placements: {report.total_placements} | "
            f"Distinct assets: {report.distinct_assets} | "
            f"Flag threshold: > {report.threshold:g}%"
        ),
        "",
    ]
    if rows:
        lines.extend(_render_table(("Asset", "Uses", "Percent", "Status", "Source"), rows))
    else:
        lines.append("No placed content in this scene.")
    lines.append("")
    lines.append(f"Flagged assets: {len(report.flagged_assets)}")
    lines.extend(_findings(report.issues))
    return "\n".join(lines)


def _label(kind: str, identity: str, members: Sequence[SceneObject]) -> str:
    """Return a readable name for one asset family."""
    if kind != "asset":
        return identity
    normalized = identity.replace("\\", "/")
    filename = PurePosixPath(normalized).name
    return PurePosixPath(filename).stem or filename or members[0].name


def _allowance(total: int, threshold: float) -> int:
    """Return how many placements one asset may hold and stay under the line."""
    return math.floor(total * threshold / 100.0 + 1e-9)


def _render_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> list[str]:
    """Return a padded text table with a divider under the header row."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    return [
        render(headers),
        "  ".join("-" * width for width in widths),
        *(render(row) for row in rows),
    ]


def _findings(issues: Sequence[Issue]) -> list[str]:
    """Return the findings section shared by every Mapwright text report."""
    if not issues:
        return ["", "Findings: none"]
    lines = ["", "Findings:"]
    for issue in issues:
        suffix = " (advisory)" if issue.advisory else ""
        lines.append("")
        lines.append(f"- {issue.code}{suffix}: {issue.summary}")
        lines.extend(f"  {line}" for line in issue.to_text().splitlines())
    return lines
