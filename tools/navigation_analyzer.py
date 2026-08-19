"""Estimate top-down navigability in Godot 4 text scenes."""

from __future__ import annotations

import argparse
import math
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

if __package__:
    from tools.density_analyzer import DensityParseError, GroundBounds, parse_ground_bounds
    from tools.landmark_analyzer import (
        LandmarkParseError,
        ObjectSize,
        analyze_landmarks,
    )
    from tools.spacing_analyzer import (
        NodePosition,
        SpacingParseError,
        parse_scene_positions,
    )
else:
    from density_analyzer import DensityParseError, GroundBounds, parse_ground_bounds
    from landmark_analyzer import LandmarkParseError, ObjectSize, analyze_landmarks
    from spacing_analyzer import NodePosition, SpacingParseError, parse_scene_positions


DEFAULT_MINIMUM_PATH_WIDTH = 1.0
DEFAULT_CELL_SIZE = 0.25
DEFAULT_MINIMUM_REGION_AREA = 1.0
MAXIMUM_GRID_CELLS = 2_000_000
_BLOCKED = -2
_UNVISITED = -1


class NavigationParseError(ValueError):
    """Raised when a static navigation estimate cannot be constructed."""


@dataclass(frozen=True)
class ObstacleFootprint:
    """Axis-aligned XZ footprint of one transformed prop."""

    name: str
    node_path: str
    center_x: float
    center_z: float
    half_width: float
    half_depth: float


@dataclass(frozen=True)
class NavigationRegion:
    """One connected free-space component in the occupancy grid."""

    component_id: int
    cell_count: int
    area: float
    min_x: float
    max_x: float
    min_z: float
    max_z: float
    significant: bool


@dataclass(frozen=True)
class ObjectApproach:
    """Nearest free-space component to one obstacle."""

    name: str
    node_path: str
    component_id: int | None
    distance: float | None
    in_main_region: bool


@dataclass(frozen=True)
class NavigationReport:
    """Complete occupancy-grid navigation estimate for a scene."""

    scene_path: str
    minimum_path_width: float
    requested_cell_size: float
    minimum_region_area: float
    ground: GroundBounds
    navigable_min_x: float
    navigable_max_x: float
    navigable_min_z: float
    navigable_max_z: float
    columns: int
    rows: int
    actual_cell_width: float
    actual_cell_depth: float
    obstacles: tuple[ObstacleFootprint, ...]
    labels: tuple[tuple[int, ...], ...]
    regions: tuple[NavigationRegion, ...]
    main_component_id: int | None
    approaches: tuple[ObjectApproach, ...]
    blocked_cell_count: int
    free_cell_count: int
    disconnected: bool
    no_navigable_area: bool
    clearance_sensitive: bool

    @property
    def cell_area(self) -> float:
        return self.actual_cell_width * self.actual_cell_depth

    @property
    def significant_regions(self) -> tuple[NavigationRegion, ...]:
        return tuple(region for region in self.regions if region.significant)

    @property
    def main_region(self) -> NavigationRegion | None:
        return next(
            (
                region
                for region in self.regions
                if region.component_id == self.main_component_id
            ),
            None,
        )

    @property
    def reachable_ratio(self) -> float:
        main = self.main_region
        if main is None or self.free_cell_count == 0:
            return 0.0
        return main.cell_count / self.free_cell_count


def _obstacle_footprint(obj: ObjectSize, node: NodePosition) -> ObstacleFootprint:
    """Project a transformed local box to a conservative world-XZ AABB."""
    dimensions = obj.base_dimensions
    half_width = 0.5 * (
        abs(node.basis_x.x) * dimensions.x
        + abs(node.basis_y.x) * dimensions.y
        + abs(node.basis_z.x) * dimensions.z
    )
    half_depth = 0.5 * (
        abs(node.basis_x.z) * dimensions.x
        + abs(node.basis_y.z) * dimensions.y
        + abs(node.basis_z.z) * dimensions.z
    )
    return ObstacleFootprint(
        name=obj.name,
        node_path=obj.node_path,
        center_x=obj.position.x,
        center_z=obj.position.z,
        half_width=half_width,
        half_depth=half_depth,
    )


def _cell_center(
    column: int,
    row: int,
    minimum_x: float,
    minimum_z: float,
    cell_width: float,
    cell_depth: float,
) -> tuple[float, float]:
    return (
        minimum_x + (column + 0.5) * cell_width,
        minimum_z + (row + 0.5) * cell_depth,
    )


def _rasterize_obstacles(
    obstacles: Sequence[ObstacleFootprint],
    columns: int,
    rows: int,
    minimum_x: float,
    minimum_z: float,
    cell_width: float,
    cell_depth: float,
    agent_radius: float,
) -> list[list[bool]]:
    blocked = [[False for _ in range(columns)] for _ in range(rows)]
    padding_x = agent_radius
    padding_z = agent_radius
    for row in range(rows):
        for column in range(columns):
            center_x, center_z = _cell_center(
                column,
                row,
                minimum_x,
                minimum_z,
                cell_width,
                cell_depth,
            )
            blocked[row][column] = any(
                abs(center_x - obstacle.center_x)
                <= obstacle.half_width + padding_x
                and abs(center_z - obstacle.center_z)
                <= obstacle.half_depth + padding_z
                for obstacle in obstacles
            )
    return blocked


def _neighbors(
    column: int,
    row: int,
    columns: int,
    rows: int,
    blocked: Sequence[Sequence[bool]],
) -> Sequence[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for delta_row in (-1, 0, 1):
        for delta_column in (-1, 0, 1):
            if delta_column == 0 and delta_row == 0:
                continue
            next_column = column + delta_column
            next_row = row + delta_row
            if not (0 <= next_column < columns and 0 <= next_row < rows):
                continue
            if blocked[next_row][next_column]:
                continue
            if delta_column and delta_row:
                if (
                    blocked[row][column + delta_column]
                    or blocked[row + delta_row][column]
                ):
                    continue
            result.append((next_column, next_row))
    return result


def _connected_regions(
    blocked: Sequence[Sequence[bool]],
    minimum_x: float,
    minimum_z: float,
    cell_width: float,
    cell_depth: float,
    minimum_region_area: float,
) -> tuple[list[list[int]], tuple[NavigationRegion, ...]]:
    rows = len(blocked)
    columns = len(blocked[0]) if rows else 0
    labels = [
        [_BLOCKED if blocked[row][column] else _UNVISITED for column in range(columns)]
        for row in range(rows)
    ]
    regions: list[NavigationRegion] = []
    cell_area = cell_width * cell_depth

    for start_row in range(rows):
        for start_column in range(columns):
            if labels[start_row][start_column] != _UNVISITED:
                continue
            component_id = len(regions)
            labels[start_row][start_column] = component_id
            pending = deque([(start_column, start_row)])
            cells: list[tuple[int, int]] = []

            while pending:
                column, row = pending.popleft()
                cells.append((column, row))
                for next_column, next_row in _neighbors(
                    column, row, columns, rows, blocked
                ):
                    if labels[next_row][next_column] != _UNVISITED:
                        continue
                    labels[next_row][next_column] = component_id
                    pending.append((next_column, next_row))

            min_column = min(column for column, _ in cells)
            max_column = max(column for column, _ in cells)
            min_row = min(row for _, row in cells)
            max_row = max(row for _, row in cells)
            area = len(cells) * cell_area
            regions.append(
                NavigationRegion(
                    component_id=component_id,
                    cell_count=len(cells),
                    area=area,
                    min_x=minimum_x + min_column * cell_width,
                    max_x=minimum_x + (max_column + 1) * cell_width,
                    min_z=minimum_z + min_row * cell_depth,
                    max_z=minimum_z + (max_row + 1) * cell_depth,
                    significant=area >= minimum_region_area,
                )
            )
    return labels, tuple(regions)


def _object_approaches(
    obstacles: Sequence[ObstacleFootprint],
    labels: Sequence[Sequence[int]],
    minimum_x: float,
    minimum_z: float,
    cell_width: float,
    cell_depth: float,
    main_component_id: int | None,
) -> tuple[ObjectApproach, ...]:
    free_cells = [
        (
            column,
            row,
            component_id,
            *_cell_center(
                column,
                row,
                minimum_x,
                minimum_z,
                cell_width,
                cell_depth,
            ),
        )
        for row, label_row in enumerate(labels)
        for column, component_id in enumerate(label_row)
        if component_id >= 0
    ]
    approaches: list[ObjectApproach] = []
    for obstacle in obstacles:
        if not free_cells:
            approaches.append(
                ObjectApproach(obstacle.name, obstacle.node_path, None, None, False)
            )
            continue
        nearest = min(
            free_cells,
            key=lambda cell: (
                max(abs(cell[3] - obstacle.center_x) - obstacle.half_width, 0.0)
                ** 2
                + max(abs(cell[4] - obstacle.center_z) - obstacle.half_depth, 0.0)
                ** 2
            ),
        )
        distance = math.sqrt(
            max(abs(nearest[3] - obstacle.center_x) - obstacle.half_width, 0.0)
            ** 2
            + max(abs(nearest[4] - obstacle.center_z) - obstacle.half_depth, 0.0)
            ** 2
        )
        approaches.append(
            ObjectApproach(
                name=obstacle.name,
                node_path=obstacle.node_path,
                component_id=nearest[2],
                distance=distance,
                in_main_region=nearest[2] == main_component_id,
            )
        )
    return tuple(approaches)


def analyze_navigation(
    path: str,
    minimum_path_width: float = DEFAULT_MINIMUM_PATH_WIDTH,
    cell_size: float = DEFAULT_CELL_SIZE,
    minimum_region_area: float = DEFAULT_MINIMUM_REGION_AREA,
) -> NavigationReport:
    """Estimate continuous traversable space using a top-down occupancy grid."""
    for value, label in (
        (minimum_path_width, "Minimum path width"),
        (cell_size, "Cell size"),
        (minimum_region_area, "Minimum region area"),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be a positive finite number.")
    if cell_size > minimum_path_width:
        raise ValueError("Cell size must not exceed the minimum path width.")

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
        ground = parse_ground_bounds(scene_text, positions)
        landmark_report = analyze_landmarks(str(scene_path))
    except (SpacingParseError, DensityParseError, LandmarkParseError) as exc:
        raise NavigationParseError(str(exc)) from exc
    if landmark_report.unresolved:
        names = ", ".join(item.name for item in landmark_report.unresolved)
        raise NavigationParseError(
            f"Navigation analysis requires measurable footprints; unresolved: {names}"
        )

    positions_by_path = {node.node_path: node for node in positions}
    obstacles = tuple(
        _obstacle_footprint(obj, positions_by_path[obj.node_path])
        for obj in landmark_report.objects
        if obj.node_path in positions_by_path
    )
    if len(obstacles) != len(landmark_report.objects):
        raise NavigationParseError("One or more measured props have no resolved transform.")

    agent_radius = minimum_path_width * 0.5
    minimum_x = ground.min_x + agent_radius
    maximum_x = ground.max_x - agent_radius
    minimum_z = ground.min_z + agent_radius
    maximum_z = ground.max_z - agent_radius
    if maximum_x <= minimum_x or maximum_z <= minimum_z:
        raise NavigationParseError(
            "Ground is smaller than the requested minimum path width."
        )

    columns = max(1, math.ceil((maximum_x - minimum_x) / cell_size))
    rows = max(1, math.ceil((maximum_z - minimum_z) / cell_size))
    if columns * rows > MAXIMUM_GRID_CELLS:
        raise ValueError(
            f"Occupancy grid would contain {columns * rows:,} cells; "
            "increase --cell-size."
        )
    actual_width = (maximum_x - minimum_x) / columns
    actual_depth = (maximum_z - minimum_z) / rows
    blocked = _rasterize_obstacles(
        obstacles,
        columns,
        rows,
        minimum_x,
        minimum_z,
        actual_width,
        actual_depth,
        agent_radius,
    )
    labels, regions = _connected_regions(
        blocked,
        minimum_x,
        minimum_z,
        actual_width,
        actual_depth,
        minimum_region_area,
    )
    significant = tuple(region for region in regions if region.significant)
    main_region = max(regions, key=lambda region: region.cell_count) if regions else None
    main_component_id = main_region.component_id if main_region is not None else None
    approaches = _object_approaches(
        obstacles,
        labels,
        minimum_x,
        minimum_z,
        actual_width,
        actual_depth,
        main_component_id,
    )
    blocked_count = sum(sum(row) for row in blocked)
    free_count = columns * rows - blocked_count
    no_navigable_area = not significant
    disconnected = len(significant) > 1
    conservative_blocked = _rasterize_obstacles(
        obstacles,
        columns,
        rows,
        minimum_x,
        minimum_z,
        actual_width,
        actual_depth,
        agent_radius + max(actual_width, actual_depth) * 0.5,
    )
    _, conservative_regions = _connected_regions(
        conservative_blocked,
        minimum_x,
        minimum_z,
        actual_width,
        actual_depth,
        minimum_region_area,
    )
    conservative_significant = tuple(
        region for region in conservative_regions if region.significant
    )
    clearance_sensitive = (
        not no_navigable_area
        and not disconnected
        and len(conservative_significant) != 1
    )
    return NavigationReport(
        scene_path=str(scene_path),
        minimum_path_width=minimum_path_width,
        requested_cell_size=cell_size,
        minimum_region_area=minimum_region_area,
        ground=ground,
        navigable_min_x=minimum_x,
        navigable_max_x=maximum_x,
        navigable_min_z=minimum_z,
        navigable_max_z=maximum_z,
        columns=columns,
        rows=rows,
        actual_cell_width=actual_width,
        actual_cell_depth=actual_depth,
        obstacles=obstacles,
        labels=tuple(tuple(row) for row in labels),
        regions=regions,
        main_component_id=main_component_id,
        approaches=approaches,
        blocked_cell_count=blocked_count,
        free_cell_count=free_count,
        disconnected=disconnected,
        no_navigable_area=no_navigable_area,
        clearance_sensitive=clearance_sensitive,
    )


def _render_map(report: NavigationReport, max_width: int = 64, max_height: int = 32) -> list[str]:
    step = max(
        1,
        math.ceil(report.columns / max_width),
        math.ceil(report.rows / max_height),
    )
    significant_ids = {
        region.component_id
        for region in report.significant_regions
        if region.component_id != report.main_component_id
    }
    disconnected_characters = {
        component_id: chr(ord("A") + index % 26)
        for index, component_id in enumerate(sorted(significant_ids))
    }
    rendered: list[str] = []
    for row_start in reversed(range(0, report.rows, step)):
        characters = []
        for column_start in range(0, report.columns, step):
            values = [
                report.labels[row][column]
                for row in range(row_start, min(row_start + step, report.rows))
                for column in range(
                    column_start, min(column_start + step, report.columns)
                )
            ]
            free = [value for value in values if value >= 0]
            if not free:
                characters.append("#")
                continue
            component_id = Counter(free).most_common(1)[0][0]
            if component_id == report.main_component_id:
                characters.append(".")
            elif component_id in disconnected_characters:
                characters.append(disconnected_characters[component_id])
            else:
                characters.append("o")
        rendered.append("".join(characters))
    width = max((len(row) for row in rendered), default=0)
    border = "+" + "-" * width + "+"
    return [border, *(f"|{row:<{width}}|" for row in rendered), border]


def format_report(report: NavigationReport, include_map: bool = True) -> str:
    """Format connectivity metrics and a compact occupancy map for a terminal."""
    main = report.main_region
    ignored_regions = sum(not region.significant for region in report.regions)
    main_approaches = sum(item.in_main_region for item in report.approaches)
    if report.no_navigable_area:
        result = "WARNING: NO USABLE NAVIGABLE AREA"
    elif report.disconnected:
        result = "WARNING: DISCONNECTED NAVIGABLE REGIONS"
    elif report.clearance_sensitive:
        result = "CONNECTED; WARNING: NARROW / GRID-SENSITIVE BOTTLENECK"
    else:
        result = "CONNECTED"

    lines = [
        f"Mapwright navigation report: {report.scene_path}",
        (
            f"Ground: {report.ground.width:.2f} x {report.ground.depth:.2f} | "
            f"Minimum path width: {report.minimum_path_width:.2f} | "
            f"Obstacles: {len(report.obstacles)}"
        ),
        (
            f"Grid: {report.columns} x {report.rows} | "
            f"Actual cell: {report.actual_cell_width:.3f} x "
            f"{report.actual_cell_depth:.3f}"
        ),
        (
            f"Blocked cells: {report.blocked_cell_count} | "
            f"Free cells: {report.free_cell_count} | "
            f"Free area: {report.free_cell_count * report.cell_area:.2f}"
        ),
        (
            f"Connected regions: {len(report.regions)} total, "
            f"{len(report.significant_regions)} significant, "
            f"{ignored_regions} below {report.minimum_region_area:g} area"
        ),
        (
            f"Largest reachable region: {main.area:.2f} "
            f"({report.reachable_ratio * 100:.2f}% of free space)"
            if main is not None
            else "Largest reachable region: none"
        ),
        (
            f"Prop approaches in main region: {main_approaches}/{len(report.approaches)}"
        ),
        (
            "Clearance sensitivity: WARNING (adding half a cell disconnects free space)"
            if report.clearance_sensitive
            else "Clearance sensitivity: stable at this grid resolution"
        ),
    ]
    if include_map:
        lines.extend(
            [
                "",
                "Occupancy map (+Z / north at top):",
                *_render_map(report),
                "Legend: # blocked, . main reachable region, A-Z disconnected region, o tiny pocket",
            ]
        )
    if report.disconnected:
        lines.extend(["", "Disconnected significant regions:"])
        lines.extend(
            (
                f"- Region {region.component_id}: area {region.area:.2f}, "
                f"bounds X[{region.min_x:.2f}, {region.max_x:.2f}] "
                f"Z[{region.min_z:.2f}, {region.max_z:.2f}]"
            )
            for region in report.significant_regions
            if region.component_id != report.main_component_id
        )
    unreachable = [item for item in report.approaches if not item.in_main_region]
    if unreachable:
        lines.extend(["", "Prop approaches outside main region:"])
        lines.extend(f"- {item.name}" for item in unreachable)
    lines.extend(["", f"Result: {result}"])
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Estimate top-down free-space connectivity in a Godot 4 scene."
    )
    parser.add_argument("scene", help="Path to the Godot 4 .tscn scene")
    parser.add_argument(
        "--minimum-path-width",
        type=float,
        default=DEFAULT_MINIMUM_PATH_WIDTH,
        help=(
            "Minimum traversable diameter in Godot units "
            f"(default: {DEFAULT_MINIMUM_PATH_WIDTH:g})"
        ),
    )
    parser.add_argument(
        "--cell-size",
        type=float,
        default=DEFAULT_CELL_SIZE,
        help=f"Target XZ occupancy cell size (default: {DEFAULT_CELL_SIZE:g})",
    )
    parser.add_argument(
        "--minimum-region-area",
        type=float,
        default=DEFAULT_MINIMUM_REGION_AREA,
        help=(
            "Minimum free-space component area to flag "
            f"(default: {DEFAULT_MINIMUM_REGION_AREA:g})"
        ),
    )
    parser.add_argument(
        "--no-map",
        action="store_true",
        help="Omit the compact ASCII occupancy map",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the navigation analyzer CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = analyze_navigation(
            args.scene,
            minimum_path_width=args.minimum_path_width,
            cell_size=args.cell_size,
            minimum_region_area=args.minimum_region_area,
        )
    except (NavigationParseError, FileNotFoundError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(format_report(report, include_map=not args.no_map))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
