"""Spacing between placed objects, at prop scale and at player scale.

Two measurements answer two different questions: centre-to-centre distance
says whether props are crowded into each other, and the surface-to-surface
horizontal gap says whether the space they leave between them is a route a
body can actually use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds
from mapwright.core.issues import Category, Issue, IssueCollector, Severity


@dataclass(frozen=True)
class ObjectPair:
    """Two placed objects and the distances measured between them."""

    first_id: str
    second_id: str
    first_name: str
    second_name: str
    distance: float
    gap: float

    @property
    def subjects(self) -> tuple[str, str]:
        """Return the two object identifiers."""
        return (self.first_id, self.second_id)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "first": self.first_id,
            "second": self.second_id,
            "first_name": self.first_name,
            "second_name": self.second_name,
            "distance": self.distance,
            "gap": self.gap,
        }


@dataclass(frozen=True)
class SpacingReport:
    """Pairwise spacing measurements for one scene."""

    issues: tuple[Issue, ...]
    scene: str
    minimum_spacing: float
    player_diameter: float
    measured_count: int
    unmeasured_count: int
    pair_count: int
    close_pairs: tuple[ObjectPair, ...]
    squeeze_pairs: tuple[ObjectPair, ...]

    @property
    def closest_distance(self) -> float | None:
        """Return the smallest centre distance measured, when there is one."""
        return self.close_pairs[0].distance if self.close_pairs else None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "minimum_spacing": self.minimum_spacing,
            "player_diameter": self.player_diameter,
            "measured_count": self.measured_count,
            "unmeasured_count": self.unmeasured_count,
            "pair_count": self.pair_count,
            "close_pairs": [pair.to_dict() for pair in self.close_pairs],
            "squeeze_pairs": [pair.to_dict() for pair in self.squeeze_pairs],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_spacing(context: AnalysisContext) -> SpacingReport:
    """Measure pairwise object spacing and flag crowding and squeeze points."""
    scene = context.scene
    minimum_spacing = context.config.thresholds.minimum_spacing
    diameter = context.config.minimum_path_width
    objects = tuple(sorted(scene.obstacles, key=lambda obj: obj.id))
    boxes = {obj.id: obj.world_bounds() for obj in objects}

    close: list[ObjectPair] = []
    squeeze: list[ObjectPair] = []
    pair_count = 0
    for index, first in enumerate(objects):
        for second in objects[index + 1 :]:
            pair_count += 1
            distance = first.position.distance_to(second.position)
            gap = _horizontal_gap(boxes[first.id], boxes[second.id])
            pair = ObjectPair(
                first_id=first.id,
                second_id=second.id,
                first_name=first.name,
                second_name=second.name,
                distance=distance,
                gap=gap,
            )
            if distance < minimum_spacing and not math.isclose(
                distance, minimum_spacing, rel_tol=1e-9, abs_tol=1e-9
            ):
                close.append(pair)
            elif 0.0 < gap < diameter and not math.isclose(
                gap, diameter, rel_tol=1e-9, abs_tol=1e-9
            ):
                squeeze.append(pair)

    close.sort(key=lambda pair: (pair.distance, pair.first_id, pair.second_id))
    squeeze.sort(key=lambda pair: (pair.gap, pair.first_id, pair.second_id))

    collector = IssueCollector(Category.SPATIAL)
    for pair in close:
        collector.add(
            "objects_too_close",
            context.config.severity_for("objects_too_close", Severity.WARNING),
            f"'{pair.first_name}' and '{pair.second_name}' are crowded together",
            evidence=(
                f"'{pair.first_name}' ({pair.first_id}) and '{pair.second_name}' "
                f"({pair.second_id}) stand {pair.distance:.2f} m apart centre to "
                f"centre, below the {minimum_spacing:.2f} m minimum spacing; their "
                f"footprints leave a {pair.gap:.2f} m horizontal gap."
            ),
            explanation=(
                "Objects this close read as one cluttered mass rather than two "
                "placed things, and they often clip into each other when seen "
                "from the ground."
            ),
            recommendation=(
                f"Move one of the two at least "
                f"{minimum_spacing - pair.distance:.2f} m further away, or commit "
                "to the overlap by making them a single deliberate cluster."
            ),
            subjects=pair.subjects,
            metrics=(
                ("distance", pair.distance),
                ("minimum_spacing", minimum_spacing),
                ("surface_gap", pair.gap),
            ),
        )
    for pair in squeeze:
        collector.add(
            "squeeze_point",
            context.config.severity_for("squeeze_point", Severity.WARNING),
            f"Gap between '{pair.first_name}' and '{pair.second_name}' is impassable",
            evidence=(
                f"'{pair.first_name}' ({pair.first_id}) and '{pair.second_name}' "
                f"({pair.second_id}) leave a {pair.gap:.2f} m horizontal gap "
                f"between their surfaces, narrower than the {diameter:.2f} m "
                "player diameter."
            ),
            explanation=(
                "A gap that looks like a way through but is too narrow to walk "
                "sends a player at it and then stops them, which reads as the "
                "level misleading them rather than as a closed route."
            ),
            recommendation=(
                f"Widen the gap to at least {diameter:.2f} m so it can be used, "
                "or close it so it reads as solid."
            ),
            subjects=pair.subjects,
            metrics=(
                ("surface_gap", pair.gap),
                ("player_diameter", diameter),
                ("distance", pair.distance),
            ),
        )

    return SpacingReport(
        issues=collector.result(),
        scene=scene.scene,
        minimum_spacing=minimum_spacing,
        player_diameter=diameter,
        measured_count=len(objects),
        unmeasured_count=len(scene.props) - len(objects),
        pair_count=pair_count,
        close_pairs=tuple(close),
        squeeze_pairs=tuple(squeeze),
    )


def format_report(report: SpacingReport) -> str:
    """Format the too-close and squeeze-point tables for a terminal."""
    lines = [
        f"Mapwright spacing report: {report.scene}",
        (
            f"Measured props: {report.measured_count} | "
            f"Unmeasured props: {report.unmeasured_count} | "
            f"Pairs tested: {report.pair_count}"
        ),
        (
            f"Minimum spacing: {report.minimum_spacing:g} | "
            f"Player diameter: {report.player_diameter:g}"
        ),
        "",
        "Too-close pairs:",
    ]
    if report.close_pairs:
        lines.extend(
            _render_table(
                ("First", "Second", "Distance", "Gap", "Status"),
                [
                    (
                        pair.first_id,
                        pair.second_id,
                        f"{pair.distance:.3f}",
                        f"{pair.gap:.3f}",
                        "TOO_CLOSE",
                    )
                    for pair in report.close_pairs
                ],
            )
        )
    else:
        lines.append("None")
    lines.extend(["", "Squeeze points (gap narrower than the player):"])
    if report.squeeze_pairs:
        lines.extend(
            _render_table(
                ("First", "Second", "Gap", "Distance", "Status"),
                [
                    (
                        pair.first_id,
                        pair.second_id,
                        f"{pair.gap:.3f}",
                        f"{pair.distance:.3f}",
                        "SQUEEZE",
                    )
                    for pair in report.squeeze_pairs
                ],
            )
        )
    else:
        lines.append("None")
    lines.append("")
    lines.append(
        f"Too-close pairs: {len(report.close_pairs)} | "
        f"Squeeze points: {len(report.squeeze_pairs)}"
    )
    lines.extend(_findings(report.issues))
    return "\n".join(lines)


def _horizontal_gap(first: Bounds | None, second: Bounds | None) -> float:
    """Return the surface-to-surface XZ distance between two world boxes."""
    if first is None or second is None:
        return 0.0
    gap_x = max(first.min.x - second.max.x, second.min.x - first.max.x, 0.0)
    gap_z = max(first.min.z - second.max.z, second.min.z - first.max.z, 0.0)
    return math.hypot(gap_x, gap_z)


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
