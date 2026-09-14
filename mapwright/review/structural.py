"""What Mapwright actually saw, and what it could not measure.

A report that lists only findings invites a reader to assume silence means
health. This review states the opposite explicitly: how much of the scene was
measurable, which checks applied, and which stood down and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.issues import Category, Issue, Severity, count_by_severity


@dataclass(frozen=True)
class Coverage:
    """Whether one report section could be assessed, and what it found."""

    category: Category
    applicable: bool
    reason: str
    issue_count: int

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping."""
        return {
            "category": self.category.value,
            "applicable": self.applicable,
            "reason": self.reason,
            "issues": self.issue_count,
        }


@dataclass(frozen=True)
class StructuralReview:
    """An inventory of the scene and the analysis coverage over it."""

    scene: str
    engine: str
    object_count: int
    measured_count: int
    unmeasured: tuple[str, ...]
    zone_count: int
    marker_count: int
    teams: tuple[str, ...]
    ground_size: tuple[float, float] | None
    walkable_area: float | None
    node_count: int
    edge_count: int
    graph_derived: bool
    coverage: tuple[Coverage, ...]
    severity_counts: Mapping[Severity, int]

    @property
    def measurable_share(self) -> float:
        """Return the share of objects with usable extents."""
        if not self.object_count:
            return 0.0
        return self.measured_count / self.object_count

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "engine": self.engine,
            "objects": self.object_count,
            "measured": self.measured_count,
            "measurable_share": round(self.measurable_share, 4),
            "unmeasured": list(self.unmeasured),
            "zones": self.zone_count,
            "markers": self.marker_count,
            "teams": list(self.teams),
            "ground": (
                {"width": self.ground_size[0], "depth": self.ground_size[1]}
                if self.ground_size
                else None
            ),
            "walkable_area": self.walkable_area,
            "route_graph": {
                "nodes": self.node_count,
                "edges": self.edge_count,
                "derived": self.graph_derived,
            },
            "coverage": [item.to_dict() for item in self.coverage],
            "severity_counts": {
                severity.value: count for severity, count in self.severity_counts.items()
            },
        }


def review_structural(
    context: AnalysisContext,
    issues: Sequence[Issue],
    applicable: Mapping[Category, bool],
) -> StructuralReview:
    """Summarize the scene's inventory and which checks could run against it."""
    scene = context.scene
    unmeasured = tuple(
        sorted(obj.id for obj in scene.props if obj.bounds is None)
    )
    ground = context.ground
    grid = context.grid
    graph = context.graph
    per_category = {category: 0 for category in Category}
    for issue in issues:
        per_category[issue.category] += 1

    coverage = tuple(
        Coverage(
            category=category,
            applicable=applicable.get(category, True),
            reason=_coverage_reason(context, category, applicable.get(category, True)),
            issue_count=per_category[category],
        )
        for category in Category
    )
    return StructuralReview(
        scene=scene.scene,
        engine=scene.source_engine,
        object_count=len(scene.objects),
        measured_count=sum(1 for obj in scene.objects if obj.measured),
        unmeasured=unmeasured,
        zone_count=len(scene.zones),
        marker_count=len(scene.all_markers),
        teams=scene.teams,
        ground_size=(ground.width, ground.depth) if ground else None,
        walkable_area=(
            grid.free_cell_count * grid.cell_area if grid is not None else None
        ),
        node_count=len(graph.nodes) if graph else 0,
        edge_count=len(graph.edges) if graph else 0,
        graph_derived=bool(graph and graph.derived),
        coverage=coverage,
        severity_counts=count_by_severity(issues),
    )


def format_report(review: StructuralReview) -> str:
    """Format the inventory and coverage for a terminal."""
    lines = [
        f"Mapwright structural review: {review.scene} ({review.engine})",
        (
            f"Objects: {review.object_count} "
            f"({review.measured_count} measured, {len(review.unmeasured)} without extents)"
        ),
        f"Zones: {review.zone_count} | Markers: {review.marker_count} | "
        f"Teams: {', '.join(review.teams) or 'none'}",
    ]
    if review.ground_size:
        area = (
            f" | Walkable: {review.walkable_area:.1f} m2"
            if review.walkable_area is not None
            else ""
        )
        lines.append(
            f"Playable area: {review.ground_size[0]:.1f} x {review.ground_size[1]:.1f} m{area}"
        )
    lines.append(
        f"Route graph: {review.node_count} places, {review.edge_count} routes"
        + (" (topology inferred from sampled waypoints)" if review.graph_derived else "")
    )
    lines.extend(["", "Coverage:"])
    width = max(len(category.title) for category in Category)
    for item in review.coverage:
        state = f"{item.issue_count} finding(s)" if item.applicable else "not applicable"
        lines.append(f"  {item.category.title.ljust(width)}  {state} — {item.reason}")
    if review.unmeasured:
        lines.extend(
            [
                "",
                "Objects without usable extents (excluded from spatial measurement):",
                *(f"- {name}" for name in review.unmeasured[:20]),
            ]
        )
        if len(review.unmeasured) > 20:
            lines.append(f"- ... and {len(review.unmeasured) - 20} more")
    return "\n".join(lines)


def _coverage_reason(
    context: AnalysisContext, category: Category, applicable: bool
) -> str:
    """Explain in one line why a section applied or stood down."""
    scene = context.scene
    if category is Category.FAIRNESS and not applicable:
        return "no spawn points with two or more teams"
    if category is Category.PACING and not applicable:
        return "the scene declares no zones, so there is no stated intent to check"
    if category is Category.ENCOUNTER and not applicable:
        return "no combat or objective zone is declared"
    if category is Category.NAVIGATION and not applicable:
        return context.grid_reason
    if category is Category.FLOW and not applicable:
        return context.graph_reason
    if not applicable:
        return "nothing measurable in this scene"
    if category is Category.FLOW and context.graph is not None and context.graph.derived:
        return "measured against a topology inferred from walkable space"
    if category is Category.VISUAL:
        return "geometric proxies only; rendered views were not inspected here"
    if category is Category.FAIRNESS:
        return f"compared across {len(scene.teams)} teams"
    return "measured"
