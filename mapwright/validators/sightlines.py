"""Landmark visibility from the places a player actually stands.

Rays are cast from eye height at walkable viewpoints to a point on each
landmark and tested against every other object's world box. This measures
occlusion by geometry only: a landmark can be geometrically visible and still
fail to read, so the finding is a review note rather than a defect.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, replace
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3, segment_intersects_aabb
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.core.metrics import OccupancyGrid
from mapwright.core.scene_ir import MarkerPoint, SceneObject
from mapwright.validators.landmarks import LandmarkReport, ObjectSize, analyze_landmarks


_SEMANTIC_NAME = re.compile(
    r"(?:^|[_\-\s])(spawn|start|entry|entrance|checkpoint|vantage|sight)(?:$|[_\-\s])"
)
_ENTRANCE_NAME = re.compile(r"(?:^|[_\-\s])(spawn|start|entry|entrance)(?:$|[_\-\s])")


@dataclass(frozen=True)
class Viewpoint:
    """One eye position used as a ray origin."""

    name: str
    position: Vec3
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "name": self.name,
            "position": self.position.to_list(),
            "source": self.source,
        }


@dataclass(frozen=True)
class Sightline:
    """One tested segment from a viewpoint to a landmark."""

    viewpoint: str
    landmark_id: str
    landmark_name: str
    target: Vec3
    clear: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "viewpoint": self.viewpoint,
            "landmark": self.landmark_id,
            "landmark_name": self.landmark_name,
            "target": self.target.to_list(),
            "clear": self.clear,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class LandmarkVisibility:
    """How many viewpoints can see one landmark."""

    landmark_id: str
    landmark_name: str
    visible_from: int
    tested_from: int
    blockers: tuple[tuple[str, int], ...]
    flagged: bool

    @property
    def visibility_percentage(self) -> float:
        """Return the share of viewpoints with a clear line."""
        return self.visible_from / self.tested_from * 100.0 if self.tested_from else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "landmark": self.landmark_id,
            "landmark_name": self.landmark_name,
            "visible_from": self.visible_from,
            "tested_from": self.tested_from,
            "visibility_percentage": self.visibility_percentage,
            "blockers": [
                {"object": identifier, "rays": count}
                for identifier, count in self.blockers
            ],
            "flagged": self.flagged,
        }


@dataclass(frozen=True)
class SightlineReport:
    """Landmark visibility measurements for one scene."""

    issues: tuple[Issue, ...]
    scene: str
    available: bool
    unavailable_reason: str | None
    eye_height: float
    target_height_ratio: float
    requested_test_points: int
    viewpoints: tuple[Viewpoint, ...]
    visibility: tuple[LandmarkVisibility, ...]
    sightlines: tuple[Sightline, ...]

    @property
    def flagged_landmarks(self) -> tuple[LandmarkVisibility, ...]:
        """Return the landmarks no viewpoint can see."""
        return tuple(item for item in self.visibility if item.flagged)

    @property
    def blocked_rays(self) -> tuple[Sightline, ...]:
        """Return the segments that geometry interrupts."""
        return tuple(ray for ray in self.sightlines if not ray.clear)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "scene": self.scene,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "eye_height": self.eye_height,
            "target_height_ratio": self.target_height_ratio,
            "requested_test_points": self.requested_test_points,
            "viewpoints": [point.to_dict() for point in self.viewpoints],
            "visibility": [item.to_dict() for item in self.visibility],
            "sightlines": [ray.to_dict() for ray in self.sightlines],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_sightlines(context: AnalysisContext) -> SightlineReport:
    """Test whether each landmark candidate is visible from walkable ground."""
    thresholds = context.config.thresholds
    point_count = max(1, int(thresholds.sightline_test_points))
    grid = context.grid
    if grid is None:
        return _unavailable(context, context.grid_reason, point_count)

    landmarks = analyze_landmarks(context)
    if not landmarks.candidates:
        return _unavailable(
            context,
            "no object in the scene has resolvable dimensions, so there are no "
            "landmark candidates to look at",
            point_count,
        )

    free_cells = _main_region_cells(context, grid)
    if not free_cells:
        return _unavailable(
            context,
            "the walkable grid contains no connected free space to stand in",
            point_count,
        )

    eye_y = _eye_height(landmarks, context.config.eye_height)
    viewpoints = _automatic_viewpoints(context, grid, free_cells, eye_y, point_count)
    boxes = _world_boxes(context)

    sightlines: list[Sightline] = []
    for landmark in landmarks.candidates:
        target = _target_point(boxes[landmark.id], thresholds.target_height_ratio)
        for viewpoint in viewpoints:
            blockers = tuple(
                identifier
                for identifier, box in sorted(boxes.items())
                if identifier != landmark.id
                and segment_intersects_aabb(viewpoint.position, target, box)
            )
            sightlines.append(
                Sightline(
                    viewpoint=viewpoint.name,
                    landmark_id=landmark.id,
                    landmark_name=landmark.name,
                    target=target,
                    clear=not blockers,
                    blockers=blockers,
                )
            )

    visibility = tuple(
        _visibility(landmark, sightlines, len(viewpoints))
        for landmark in landmarks.candidates
    )
    report = SightlineReport(
        issues=(),
        scene=context.scene.scene,
        available=True,
        unavailable_reason=None,
        eye_height=context.config.eye_height,
        target_height_ratio=thresholds.target_height_ratio,
        requested_test_points=point_count,
        viewpoints=viewpoints,
        visibility=visibility,
        sightlines=tuple(sightlines),
    )
    return replace(report, issues=_issues(context, report))


def format_report(report: SightlineReport) -> str:
    """Format viewpoints, visibility, and blocked rays for a terminal."""
    if not report.available:
        return "\n".join(
            [
                f"Mapwright sightline report: {report.scene}",
                f"Not measured: {report.unavailable_reason}",
                *_findings(report.issues),
            ]
        )

    viewpoint_rows = [
        (
            point.name,
            f"({point.position.x:.2f}, {point.position.y:.2f}, {point.position.z:.2f})",
            point.source,
        )
        for point in report.viewpoints
    ]
    visibility_rows = [
        (
            item.landmark_name,
            item.landmark_id,
            f"{item.visible_from}/{item.tested_from}",
            f"{item.visibility_percentage:.1f}%",
            "FLAGGED: NEVER VISIBLE" if item.flagged else "VISIBLE",
        )
        for item in report.visibility
    ]
    lines = [
        f"Mapwright sightline report: {report.scene}",
        (
            f"Landmarks: {len(report.visibility)} | "
            f"Test points: {len(report.viewpoints)} | "
            f"Eye height: {report.eye_height:.2f} | "
            f"Target height: {report.target_height_ratio * 100:.0f}%"
        ),
        "",
        "Test points:",
        *_render_table(("Point", "Position", "Source"), viewpoint_rows),
        "",
        "Landmark visibility:",
        *_render_table(
            ("Landmark", "Id", "Clear", "Visible", "Status"), visibility_rows
        ),
    ]
    if report.blocked_rays:
        lines.extend(["", "Blocked rays:"])
        lines.extend(
            f"- {ray.viewpoint} -> {ray.landmark_name}: {', '.join(ray.blockers)}"
            for ray in report.blocked_rays
        )
    result = (
        "REVIEW: LANDMARK(S) NEVER VISIBLE"
        if report.flagged_landmarks
        else "ALL LANDMARKS VISIBLE FROM AT LEAST ONE TEST POINT"
    )
    lines.extend(["", f"Result: {result}"])
    lines.extend(_findings(report.issues))
    return "\n".join(lines)


def _issues(context: AnalysisContext, report: SightlineReport) -> tuple[Issue, ...]:
    """Return the visibility review notes this test supports."""
    collector = IssueCollector(Category.VISUAL)
    for item in report.flagged_landmarks:
        listed = ", ".join(
            f"'{identifier}' ({count} of {item.tested_from} rays)"
            for identifier, count in item.blockers[:3]
        )
        worst = item.blockers[0][0] if item.blockers else "the surrounding geometry"
        collector.add(
            "landmark_never_visible",
            context.config.severity_for("landmark_never_visible", Severity.WARNING),
            f"'{item.landmark_name}' is not visible from any test point",
            evidence=(
                f"'{item.landmark_name}' ({item.landmark_id}) was visible from "
                f"{item.visible_from} of {item.tested_from} walkable test points at "
                f"{report.eye_height:.2f} m eye height, aiming "
                f"{report.target_height_ratio * 100:.0f}% up its bounds. Blocked by: "
                f"{listed or 'other scene geometry'}."
            ),
            explanation=(
                "A landmark that no standing player can see cannot orient anyone; "
                "it only pays off from angles the level never puts them in. This "
                "tests boxes, not readability, so a landmark may still fail to "
                "register even when a ray reaches it."
            ),
            recommendation=(
                f"Lower, narrow, or move '{worst}', raise the landmark so its "
                "upper portion clears the blockers, or relocate the landmark to a "
                "spot the main approach routes can see."
            ),
            subjects=(item.landmark_id,),
            metrics=(
                ("visible_from", float(item.visible_from)),
                ("tested_from", float(item.tested_from)),
                ("visibility_percent", item.visibility_percentage),
                ("blockers", float(len(item.blockers))),
            ),
            advisory=True,
        )
    return collector.result()


def _visibility(
    landmark: ObjectSize, sightlines: Sequence[Sightline], tested: int
) -> LandmarkVisibility:
    """Aggregate every ray cast at one landmark."""
    rays = [ray for ray in sightlines if ray.landmark_id == landmark.id]
    counts: dict[str, int] = {}
    for ray in rays:
        for blocker in ray.blockers:
            counts[blocker] = counts.get(blocker, 0) + 1
    return LandmarkVisibility(
        landmark_id=landmark.id,
        landmark_name=landmark.name,
        visible_from=sum(ray.clear for ray in rays),
        tested_from=tested,
        blockers=tuple(
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        flagged=bool(rays) and not any(ray.clear for ray in rays),
    )


def _world_boxes(context: AnalysisContext) -> dict[str, Bounds]:
    """Return the world box of every object that can occlude a view."""
    return {
        obj.id: box
        for obj in context.scene.obstacles
        if (box := obj.world_bounds()) is not None
    }


def _target_point(box: Bounds, height_ratio: float) -> Vec3:
    """Return the aim point partway up a landmark's world box."""
    return Vec3(
        box.center.x,
        box.min.y + box.height * height_ratio,
        box.center.z,
    )


def _eye_height(landmarks: LandmarkReport, eye_height: float) -> float:
    """Return the world Y a standing player's eyes occupy."""
    heights = [obj.position.y for obj in landmarks.objects]
    base = statistics.median(heights) if heights else 0.0
    return base + eye_height


def _main_region_cells(
    context: AnalysisContext, grid: OccupancyGrid
) -> tuple[tuple[int, int, Vec3], ...]:
    """Return the free cells of the largest connected region."""
    labels, regions = grid.connected_regions(
        context.config.thresholds.navigation_minimum_region_area
    )
    if not regions:
        return ()
    main = max(regions, key=lambda region: region.cell_count).component_id
    return tuple(
        (column, row, grid.cell_center(column, row))
        for row, label_row in enumerate(labels)
        for column, component_id in enumerate(label_row)
        if component_id == main
    )


def _automatic_viewpoints(
    context: AnalysisContext,
    grid: OccupancyGrid,
    free_cells: Sequence[tuple[int, int, Vec3]],
    eye_y: float,
    point_count: int,
) -> tuple[Viewpoint, ...]:
    """Choose walkable viewpoints: declared markers first, then coverage.

    Declared entries, spawns, and objectives are where players really stand.
    Scenes imported from engines that carry no such declarations fall back to
    name-matched markers, then to the centre of the playable area, and finally
    to the points furthest from everything already chosen.
    """
    anchors = _marker_anchors(context.scene.entry_points, "declared entry point")
    anchors += _marker_anchors(context.scene.spawn_points, "declared spawn point")
    anchors += _marker_anchors(context.scene.objectives, "declared objective")
    if not anchors:
        anchors = _named_anchors(context.scene.objects)
    center = grid.bounds.center
    anchors = anchors + (
        ("center", Vec3(center.x, eye_y, center.z), "ground centre fallback"),
    )

    selected: list[Viewpoint] = []
    used: set[tuple[int, int]] = set()
    for name, anchor, source in anchors:
        column, row, snapped = min(
            free_cells,
            key=lambda cell: (
                (cell[2].x - anchor.x) ** 2 + (cell[2].z - anchor.z) ** 2,
                cell[0],
                cell[1],
            ),
        )
        if (column, row) in used:
            continue
        used.add((column, row))
        selected.append(Viewpoint(name, Vec3(snapped.x, eye_y, snapped.z), source))
        if len(selected) >= point_count:
            return tuple(selected)

    while len(selected) < point_count and len(used) < len(free_cells):
        column, row, point = max(
            (cell for cell in free_cells if (cell[0], cell[1]) not in used),
            key=lambda cell: (
                min(
                    (cell[2].x - chosen.position.x) ** 2
                    + (cell[2].z - chosen.position.z) ** 2
                    for chosen in selected
                ),
                -cell[0],
                -cell[1],
            ),
        )
        used.add((column, row))
        selected.append(
            Viewpoint(
                f"coverage-{len(selected) + 1}",
                Vec3(point.x, eye_y, point.z),
                "main-region farthest-point fill",
            )
        )
    return tuple(selected)


def _marker_anchors(
    markers: Sequence[MarkerPoint], source: str
) -> tuple[tuple[str, Vec3, str], ...]:
    """Return anchors for declared points of interest."""
    return tuple(
        (f"{marker.kind.value}:{marker.label}", marker.position, source)
        for marker in markers
    )


def _named_anchors(
    objects: Sequence[SceneObject],
) -> tuple[tuple[str, Vec3, str], ...]:
    """Return anchors inferred from object names when nothing is declared."""
    semantic = [
        obj
        for obj in objects
        if not obj.obstructs and _SEMANTIC_NAME.search(_normalize(obj.name))
    ]
    if semantic:
        return tuple(
            (f"marker:{obj.name}", obj.position, "name-matched marker")
            for obj in semantic
        )
    entrance = next(
        (obj for obj in objects if _ENTRANCE_NAME.search(_normalize(obj.name))), None
    )
    if entrance is not None:
        return (
            (
                f"entrance:{entrance.name}",
                entrance.position,
                "named entrance fallback",
            ),
        )
    return ()


def _normalize(name: str) -> str:
    """Return a name with camel case split into underscore-separated words."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _unavailable(
    context: AnalysisContext, reason: str, point_count: int
) -> SightlineReport:
    """Return a report explaining why visibility could not be measured."""
    collector = IssueCollector(Category.VISUAL)
    collector.add(
        "sightlines_unavailable",
        context.config.severity_for("sightlines_unavailable", Severity.INFO),
        "Landmark visibility was not measured",
        evidence=(
            f"Sightline analysis needs walkable ground and measurable landmarks, "
            f"but {reason.rstrip('.')}; "
            f"{_plural(len(context.scene.props), 'placed object')} went untested."
        ),
        explanation=(
            "Visibility is measured from the places a player can stand, so "
            "without walkable space or measurable geometry there is no view to "
            "test."
        ),
        recommendation=(
            "Declare a ground extent and give the intended landmarks explicit "
            "dimensions, then rerun the sightline analysis."
        ),
        metrics=(("props", float(len(context.scene.props))),),
    )
    return SightlineReport(
        issues=collector.result(),
        scene=context.scene.scene,
        available=False,
        unavailable_reason=reason,
        eye_height=context.config.eye_height,
        target_height_ratio=context.config.thresholds.target_height_ratio,
        requested_test_points=point_count,
        viewpoints=(),
        visibility=(),
        sightlines=(),
    )


def _plural(count: int, singular: str) -> str:
    """Return a count and its noun, pluralized with a trailing 's'."""
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


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
