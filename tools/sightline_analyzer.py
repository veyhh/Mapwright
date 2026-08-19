"""Estimate static 3D sightlines from viewpoints to landmark candidates."""

from __future__ import annotations

import argparse
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

if __package__:
    from tools.landmark_analyzer import (
        LandmarkParseError,
        ObjectSize,
        analyze_landmarks,
    )
    from tools.navigation_analyzer import (
        NavigationParseError,
        NavigationReport,
        analyze_navigation,
    )
    from tools.spacing_analyzer import (
        NodePosition,
        SpacingParseError,
        Vector3,
        parse_scene_positions,
    )
else:
    from landmark_analyzer import LandmarkParseError, ObjectSize, analyze_landmarks
    from navigation_analyzer import (
        NavigationParseError,
        NavigationReport,
        analyze_navigation,
    )
    from spacing_analyzer import (
        NodePosition,
        SpacingParseError,
        Vector3,
        parse_scene_positions,
    )


DEFAULT_EYE_HEIGHT = 1.6
DEFAULT_TARGET_HEIGHT_RATIO = 0.6
DEFAULT_TEST_POINT_COUNT = 5
_POINT_NAME_PATTERN = re.compile(
    r"(?:^|[_\-\s])(spawn|start|entry|entrance|checkpoint|vantage|sight)(?:$|[_\-\s])"
)
_ENTRANCE_NAME_PATTERN = re.compile(
    r"(?:^|[_\-\s])(spawn|start|entry|entrance)(?:$|[_\-\s])"
)


class SightlineParseError(ValueError):
    """Raised when static sightline geometry cannot be constructed."""


@dataclass(frozen=True)
class SightPoint:
    """One eye position used as a ray origin."""

    name: str
    position: Vector3
    source: str


@dataclass(frozen=True)
class WorldAABB:
    """Conservative world-axis-aligned bounds for a prop."""

    name: str
    node_path: str
    minimum: Vector3
    maximum: Vector3

    @property
    def center(self) -> Vector3:
        return Vector3(
            (self.minimum.x + self.maximum.x) * 0.5,
            (self.minimum.y + self.maximum.y) * 0.5,
            (self.minimum.z + self.maximum.z) * 0.5,
        )


@dataclass(frozen=True)
class Sightline:
    """One tested segment from an eye point to a landmark."""

    viewpoint: SightPoint
    landmark: ObjectSize
    target: Vector3
    clear: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class LandmarkVisibility:
    """Aggregated visibility result for one landmark candidate."""

    landmark: ObjectSize
    visible_from: int
    tested_from: int
    flagged: bool

    @property
    def visibility_percentage(self) -> float:
        return self.visible_from / self.tested_from * 100 if self.tested_from else 0.0


@dataclass(frozen=True)
class SightlineReport:
    """Complete landmark visibility analysis for a scene."""

    scene_path: str
    eye_height: float
    target_height_ratio: float
    viewpoints: tuple[SightPoint, ...]
    landmarks: tuple[ObjectSize, ...]
    bounds: tuple[WorldAABB, ...]
    sightlines: tuple[Sightline, ...]
    visibility: tuple[LandmarkVisibility, ...]

    @property
    def flagged_landmarks(self) -> tuple[LandmarkVisibility, ...]:
        return tuple(item for item in self.visibility if item.flagged)


def _normalize_name(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _world_aabb(obj: ObjectSize, node: NodePosition) -> WorldAABB:
    """Transform a base-aligned local prop box into a conservative world AABB."""
    dimensions = obj.base_dimensions
    local_center = Vector3(0.0, dimensions.y * 0.5, 0.0)
    center = (
        node.position
        + node.basis_x * local_center.x
        + node.basis_y * local_center.y
        + node.basis_z * local_center.z
    )
    half_x = 0.5 * (
        abs(node.basis_x.x) * dimensions.x
        + abs(node.basis_y.x) * dimensions.y
        + abs(node.basis_z.x) * dimensions.z
    )
    half_y = 0.5 * (
        abs(node.basis_x.y) * dimensions.x
        + abs(node.basis_y.y) * dimensions.y
        + abs(node.basis_z.y) * dimensions.z
    )
    half_z = 0.5 * (
        abs(node.basis_x.z) * dimensions.x
        + abs(node.basis_y.z) * dimensions.y
        + abs(node.basis_z.z) * dimensions.z
    )
    return WorldAABB(
        name=obj.name,
        node_path=obj.node_path,
        minimum=Vector3(center.x - half_x, center.y - half_y, center.z - half_z),
        maximum=Vector3(center.x + half_x, center.y + half_y, center.z + half_z),
    )


def _segment_intersects_aabb(
    start: Vector3,
    end: Vector3,
    bounds: WorldAABB,
    endpoint_epsilon: float = 1e-6,
) -> bool:
    """Return whether the open segment intersects an AABB using slab clipping."""
    direction = Vector3(end.x - start.x, end.y - start.y, end.z - start.z)
    minimum_t = 0.0
    maximum_t = 1.0
    for origin, delta, lower, upper in (
        (start.x, direction.x, bounds.minimum.x, bounds.maximum.x),
        (start.y, direction.y, bounds.minimum.y, bounds.maximum.y),
        (start.z, direction.z, bounds.minimum.z, bounds.maximum.z),
    ):
        if math.isclose(delta, 0.0, abs_tol=1e-12):
            if origin < lower or origin > upper:
                return False
            continue
        first = (lower - origin) / delta
        second = (upper - origin) / delta
        if first > second:
            first, second = second, first
        minimum_t = max(minimum_t, first)
        maximum_t = min(maximum_t, second)
        if minimum_t > maximum_t:
            return False
    return maximum_t > endpoint_epsilon and minimum_t < 1.0 - endpoint_epsilon


def _grid_center(
    report: NavigationReport, column: int, row: int, eye_y: float
) -> Vector3:
    return Vector3(
        report.navigable_min_x + (column + 0.5) * report.actual_cell_width,
        eye_y,
        report.navigable_min_z + (row + 0.5) * report.actual_cell_depth,
    )


def _main_free_cells(
    report: NavigationReport, eye_y: float
) -> list[tuple[int, int, Vector3]]:
    if report.main_component_id is None:
        return []
    return [
        (column, row, _grid_center(report, column, row, eye_y))
        for row, label_row in enumerate(report.labels)
        for column, component_id in enumerate(label_row)
        if component_id == report.main_component_id
    ]


def _nearest_free_point(
    anchor: Vector3,
    free_cells: Sequence[tuple[int, int, Vector3]],
) -> tuple[int, int, Vector3]:
    return min(
        free_cells,
        key=lambda cell: (cell[2].x - anchor.x) ** 2 + (cell[2].z - anchor.z) ** 2,
    )


def _automatic_viewpoints(
    navigation: NavigationReport,
    positions: Sequence[NodePosition],
    objects: Sequence[ObjectSize],
    eye_height: float,
    point_count: int,
) -> tuple[SightPoint, ...]:
    base_y = statistics.median(obj.position.y for obj in objects)
    eye_y = base_y + eye_height
    free_cells = _main_free_cells(navigation, eye_y)
    if not free_cells:
        raise SightlineParseError("Navigation analysis found no main free-space region.")

    semantic_nodes = [
        node
        for node in positions
        if _POINT_NAME_PATTERN.search(_normalize_name(node.name))
        and not node.analyzed
    ]
    entrance_nodes = [
        node
        for node in positions
        if _ENTRANCE_NAME_PATTERN.search(_normalize_name(node.name))
    ]
    anchors: list[tuple[str, Vector3, str]] = []
    anchors.extend(
        (f"marker:{node.name}", node.position, "semantic marker")
        for node in semantic_nodes
    )
    if not semantic_nodes and entrance_nodes:
        entrance = entrance_nodes[0]
        anchors.append(
            (f"entrance:{entrance.name}", entrance.position, "named entrance fallback")
        )
    anchors.append(
        (
            "center",
            Vector3(
                (navigation.navigable_min_x + navigation.navigable_max_x) * 0.5,
                eye_y,
                (navigation.navigable_min_z + navigation.navigable_max_z) * 0.5,
            ),
            "ground center fallback",
        )
    )

    selected: list[tuple[int, int, SightPoint]] = []
    used_cells: set[tuple[int, int]] = set()
    for name, anchor, source in anchors:
        column, row, snapped = _nearest_free_point(anchor, free_cells)
        if (column, row) in used_cells:
            continue
        used_cells.add((column, row))
        selected.append((column, row, SightPoint(name, snapped, source)))
        if len(selected) >= point_count:
            return tuple(item[2] for item in selected)

    while len(selected) < point_count and len(used_cells) < len(free_cells):
        column, row, point = max(
            (cell for cell in free_cells if (cell[0], cell[1]) not in used_cells),
            key=lambda cell: min(
                (cell[2].x - chosen[2].position.x) ** 2
                + (cell[2].z - chosen[2].position.z) ** 2
                for chosen in selected
            ),
        )
        used_cells.add((column, row))
        selected.append(
            (
                column,
                row,
                SightPoint(
                    f"coverage-{len(selected) + 1}",
                    point,
                    "main-region farthest-point fallback",
                ),
            )
        )
    return tuple(item[2] for item in selected)


def _target_point(bounds: WorldAABB, height_ratio: float) -> Vector3:
    return Vector3(
        (bounds.minimum.x + bounds.maximum.x) * 0.5,
        bounds.minimum.y + (bounds.maximum.y - bounds.minimum.y) * height_ratio,
        (bounds.minimum.z + bounds.maximum.z) * 0.5,
    )


def analyze_sightlines(
    path: str,
    viewpoints: Sequence[SightPoint] | None = None,
    eye_height: float = DEFAULT_EYE_HEIGHT,
    target_height_ratio: float = DEFAULT_TARGET_HEIGHT_RATIO,
    test_point_count: int = DEFAULT_TEST_POINT_COUNT,
) -> SightlineReport:
    """Test static segments from viewpoints to size-based landmark candidates."""
    if not math.isfinite(eye_height) or eye_height <= 0:
        raise ValueError("Eye height must be a positive finite number.")
    if not math.isfinite(target_height_ratio) or not 0 < target_height_ratio < 1:
        raise ValueError("Target height ratio must be between 0 and 1.")
    if test_point_count <= 0:
        raise ValueError("Test point count must be positive.")

    scene_path = Path(path).expanduser()
    if scene_path.suffix.lower() != ".tscn":
        raise ValueError(f"Expected a .tscn scene file: {scene_path}")
    try:
        scene_text = scene_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Godot scene file not found: {scene_path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"Godot scene is not a UTF-8 text scene: {scene_path}") from exc

    try:
        positions = parse_scene_positions(scene_text)
        landmark_report = analyze_landmarks(str(scene_path))
        navigation_report = analyze_navigation(str(scene_path))
    except (SpacingParseError, LandmarkParseError, NavigationParseError) as exc:
        raise SightlineParseError(str(exc)) from exc
    if landmark_report.unresolved:
        names = ", ".join(item.name for item in landmark_report.unresolved)
        raise SightlineParseError(
            f"Sightline analysis requires measurable bounds; unresolved: {names}"
        )

    positions_by_path = {node.node_path: node for node in positions}
    objects_by_path = {obj.node_path: obj for obj in landmark_report.objects}
    missing = sorted(set(objects_by_path).difference(positions_by_path))
    if missing:
        raise SightlineParseError(
            "Measured props have no resolved transform: " + ", ".join(missing)
        )
    bounds = tuple(
        _world_aabb(obj, positions_by_path[obj.node_path])
        for obj in landmark_report.objects
    )
    bounds_by_path = {item.node_path: item for item in bounds}
    landmark_candidates = landmark_report.candidates
    if not landmark_candidates:
        raise SightlineParseError("Landmark analyzer produced no candidates.")

    selected_viewpoints = tuple(viewpoints or ())
    if not selected_viewpoints:
        selected_viewpoints = _automatic_viewpoints(
            navigation_report,
            positions,
            landmark_report.objects,
            eye_height,
            test_point_count,
        )

    sightlines: list[Sightline] = []
    for landmark in landmark_candidates:
        target = _target_point(
            bounds_by_path[landmark.node_path], target_height_ratio
        )
        for viewpoint in selected_viewpoints:
            blockers = tuple(
                bounds_item.name
                for bounds_item in bounds
                if bounds_item.node_path != landmark.node_path
                and _segment_intersects_aabb(
                    viewpoint.position, target, bounds_item
                )
            )
            sightlines.append(
                Sightline(
                    viewpoint=viewpoint,
                    landmark=landmark,
                    target=target,
                    clear=not blockers,
                    blockers=blockers,
                )
            )

    visibility = tuple(
        LandmarkVisibility(
            landmark=landmark,
            visible_from=sum(
                ray.clear
                for ray in sightlines
                if ray.landmark.node_path == landmark.node_path
            ),
            tested_from=len(selected_viewpoints),
            flagged=not any(
                ray.clear for ray in sightlines if ray.landmark.node_path == landmark.node_path
            ),
        )
        for landmark in landmark_candidates
    )
    return SightlineReport(
        scene_path=str(scene_path),
        eye_height=eye_height,
        target_height_ratio=target_height_ratio,
        viewpoints=selected_viewpoints,
        landmarks=landmark_candidates,
        bounds=bounds,
        sightlines=tuple(sightlines),
        visibility=visibility,
    )


def _format_table(rows: Sequence[tuple[str, ...]], headers: tuple[str, ...]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    template = "  ".join(f"{{:{width}}}" for width in widths)
    return [
        template.format(*headers),
        template.format(*(char * width for char, width in zip("-----", widths))),
        *(template.format(*row) for row in rows),
    ]


def format_report(report: SightlineReport) -> str:
    """Format landmark visibility and blocked rays for a terminal."""
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
            item.landmark.name,
            f"{item.visible_from}/{item.tested_from}",
            f"{item.visibility_percentage:.1f}%",
            "FLAGGED: NEVER VISIBLE" if item.flagged else "VISIBLE",
        )
        for item in report.visibility
    ]
    blocked_rays = [ray for ray in report.sightlines if not ray.clear]
    lines = [
        f"Mapwright sightline report: {report.scene_path}",
        (
            f"Landmarks: {len(report.landmarks)} | Test points: {len(report.viewpoints)} | "
            f"Eye height: {report.eye_height:.2f} | "
            f"Target height: {report.target_height_ratio * 100:.0f}%"
        ),
        "",
        "Test points:",
        *_format_table(viewpoint_rows, ("Point", "Position", "Source")),
        "",
        "Landmark visibility:",
        *_format_table(visibility_rows, ("Landmark", "Clear", "Visible", "Status")),
    ]
    if blocked_rays:
        lines.extend(["", "Blocked rays:"])
        lines.extend(
            f"- {ray.viewpoint.name} -> {ray.landmark.name}: {', '.join(ray.blockers)}"
            for ray in blocked_rays
        )
    result = (
        "WARNING: LANDMARK(S) NEVER VISIBLE"
        if report.flagged_landmarks
        else "ALL LANDMARKS VISIBLE FROM AT LEAST ONE TEST POINT"
    )
    lines.extend(["", f"Result: {result}"])
    return "\n".join(lines)


def _parse_cli_point(value: str) -> Vector3:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("point must use X,Y,Z format")
    try:
        point = Vector3(*(float(part) for part in parts))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("point coordinates must be numbers") from exc
    if not all(math.isfinite(component) for component in (point.x, point.y, point.z)):
        raise argparse.ArgumentTypeError("point coordinates must be finite")
    return point


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Estimate static 3D sightlines to landmark candidates."
    )
    parser.add_argument("scene", help="Path to the Godot 4 .tscn scene")
    parser.add_argument(
        "--point",
        type=_parse_cli_point,
        action="append",
        default=[],
        metavar="X,Y,Z",
        help="Explicit eye position; repeat for multiple test points",
    )
    parser.add_argument(
        "--eye-height",
        type=float,
        default=DEFAULT_EYE_HEIGHT,
        help=f"Automatic viewpoint eye height (default: {DEFAULT_EYE_HEIGHT:g})",
    )
    parser.add_argument(
        "--target-height-ratio",
        type=float,
        default=DEFAULT_TARGET_HEIGHT_RATIO,
        help=(
            "Vertical target point within landmark bounds "
            f"(default: {DEFAULT_TARGET_HEIGHT_RATIO:g})"
        ),
    )
    parser.add_argument(
        "--test-point-count",
        type=int,
        default=DEFAULT_TEST_POINT_COUNT,
        help=f"Automatic test point count (default: {DEFAULT_TEST_POINT_COUNT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sightline analyzer CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    viewpoints = tuple(
        SightPoint(f"cli-{index}", point, "CLI")
        for index, point in enumerate(args.point, 1)
    )
    try:
        report = analyze_sightlines(
            args.scene,
            viewpoints=viewpoints or None,
            eye_height=args.eye_height,
            target_height_ratio=args.target_height_ratio,
            test_point_count=args.test_point_count,
        )
    except (SightlineParseError, FileNotFoundError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
