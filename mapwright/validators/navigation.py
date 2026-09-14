"""Approximate navigability: what a player can actually reach on foot.

Free space is read off the shared occupancy grid, which is already inset and
padded by the player radius, so "reachable" means reachable by a body of the
configured size rather than by a point. The estimate is a rasterization, so it
is re-tested at a coarser clearance to say when a result depends on the grid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.core.metrics import (
    GridError,
    OccupancyGrid,
    Region,
    build_occupancy_grid,
    render_occupancy_map,
)


@dataclass(frozen=True)
class ObjectApproach:
    """Which free-space region one object can be walked up to from."""

    object_id: str
    name: str
    component_id: int | None
    distance: float | None
    in_main_region: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "object": self.object_id,
            "name": self.name,
            "component_id": self.component_id,
            "distance": self.distance,
            "in_main_region": self.in_main_region,
        }


@dataclass(frozen=True)
class NavigationReport:
    """Connectivity measurements over the walkable grid."""

    issues: tuple[Issue, ...]
    scene: str
    available: bool
    unavailable_reason: str | None
    minimum_path_width: float
    minimum_region_area: float
    columns: int
    rows: int
    cell_width: float
    cell_depth: float
    ground: Bounds | None
    free_cell_count: int
    blocked_cell_count: int
    regions: tuple[Region, ...]
    main_component_id: int | None
    reachable_area: float
    reachable_ratio: float
    approaches: tuple[ObjectApproach, ...]
    occupancy_map: tuple[str, ...]
    no_navigable_area: bool
    disconnected: bool
    clearance_sensitive: bool
    clearance_tested: bool

    @property
    def cell_area(self) -> float:
        """Return the ground area one cell covers."""
        return self.cell_width * self.cell_depth

    @property
    def free_area(self) -> float:
        """Return the total walkable area."""
        return self.free_cell_count * self.cell_area

    @property
    def significant_regions(self) -> tuple[Region, ...]:
        """Return the free-space components large enough to matter."""
        return tuple(region for region in self.regions if region.significant)

    @property
    def main_region(self) -> Region | None:
        """Return the largest reachable region, when there is one."""
        return next(
            (
                region
                for region in self.regions
                if region.component_id == self.main_component_id
            ),
            None,
        )

    @property
    def isolated_regions(self) -> tuple[Region, ...]:
        """Return the significant regions cut off from the main one."""
        return tuple(
            region
            for region in self.significant_regions
            if region.component_id != self.main_component_id
        )

    @property
    def unreachable_approaches(self) -> tuple[ObjectApproach, ...]:
        """Return the objects that cannot be walked up to from the main region."""
        return tuple(item for item in self.approaches if not item.in_main_region)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "minimum_path_width": self.minimum_path_width,
            "minimum_region_area": self.minimum_region_area,
            "columns": self.columns,
            "rows": self.rows,
            "cell_width": self.cell_width,
            "cell_depth": self.cell_depth,
            "ground": self.ground.to_dict() if self.ground is not None else None,
            "free_cell_count": self.free_cell_count,
            "blocked_cell_count": self.blocked_cell_count,
            "regions": [
                {
                    "component_id": region.component_id,
                    "cell_count": region.cell_count,
                    "area": region.area,
                    "bounds": region.bounds.to_dict(),
                    "significant": region.significant,
                }
                for region in self.regions
            ],
            "main_component_id": self.main_component_id,
            "reachable_area": self.reachable_area,
            "reachable_ratio": self.reachable_ratio,
            "approaches": [item.to_dict() for item in self.approaches],
            "occupancy_map": list(self.occupancy_map),
            "no_navigable_area": self.no_navigable_area,
            "disconnected": self.disconnected,
            "clearance_sensitive": self.clearance_sensitive,
            "clearance_tested": self.clearance_tested,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_navigation(context: AnalysisContext) -> NavigationReport:
    """Estimate connected walkable space and what it can reach."""
    grid = context.grid
    if grid is None:
        return _unavailable(context)

    thresholds = context.config.thresholds
    minimum_region_area = thresholds.navigation_minimum_region_area
    labels, regions = grid.connected_regions(minimum_region_area)
    significant = tuple(region for region in regions if region.significant)
    main_region = (
        max(regions, key=lambda region: region.cell_count) if regions else None
    )
    main_component_id = main_region.component_id if main_region is not None else None
    free_cells = grid.free_cell_count

    sensitive, tested, inflation = _clearance_sensitivity(
        context, grid, minimum_region_area, bool(significant), len(significant) > 1
    )

    report = NavigationReport(
        issues=(),
        scene=context.scene.scene,
        available=True,
        unavailable_reason=None,
        minimum_path_width=context.config.minimum_path_width,
        minimum_region_area=minimum_region_area,
        columns=grid.columns,
        rows=grid.rows,
        cell_width=grid.cell_width,
        cell_depth=grid.cell_depth,
        ground=grid.ground,
        free_cell_count=free_cells,
        blocked_cell_count=grid.blocked_cell_count,
        regions=regions,
        main_component_id=main_component_id,
        reachable_area=main_region.area if main_region is not None else 0.0,
        reachable_ratio=(
            main_region.cell_count / free_cells
            if main_region is not None and free_cells
            else 0.0
        ),
        approaches=_approaches(grid, labels, main_component_id),
        occupancy_map=tuple(
            render_occupancy_map(
                grid,
                labels,
                main_component_id,
                (region.component_id for region in significant),
            )
        ),
        no_navigable_area=not significant,
        disconnected=len(significant) > 1,
        clearance_sensitive=sensitive,
        clearance_tested=tested,
    )
    return replace(report, issues=_issues(context, report, inflation))


def format_report(report: NavigationReport) -> str:
    """Format connectivity metrics and the occupancy map for a terminal."""
    if not report.available:
        return "\n".join(
            [
                f"Mapwright navigation report: {report.scene}",
                f"Not measured: {report.unavailable_reason}",
                *_findings(report.issues),
            ]
        )

    ground = report.ground
    main = report.main_region
    below_threshold = sum(not region.significant for region in report.regions)
    in_main = sum(item.in_main_region for item in report.approaches)
    if report.no_navigable_area:
        result = "WARNING: NO USABLE NAVIGABLE AREA"
    elif report.disconnected:
        result = "WARNING: DISCONNECTED NAVIGABLE REGIONS"
    elif report.clearance_sensitive:
        result = "CONNECTED; REVIEW: NARROW / GRID-SENSITIVE BOTTLENECK"
    else:
        result = "CONNECTED"

    lines = [
        f"Mapwright navigation report: {report.scene}",
        (
            f"Ground: {ground.width:.2f} x {ground.depth:.2f} | "
            f"Minimum path width: {report.minimum_path_width:.2f} | "
            f"Obstacles: {len(report.approaches)}"
        ),
        (
            f"Grid: {report.columns} x {report.rows} | "
            f"Actual cell: {report.cell_width:.3f} x {report.cell_depth:.3f}"
        ),
        (
            f"Blocked cells: {report.blocked_cell_count} | "
            f"Free cells: {report.free_cell_count} | "
            f"Free area: {report.free_area:.2f}"
        ),
        (
            f"Connected regions: {len(report.regions)} total, "
            f"{len(report.significant_regions)} significant, "
            f"{below_threshold} below {report.minimum_region_area:g} area"
        ),
        (
            f"Largest reachable region: {main.area:.2f} "
            f"({report.reachable_ratio * 100:.2f}% of free space)"
            if main is not None
            else "Largest reachable region: none"
        ),
        f"Prop approaches in main region: {in_main}/{len(report.approaches)}",
        f"Clearance sensitivity: {_sensitivity_line(report)}",
        "",
        "Occupancy map (+Z / north at top):",
        *report.occupancy_map,
        "Legend: # blocked, . main reachable region, A-Z disconnected region, o tiny pocket",
    ]
    if report.isolated_regions:
        lines.extend(["", "Disconnected significant regions:"])
        lines.extend(
            (
                f"- Region {region.component_id}: area {region.area:.2f}, "
                f"bounds X[{region.bounds.min.x:.2f}, {region.bounds.max.x:.2f}] "
                f"Z[{region.bounds.min.z:.2f}, {region.bounds.max.z:.2f}]"
            )
            for region in report.isolated_regions
        )
    if report.unreachable_approaches:
        lines.extend(["", "Prop approaches outside the main region:"])
        lines.extend(
            f"- {item.name} ({item.object_id})"
            for item in report.unreachable_approaches
        )
    lines.extend(["", f"Result: {result}"])
    lines.extend(_findings(report.issues))
    return "\n".join(lines)


def _sensitivity_line(report: NavigationReport) -> str:
    """Return how the re-test at coarser clearance turned out."""
    if report.clearance_sensitive:
        return "REVIEW (half a cell more clearance changes connectivity)"
    if report.clearance_tested:
        return "stable at this grid resolution"
    if report.no_navigable_area or report.disconnected:
        return "not applicable; connectivity is already decided"
    return "not testable at this grid resolution"


def _approaches(
    grid: OccupancyGrid,
    labels: Sequence[Sequence[int]],
    main_component_id: int | None,
) -> tuple[ObjectApproach, ...]:
    """Return the nearest free-space component to each obstacle."""
    free_cells = [
        (component_id, grid.cell_center(column, row))
        for row, label_row in enumerate(labels)
        for column, component_id in enumerate(label_row)
        if component_id >= 0
    ]
    approaches: list[ObjectApproach] = []
    for footprint in grid.footprints:
        if not free_cells:
            approaches.append(
                ObjectApproach(footprint.object_id, footprint.name, None, None, False)
            )
            continue
        best_distance = math.inf
        best_component = free_cells[0][0]
        for component_id, center in free_cells:
            distance = footprint.distance_to(center.x, center.z)
            if distance < best_distance:
                best_distance = distance
                best_component = component_id
        approaches.append(
            ObjectApproach(
                object_id=footprint.object_id,
                name=footprint.name,
                component_id=best_component,
                distance=best_distance,
                in_main_region=best_component == main_component_id,
            )
        )
    return tuple(sorted(approaches, key=lambda item: item.object_id))


def _clearance_sensitivity(
    context: AnalysisContext,
    grid: OccupancyGrid,
    minimum_region_area: float,
    has_navigable_area: bool,
    disconnected: bool,
) -> tuple[bool, bool, float]:
    """Re-test connectivity with half a cell of extra clearance.

    The rasterized answer can hinge on a passage only as wide as one cell, so
    the same scene is measured again with a slightly fatter body over the same
    playable area. A result that changes is a measurement caveat, not proof
    that the level is cut in two.
    """
    inflation = max(grid.cell_width, grid.cell_depth) * 0.5
    if not has_navigable_area or disconnected:
        return False, False, inflation
    expanded = Bounds(
        Vec3(
            grid.ground.min.x - inflation,
            grid.ground.min.y,
            grid.ground.min.z - inflation,
        ),
        Vec3(
            grid.ground.max.x + inflation,
            grid.ground.max.y,
            grid.ground.max.z + inflation,
        ),
    )
    try:
        conservative = build_occupancy_grid(
            context.scene,
            cell_size=context.config.thresholds.navigation_cell_size,
            agent_radius=grid.agent_radius + inflation,
            area=expanded,
        )
    except GridError:
        return False, False, inflation
    _, regions = conservative.connected_regions(minimum_region_area)
    significant = sum(region.significant for region in regions)
    return significant != 1, True, inflation


def _issues(
    context: AnalysisContext, report: NavigationReport, inflation: float
) -> tuple[Issue, ...]:
    """Return the connectivity findings this estimate supports."""
    collector = IssueCollector(Category.NAVIGATION)
    ground = report.ground
    largest = max((region.area for region in report.regions), default=0.0)

    if report.no_navigable_area:
        collector.add(
            "no_navigable_area",
            context.config.severity_for("no_navigable_area", Severity.CRITICAL),
            "The level has no usable walkable space",
            evidence=(
                f"No free-space region reaches the "
                f"{report.minimum_region_area:g} m2 significance line: "
                f"{report.free_cell_count} of "
                f"{report.free_cell_count + report.blocked_cell_count} grid cells "
                f"are free ({report.free_area:.2f} m2 in "
                f"{len(report.regions)} pocket(s), largest {largest:.2f} m2) over a "
                f"{ground.width:.1f} x {ground.depth:.1f} m area with "
                f"{len(report.approaches)} obstacle(s)."
            ),
            explanation=(
                "A body "
                f"{report.minimum_path_width:.2f} m across cannot stand anywhere "
                "that matters, so nothing in the level can be played as laid out."
            ),
            recommendation=(
                "Remove, shrink, or spread the obstacles until at least one open "
                f"area of {report.minimum_region_area:g} m2 exists, or enlarge the "
                "ground the level sits on."
            ),
            metrics=(
                ("free_area", report.free_area),
                ("free_cells", float(report.free_cell_count)),
                ("largest_region_area", largest),
                ("minimum_region_area", report.minimum_region_area),
                ("obstacles", float(len(report.approaches))),
            ),
        )

    if report.disconnected:
        isolated = report.isolated_regions
        worst = max(isolated, key=lambda region: region.area)
        center = worst.bounds.center
        collector.add(
            "disconnected_regions",
            context.config.severity_for("disconnected_regions", Severity.ERROR),
            f"Walkable space splits into {len(report.significant_regions)} regions",
            evidence=(
                f"Free space splits into {len(report.significant_regions)} regions "
                f"of at least {report.minimum_region_area:g} m2. The main region "
                f"covers {report.reachable_area:.2f} m2 "
                f"({report.reachable_ratio * 100:.1f}% of free space); the largest "
                f"cut-off region is {worst.area:.2f} m2 around "
                f"({center.x:.1f}, {center.z:.1f})."
            ),
            explanation=(
                "A player standing in the main region cannot walk to the other "
                "region at all, so whatever is placed there is unreachable unless "
                "some other movement mechanic connects it."
            ),
            recommendation=(
                f"Open a passage at least {report.minimum_path_width:.2f} m wide "
                f"between the main region and the area around "
                f"({center.x:.1f}, {center.z:.1f}), or record the separation as "
                "intentional."
            ),
            metrics=(
                ("significant_regions", float(len(report.significant_regions))),
                ("main_region_area", report.reachable_area),
                ("reachable_ratio_percent", report.reachable_ratio * 100.0),
                ("isolated_area", worst.area),
                ("isolated_center_x", center.x),
                ("isolated_center_z", center.z),
            ),
        )

    if report.clearance_sensitive:
        collector.add(
            "narrow_clearance",
            context.config.severity_for("narrow_clearance", Severity.WARNING),
            "Connectivity depends on a passage about one cell wide",
            evidence=(
                f"Free space is connected at the analysis clearance, but adding "
                f"{inflation:.2f} m of clearance (half a grid cell of "
                f"{report.cell_width:.2f} x {report.cell_depth:.2f} m) splits it "
                f"into more than one significant region."
            ),
            explanation=(
                "This measures the grid, not the level: a passage this close to "
                "the cell size may be perfectly walkable. It is not proof of "
                "disconnection, only a sign that the answer is resolution "
                "dependent."
            ),
            recommendation=(
                "Re-run with a smaller navigation cell size and inspect the "
                "narrow passage on the occupancy map; widen it past "
                f"{report.minimum_path_width + inflation:.2f} m if it is meant to "
                "be a route."
            ),
            metrics=(
                ("inflation", inflation),
                ("cell_width", report.cell_width),
                ("cell_depth", report.cell_depth),
                ("minimum_path_width", report.minimum_path_width),
            ),
            advisory=True,
        )

    if not report.no_navigable_area:
        for approach in report.unreachable_approaches:
            distance = approach.distance if approach.distance is not None else 0.0
            collector.add(
                "prop_unreachable",
                context.config.severity_for("prop_unreachable", Severity.WARNING),
                f"'{approach.name}' cannot be approached from the main region",
                evidence=(
                    f"The nearest walkable cell to '{approach.name}' "
                    f"({approach.object_id}) is {distance:.2f} m away in free-space "
                    f"region {approach.component_id}, not in the main region of "
                    f"{report.reachable_area:.2f} m2."
                ),
                explanation=(
                    "A player who never reaches this object cannot use it as "
                    "cover, a target, or a landmark seen up close; the work put "
                    "into it is spent on an area nobody walks."
                ),
                recommendation=(
                    f"Move '{approach.name}' into the main walkable region, or "
                    "connect its pocket of free space to the main region with a "
                    f"passage at least {report.minimum_path_width:.2f} m wide."
                ),
                subjects=(approach.object_id,),
                metrics=(
                    ("approach_distance", distance),
                    ("component_id", float(approach.component_id or 0)),
                    ("main_region_area", report.reachable_area),
                ),
            )

    return collector.result()


def _unavailable(context: AnalysisContext) -> NavigationReport:
    """Return a report explaining why connectivity could not be measured."""
    reason = context.grid_reason
    collector = IssueCollector(Category.NAVIGATION)
    collector.add(
        "navigation_unavailable",
        context.config.severity_for("navigation_unavailable", Severity.INFO),
        "Navigability was not measured",
        evidence=(
            f"No walkability grid could be built for this scene: {reason}. "
            f"{len(context.scene.props)} placed object(s) and "
            f"{len(context.scene.all_markers)} marker(s) went untested."
        ),
        explanation=(
            "Connectivity is measured against the playable area; with no such "
            "area there is nothing to divide into reachable and unreachable."
        ),
        recommendation=(
            "Declare a ground object or ground extent large enough for a body "
            f"{context.config.minimum_path_width:.2f} m across, then rerun the "
            "navigation analysis."
        ),
        metrics=(("props", float(len(context.scene.props))),),
    )
    return NavigationReport(
        issues=collector.result(),
        scene=context.scene.scene,
        available=False,
        unavailable_reason=reason,
        minimum_path_width=context.config.minimum_path_width,
        minimum_region_area=context.config.thresholds.navigation_minimum_region_area,
        columns=0,
        rows=0,
        cell_width=0.0,
        cell_depth=0.0,
        ground=context.ground,
        free_cell_count=0,
        blocked_cell_count=0,
        regions=(),
        main_component_id=None,
        reachable_area=0.0,
        reachable_ratio=0.0,
        approaches=(),
        occupancy_map=(),
        no_navigable_area=False,
        disconnected=False,
        clearance_sensitive=False,
        clearance_tested=False,
    )


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
