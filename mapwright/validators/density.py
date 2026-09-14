"""Regional density: where content sits on the ground, and where it does not.

The playable area is divided into equal-area cells and each cell counts the
prop origins inside it. A level can hold the right amount of content overall
and still be badly composed, so the finding is about distribution rather than
about the total.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.core.metrics import Distribution


HEAT_CHARACTERS = " .:-=+*#%@"


@dataclass(frozen=True)
class DensityCell:
    """One equal-area ground cell and the number of prop origins in it."""

    column: int
    row: int
    min_x: float
    max_x: float
    min_z: float
    max_z: float
    count: int

    @property
    def label(self) -> str:
        """Return the designer-facing cell name, counted from one."""
        return f"C{self.column + 1}/R{self.row + 1}"

    @property
    def center_x(self) -> float:
        """Return the cell centre along X."""
        return (self.min_x + self.max_x) * 0.5

    @property
    def center_z(self) -> float:
        """Return the cell centre along Z."""
        return (self.min_z + self.max_z) * 0.5

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "label": self.label,
            "column": self.column,
            "row": self.row,
            "bounds": {
                "min": [self.min_x, self.min_z],
                "max": [self.max_x, self.max_z],
            },
            "count": self.count,
        }


@dataclass(frozen=True)
class DensityReport:
    """Grid density measurements over the playable area."""

    issues: tuple[Issue, ...]
    scene: str
    available: bool
    unavailable_reason: str | None
    requested_cell_size: float
    cell_width: float
    cell_depth: float
    columns: int
    rows: int
    ground: Bounds | None
    cells: tuple[DensityCell, ...]
    prop_count: int
    outside_count: int
    mean: float
    variance: float
    standard_deviation: float
    imbalance: float
    imbalance_threshold: float
    empty_share_threshold: float

    @property
    def empty_cells(self) -> tuple[DensityCell, ...]:
        """Return the cells holding no props."""
        return tuple(cell for cell in self.cells if cell.count == 0)

    @property
    def densest_cells(self) -> tuple[DensityCell, ...]:
        """Return every cell tied for the highest count."""
        maximum = max((cell.count for cell in self.cells), default=0)
        return tuple(cell for cell in self.cells if cell.count == maximum)

    @property
    def empty_ratio(self) -> float:
        """Return the share of cells holding nothing, from zero to one."""
        return len(self.empty_cells) / len(self.cells) if self.cells else 0.0

    @property
    def densest_share(self) -> float:
        """Return the share of props inside the densest cell."""
        maximum = max((cell.count for cell in self.cells), default=0)
        return maximum / self.prop_count if self.prop_count else 0.0

    @property
    def imbalanced(self) -> bool:
        """Return whether the distribution is past the imbalance line."""
        return self.available and self.imbalance > self.imbalance_threshold

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "requested_cell_size": self.requested_cell_size,
            "cell_width": self.cell_width,
            "cell_depth": self.cell_depth,
            "columns": self.columns,
            "rows": self.rows,
            "ground": self.ground.to_dict() if self.ground is not None else None,
            "cells": [cell.to_dict() for cell in self.cells],
            "prop_count": self.prop_count,
            "outside_count": self.outside_count,
            "mean": self.mean,
            "variance": self.variance,
            "standard_deviation": self.standard_deviation,
            "imbalance": self.imbalance,
            "imbalance_threshold": self.imbalance_threshold,
            "empty_share_threshold": self.empty_share_threshold,
            "empty_ratio": self.empty_ratio,
            "densest_share": self.densest_share,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_density(context: AnalysisContext) -> DensityReport:
    """Measure how evenly placed content covers the playable area."""
    thresholds = context.config.thresholds
    cell_size = thresholds.density_cell_size
    ground = context.ground
    if ground is None or ground.width <= 0.0 or ground.depth <= 0.0:
        return _unavailable(context, ground)

    columns = max(1, math.ceil(ground.width / cell_size))
    rows = max(1, math.ceil(ground.depth / cell_size))
    cell_width = ground.width / columns
    cell_depth = ground.depth / rows

    counts = [[0 for _ in range(columns)] for _ in range(rows)]
    outside = 0
    for obj in context.scene.props:
        point = obj.position
        if not ground.contains_xz(point):
            outside += 1
            continue
        column = _cell_index(point.x, ground.min.x, ground.max.x, columns)
        row = _cell_index(point.z, ground.min.z, ground.max.z, rows)
        counts[row][column] += 1

    cells = tuple(
        DensityCell(
            column=column,
            row=row,
            min_x=ground.min.x + column * cell_width,
            max_x=ground.min.x + (column + 1) * cell_width,
            min_z=ground.min.z + row * cell_depth,
            max_z=ground.min.z + (row + 1) * cell_depth,
            count=counts[row][column],
        )
        for row in range(rows)
        for column in range(columns)
    )
    cell_counts = [cell.count for cell in cells]
    distribution = Distribution.of(cell_counts)
    prop_count = sum(cell_counts)

    report = DensityReport(
        issues=(),
        scene=context.scene.scene,
        available=True,
        unavailable_reason=None,
        requested_cell_size=cell_size,
        cell_width=cell_width,
        cell_depth=cell_depth,
        columns=columns,
        rows=rows,
        ground=ground,
        cells=cells,
        prop_count=prop_count,
        outside_count=outside,
        mean=distribution.mean,
        variance=distribution.variance,
        standard_deviation=distribution.standard_deviation,
        imbalance=distribution.imbalance,
        imbalance_threshold=thresholds.density_imbalance,
        empty_share_threshold=thresholds.empty_zone_ratio,
    )
    return _with_issues(context, report)


def format_report(report: DensityReport) -> str:
    """Format the density metrics and the ASCII heatmap for a terminal."""
    if not report.available:
        return "\n".join(
            [
                f"Mapwright density report: {report.scene}",
                f"Not measured: {report.unavailable_reason}",
                *_findings(report.issues),
            ]
        )

    counts = {(cell.column, cell.row): cell.count for cell in report.cells}
    border = "+" + "-" * report.columns + "+"
    heatmap = [border]
    for row in reversed(range(report.rows)):
        heatmap.append(
            "|"
            + "".join(
                _heat_character(counts[(column, row)]) for column in range(report.columns)
            )
            + "|"
        )
    heatmap.append(border)

    ground = report.ground
    densest_count = max((cell.count for cell in report.cells), default=0)
    status = (
        "WARNING: IMBALANCED" if report.imbalanced else "BELOW WARNING THRESHOLD"
    )
    lines = [
        f"Mapwright density report: {report.scene}",
        (
            f"Ground: {ground.width:.2f} x {ground.depth:.2f} | "
            f"Grid: {report.columns} x {report.rows} | "
            f"Actual cell: {report.cell_width:.2f} x {report.cell_depth:.2f}"
        ),
        "",
        "ASCII heatmap (+Z / north at top):",
        *heatmap,
        "Legend: 0=' ' 1='.' 2=':' 3='-' 4='=' 5='+' 6='*' 7='#' 8='%' 9+='@'",
        "",
        (
            f"Props inside ground: {report.prop_count} | "
            f"Outside ground: {report.outside_count}"
        ),
        (
            f"Densest cells: {', '.join(cell.label for cell in report.densest_cells)} "
            f"({_plural(densest_count, 'prop')})"
        ),
        f"Densest cell share: {report.densest_share * 100:.2f}%",
        (
            f"Empty cells: {len(report.empty_cells)}/{len(report.cells)} "
            f"({report.empty_ratio * 100:.2f}%)"
        ),
        (
            f"Mean: {report.mean:.3f} | Variance: {report.variance:.3f} | "
            f"Std dev: {report.standard_deviation:.3f}"
        ),
        (
            f"Imbalance score: {report.imbalance:.2f}/100 | "
            f"Warning threshold: > {report.imbalance_threshold:g} | {status}"
        ),
    ]
    lines.extend(_findings(report.issues))
    return "\n".join(lines)


def _with_issues(context: AnalysisContext, report: DensityReport) -> DensityReport:
    """Return the report with its findings attached."""
    collector = IssueCollector(Category.SPATIAL)
    densest = report.densest_cells[0] if report.densest_cells else None
    densest_count = densest.count if densest is not None else 0
    targets = _redistribution_targets(report, densest)

    if report.imbalanced and densest is not None:
        move = max(1, int(round(densest_count - report.mean)))
        collector.add(
            "density_imbalance",
            context.config.severity_for("density_imbalance", Severity.WARNING),
            "Content is concentrated in a few cells",
            evidence=(
                f"Density imbalance score is {report.imbalance:.1f}/100 across "
                f"{len(report.cells)} cells; the densest cell {densest.label} holds "
                f"{densest_count} of {report.prop_count} props "
                f"({report.densest_share * 100:.1f}%), against a mean of "
                f"{report.mean:.2f} per cell."
            ),
            explanation=(
                "Content concentrated in a few cells leaves the rest of the space "
                "reading as filler, and the crowded cells stop reading as "
                "individual placements."
            ),
            recommendation=(
                (
                    f"No cell holds more than {_plural(densest_count, 'prop')}, so "
                    f"the score comes from empty ground rather than crowding: add "
                    f"content across {targets}, or reduce the playable area."
                )
                if densest_count <= 1
                else (
                    f"Redistribute {_plural(move, 'prop')} from cell "
                    f"{densest.label} into {targets}."
                )
            ),
            metrics=(
                ("imbalance", report.imbalance),
                ("imbalance_threshold", report.imbalance_threshold),
                ("cells", float(len(report.cells))),
                ("densest_count", float(densest_count)),
                ("densest_share_percent", report.densest_share * 100.0),
                ("mean_per_cell", report.mean),
                ("standard_deviation", report.standard_deviation),
            ),
        )

    empty_share = report.empty_ratio * 100.0
    if report.cells and empty_share > report.empty_share_threshold:
        collector.add(
            "empty_region_share",
            context.config.severity_for("empty_region_share", Severity.INFO),
            "Most of the playable area holds no content",
            evidence=(
                f"{len(report.empty_cells)} of {len(report.cells)} cells "
                f"({empty_share:.1f}%) hold no props, above the "
                f"{report.empty_share_threshold:g}% empty-cell line, over a ground "
                f"area of {report.ground.width:.1f} x {report.ground.depth:.1f} m."
            ),
            explanation=(
                "Negative space is a design tool, but this much of it usually "
                "means the playable area is larger than the content built for it, "
                "and the walk between places carries nothing."
            ),
            recommendation=(
                f"Either extend content into {targets}, or shrink the playable "
                "area to the part of the level that is actually used."
            ),
            metrics=(
                ("empty_cells", float(len(report.empty_cells))),
                ("cells", float(len(report.cells))),
                ("empty_share_percent", empty_share),
                ("empty_share_threshold", report.empty_share_threshold),
            ),
        )

    return replace(report, issues=collector.result())


def _redistribution_targets(
    report: DensityReport, densest: DensityCell | None
) -> str:
    """Name up to three sparse cells worth moving content into."""
    candidates = report.empty_cells or tuple(
        cell
        for cell in report.cells
        if cell.count == min((item.count for item in report.cells), default=0)
    )
    candidates = tuple(cell for cell in candidates if cell is not densest)
    if not candidates:
        return "the sparser cells"
    if densest is not None:
        candidates = tuple(
            sorted(
                candidates,
                key=lambda cell: (
                    math.hypot(
                        cell.center_x - densest.center_x,
                        cell.center_z - densest.center_z,
                    ),
                    cell.column,
                    cell.row,
                ),
            )
        )
    chosen = [cell.label for cell in candidates[:3]]
    listed = ", ".join(chosen)
    kind = "empty" if report.empty_cells else "sparsest"
    return f"the {kind} cells at {listed}"


def _unavailable(context: AnalysisContext, ground: Bounds | None) -> DensityReport:
    """Return a report explaining why density could not be measured."""
    reason = (
        "the scene declares no ground and no measured objects, so there is no "
        "playable area to divide into cells"
        if ground is None
        else (
            f"the playable area measures {ground.width:.2f} x {ground.depth:.2f} m, "
            "which has no usable extent"
        )
    )
    collector = IssueCollector(Category.SPATIAL)
    collector.add(
        "density_area_unavailable",
        context.config.severity_for("density_area_unavailable", Severity.INFO),
        "Density was not measured",
        evidence=(
            f"Density analysis needs a playable area, but {reason}; "
            f"{_plural(len(context.scene.props), 'placed object')} went uncounted."
        ),
        explanation=(
            "Without a ground extent there is no denominator for distribution: "
            "the same props can be even or clustered depending on the area."
        ),
        recommendation=(
            "Declare a ground object or a scene ground extent, then rerun the "
            "density analysis."
        ),
        metrics=(("props", float(len(context.scene.props))),),
    )
    thresholds = context.config.thresholds
    return DensityReport(
        issues=collector.result(),
        scene=context.scene.scene,
        available=False,
        unavailable_reason=reason,
        requested_cell_size=thresholds.density_cell_size,
        cell_width=0.0,
        cell_depth=0.0,
        columns=0,
        rows=0,
        ground=ground,
        cells=(),
        prop_count=0,
        outside_count=len(context.scene.props),
        mean=0.0,
        variance=0.0,
        standard_deviation=0.0,
        imbalance=0.0,
        imbalance_threshold=thresholds.density_imbalance,
        empty_share_threshold=thresholds.empty_zone_ratio,
    )


def _cell_index(value: float, minimum: float, maximum: float, count: int) -> int:
    """Return the cell a coordinate falls in, with the far edge held inside."""
    if math.isclose(value, maximum, rel_tol=1e-9, abs_tol=1e-9):
        return count - 1
    normalized = (value - minimum) / (maximum - minimum)
    return min(max(int(normalized * count), 0), count - 1)


def _plural(count: int, singular: str) -> str:
    """Return a count and its noun, pluralized with a trailing 's'."""
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _heat_character(count: int) -> str:
    """Return the heatmap character for one cell count."""
    return HEAT_CHARACTERS[min(count, len(HEAT_CHARACTERS) - 1)]


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
