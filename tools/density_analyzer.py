"""Analyze regional prop density across a Godot 4 scene ground plane."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

if __package__:
    from tools.spacing_analyzer import (
        NodePosition,
        SpacingParseError,
        parse_scene_positions,
    )
else:
    from spacing_analyzer import NodePosition, SpacingParseError, parse_scene_positions


DEFAULT_CELL_SIZE = 3.0
DEFAULT_IMBALANCE_THRESHOLD = 55.0
HEAT_CHARACTERS = " .:-=+*#%@"

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_TAG_PATTERN = re.compile(
    r"^\s*\[(?P<name>[a-z_]+)(?P<body>.*?)\]\s*(?:;.*)?$"
)
_VECTOR2_PATTERN = re.compile(
    rf"(?:^|\n)\s*size\s*=\s*Vector2\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_VECTOR3_SIZE_PATTERN = re.compile(
    rf"(?:^|\n)\s*size\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_SCALE_PATTERN = re.compile(
    rf"(?:^|\n)\s*scale\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_ROTATION_PATTERN = re.compile(
    rf"(?:^|\n)\s*rotation\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_TRANSFORM_PATTERN = re.compile(
    r"(?:^|\n)\s*transform\s*=\s*Transform3D\s*\((.*?)\)",
    re.MULTILINE | re.DOTALL,
)
_MESH_REFERENCE_PATTERN = re.compile(
    r'(?:^|\n)\s*mesh\s*=\s*SubResource\s*\(\s*"((?:\\.|[^"\\])*)"\s*\)',
    re.MULTILINE,
)
_GROUND_NAME_PATTERN = re.compile(
    r"(?:^|[_\-\s])(ground|floor|terrain)(?:$|[_\-\s])"
)


class DensityParseError(ValueError):
    """Raised when a supported ground area cannot be parsed from a scene."""


@dataclass(frozen=True)
class GroundBounds:
    """Axis-aligned XZ bounds covering all supported ground geometry."""

    min_x: float
    max_x: float
    min_z: float
    max_z: float
    source_nodes: tuple[str, ...]

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def depth(self) -> float:
        return self.max_z - self.min_z


@dataclass(frozen=True)
class DensityCell:
    """One equal-area ground cell and its prop count."""

    column: int
    row: int
    min_x: float
    max_x: float
    min_z: float
    max_z: float
    count: int

    @property
    def label(self) -> str:
        return f"C{self.column + 1}/R{self.row + 1}"


@dataclass(frozen=True)
class DensityReport:
    """Complete regional density analysis for one scene."""

    scene_path: str
    requested_cell_size: float
    actual_cell_width: float
    actual_cell_depth: float
    columns: int
    rows: int
    ground: GroundBounds
    cells: tuple[DensityCell, ...]
    prop_count: int
    outside_prop_count: int
    mean: float
    variance: float
    standard_deviation: float
    imbalance_score: float
    imbalance_threshold: float
    imbalanced: bool

    @property
    def empty_cells(self) -> tuple[DensityCell, ...]:
        return tuple(cell for cell in self.cells if cell.count == 0)

    @property
    def densest_cells(self) -> tuple[DensityCell, ...]:
        maximum = max((cell.count for cell in self.cells), default=0)
        return tuple(cell for cell in self.cells if cell.count == maximum)

    @property
    def empty_ratio(self) -> float:
        return len(self.empty_cells) / len(self.cells) if self.cells else 0.0

    @property
    def densest_share(self) -> float:
        maximum = max((cell.count for cell in self.cells), default=0)
        return maximum / self.prop_count if self.prop_count else 0.0


@dataclass(frozen=True)
class _SceneBlock:
    line_number: int
    tag_name: str
    tag_body: str
    properties: str


@dataclass(frozen=True)
class _MeshSize:
    width: float
    depth: float


def _decode_quoted_string(raw_value: str, context: str) -> str:
    try:
        return json.loads(f'"{raw_value}"')
    except json.JSONDecodeError as exc:
        raise DensityParseError(f"Invalid quoted string in {context}.") from exc


def _optional_quoted_attribute(tag_body: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*\"((?:\\.|[^\"\\])*)\"",
        tag_body,
    )
    if match is None:
        return None
    return _decode_quoted_string(match.group(1), f"'{name}' attribute")


def _normalize_name(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _is_ground_name(name: str) -> bool:
    return bool(_GROUND_NAME_PATTERN.search(_normalize_name(name)))


def _parse_blocks(scene_text: str) -> tuple[_SceneBlock, ...]:
    blocks: list[_SceneBlock] = []
    current: tuple[int, str, str] | None = None
    properties: list[str] = []

    for line_number, line in enumerate(scene_text.lstrip("\ufeff").splitlines(), 1):
        stripped = line.strip()
        tag_match = _TAG_PATTERN.match(line) if stripped and not stripped.startswith(";") else None
        if tag_match is not None:
            if current is not None:
                blocks.append(
                    _SceneBlock(*current, properties="\n".join(properties))
                )
            current = (line_number, tag_match.group("name"), tag_match.group("body"))
            properties = []
        elif current is not None:
            properties.append(line)

    if current is not None:
        blocks.append(_SceneBlock(*current, properties="\n".join(properties)))
    return tuple(blocks)


def _mesh_sizes(blocks: tuple[_SceneBlock, ...]) -> dict[str, _MeshSize]:
    meshes: dict[str, _MeshSize] = {}
    for block in blocks:
        if block.tag_name != "sub_resource":
            continue
        resource_type = _optional_quoted_attribute(block.tag_body, "type")
        resource_id = _optional_quoted_attribute(block.tag_body, "id")
        if resource_id is None:
            continue
        if resource_type == "BoxMesh":
            match = _VECTOR3_SIZE_PATTERN.search(block.properties)
            width = float(match.group(1)) if match else 1.0
            depth = float(match.group(3)) if match else 1.0
            meshes[resource_id] = _MeshSize(abs(width), abs(depth))
        elif resource_type == "PlaneMesh":
            match = _VECTOR2_PATTERN.search(block.properties)
            width = float(match.group(1)) if match else 2.0
            depth = float(match.group(2)) if match else 2.0
            meshes[resource_id] = _MeshSize(abs(width), abs(depth))
    return meshes


def _node_path(tag_body: str, root_name: str, line_number: int) -> tuple[str, str]:
    name = _optional_quoted_attribute(tag_body, "name")
    if name is None:
        raise DensityParseError(
            f"Missing 'name' in node declaration on line {line_number}."
        )
    parent = _optional_quoted_attribute(tag_body, "parent")
    if parent is None:
        return name, name
    if parent == ".":
        return name, f"{root_name}/{name}"
    return name, f"{root_name}/{parent.strip('/')}/{name}"


def _ground_extents(properties: str, mesh_size: _MeshSize) -> tuple[float, float]:
    transform_match = _TRANSFORM_PATTERN.search(properties)
    if transform_match is not None:
        values = [float(number) for number in re.findall(_NUMBER, transform_match.group(1))]
        if len(values) != 12:
            raise DensityParseError(
                f"Expected 12 numeric values in ground Transform3D, found {len(values)}."
            )
        half_width = mesh_size.width * 0.5
        half_depth = mesh_size.depth * 0.5
        extent_x = abs(values[0]) * half_width + abs(values[6]) * half_depth
        extent_z = abs(values[2]) * half_width + abs(values[8]) * half_depth
        return extent_x, extent_z

    scale_match = _SCALE_PATTERN.search(properties)
    scale_x = abs(float(scale_match.group(1))) if scale_match else 1.0
    scale_z = abs(float(scale_match.group(3))) if scale_match else 1.0
    rotation_match = _ROTATION_PATTERN.search(properties)
    yaw = float(rotation_match.group(2)) if rotation_match else 0.0
    half_width = mesh_size.width * scale_x * 0.5
    half_depth = mesh_size.depth * scale_z * 0.5
    extent_x = abs(math.cos(yaw)) * half_width + abs(math.sin(yaw)) * half_depth
    extent_z = abs(math.sin(yaw)) * half_width + abs(math.cos(yaw)) * half_depth
    return extent_x, extent_z


def parse_ground_bounds(
    scene_text: str, positions: tuple[NodePosition, ...]
) -> GroundBounds:
    """Find the union of supported ground geometry projected onto the XZ plane."""
    blocks = _parse_blocks(scene_text)
    node_blocks = [block for block in blocks if block.tag_name == "node"]
    if not node_blocks:
        raise DensityParseError("TSCN scene has no node declarations.")
    root_name = _optional_quoted_attribute(node_blocks[0].tag_body, "name")
    if root_name is None:
        raise DensityParseError("Scene root node has no name.")

    meshes = _mesh_sizes(blocks)
    positions_by_path = {node.node_path: node.position for node in positions}
    bounds: list[tuple[float, float, float, float, str]] = []

    for block in node_blocks:
        name, path = _node_path(block.tag_body, root_name, block.line_number)
        if not _is_ground_name(name):
            continue
        node_type = _optional_quoted_attribute(block.tag_body, "type") or ""
        mesh_size: _MeshSize | None = None
        if node_type == "MeshInstance3D":
            mesh_match = _MESH_REFERENCE_PATTERN.search(block.properties)
            if mesh_match is not None:
                mesh_id = _decode_quoted_string(
                    mesh_match.group(1), f"mesh reference for node '{path}'"
                )
                mesh_size = meshes.get(mesh_id)
        elif node_type == "CSGBox3D":
            size_match = _VECTOR3_SIZE_PATTERN.search(block.properties)
            if size_match is not None:
                mesh_size = _MeshSize(
                    abs(float(size_match.group(1))), abs(float(size_match.group(3)))
                )

        if mesh_size is None or path not in positions_by_path:
            continue
        center = positions_by_path[path]
        extent_x, extent_z = _ground_extents(block.properties, mesh_size)
        bounds.append(
            (
                center.x - extent_x,
                center.x + extent_x,
                center.z - extent_z,
                center.z + extent_z,
                path,
            )
        )

    if not bounds:
        raise DensityParseError(
            "No supported ground geometry found. Name a BoxMesh/PlaneMesh/CSGBox3D "
            "node with a ground, floor, or terrain token."
        )
    return GroundBounds(
        min_x=min(bound[0] for bound in bounds),
        max_x=max(bound[1] for bound in bounds),
        min_z=min(bound[2] for bound in bounds),
        max_z=max(bound[3] for bound in bounds),
        source_nodes=tuple(bound[4] for bound in bounds),
    )


def _cell_index(value: float, minimum: float, maximum: float, count: int) -> int:
    if math.isclose(value, maximum, rel_tol=1e-9, abs_tol=1e-9):
        return count - 1
    normalized = (value - minimum) / (maximum - minimum)
    return min(max(int(normalized * count), 0), count - 1)


def analyze_density(
    path: str,
    cell_size: float = DEFAULT_CELL_SIZE,
    imbalance_threshold: float = DEFAULT_IMBALANCE_THRESHOLD,
) -> DensityReport:
    """Analyze equal-area grid density over a supported Godot scene ground."""
    if not math.isfinite(cell_size) or cell_size <= 0:
        raise ValueError("Density cell size must be a positive finite number.")
    if not math.isfinite(imbalance_threshold) or not 0 <= imbalance_threshold <= 100:
        raise ValueError("Imbalance threshold must be between 0 and 100.")

    scene_path = Path(path).expanduser()
    if scene_path.suffix.lower() != ".tscn":
        raise ValueError(f"Expected a .tscn scene file: {scene_path}")
    try:
        scene_text = scene_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Godot scene file not found: {scene_path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Godot scene is not a UTF-8 text scene: {scene_path}"
        ) from exc

    try:
        positions = parse_scene_positions(scene_text)
    except SpacingParseError as exc:
        raise DensityParseError(str(exc)) from exc
    ground = parse_ground_bounds(scene_text, positions)
    if ground.width <= 0 or ground.depth <= 0:
        raise DensityParseError("Ground bounds must have positive width and depth.")

    columns = max(1, math.ceil(ground.width / cell_size))
    rows = max(1, math.ceil(ground.depth / cell_size))
    actual_width = ground.width / columns
    actual_depth = ground.depth / rows
    counts = [[0 for _ in range(columns)] for _ in range(rows)]
    outside_count = 0
    analyzed_nodes = [node for node in positions if node.analyzed]

    for node in analyzed_nodes:
        point = node.position
        inside = (
            ground.min_x <= point.x <= ground.max_x
            and ground.min_z <= point.z <= ground.max_z
        )
        if not inside:
            outside_count += 1
            continue
        column = _cell_index(point.x, ground.min_x, ground.max_x, columns)
        row = _cell_index(point.z, ground.min_z, ground.max_z, rows)
        counts[row][column] += 1

    cells = tuple(
        DensityCell(
            column=column,
            row=row,
            min_x=ground.min_x + column * actual_width,
            max_x=ground.min_x + (column + 1) * actual_width,
            min_z=ground.min_z + row * actual_depth,
            max_z=ground.min_z + (row + 1) * actual_depth,
            count=counts[row][column],
        )
        for row in range(rows)
        for column in range(columns)
    )
    cell_counts = [cell.count for cell in cells]
    prop_count = sum(cell_counts)
    mean = statistics.fmean(cell_counts) if cell_counts else 0.0
    variance = statistics.pvariance(cell_counts) if cell_counts else 0.0
    standard_deviation = math.sqrt(variance)
    if prop_count == 0:
        imbalance_score = 100.0
    elif standard_deviation == 0:
        imbalance_score = 0.0
    else:
        imbalance_score = 100.0 * standard_deviation / (standard_deviation + mean)

    return DensityReport(
        scene_path=str(scene_path),
        requested_cell_size=cell_size,
        actual_cell_width=actual_width,
        actual_cell_depth=actual_depth,
        columns=columns,
        rows=rows,
        ground=ground,
        cells=cells,
        prop_count=prop_count,
        outside_prop_count=outside_count,
        mean=mean,
        variance=variance,
        standard_deviation=standard_deviation,
        imbalance_score=imbalance_score,
        imbalance_threshold=imbalance_threshold,
        imbalanced=imbalance_score > imbalance_threshold,
    )


def _heat_character(count: int) -> str:
    return HEAT_CHARACTERS[min(count, len(HEAT_CHARACTERS) - 1)]


def format_report(report: DensityReport) -> str:
    """Format grid density metrics and an ASCII heatmap for a terminal."""
    counts = {(cell.column, cell.row): cell.count for cell in report.cells}
    border = "+" + "-" * report.columns + "+"
    heatmap = [border]
    for row in reversed(range(report.rows)):
        heatmap.append(
            "|"
            + "".join(_heat_character(counts[(column, row)]) for column in range(report.columns))
            + "|"
        )
    heatmap.append(border)

    densest_count = max((cell.count for cell in report.cells), default=0)
    densest_labels = ", ".join(cell.label for cell in report.densest_cells)
    status = (
        "WARNING: IMBALANCED"
        if report.imbalanced
        else "BELOW WARNING THRESHOLD"
    )
    lines = [
        f"Mapwright density report: {report.scene_path}",
        (
            f"Ground: {report.ground.width:.2f} x {report.ground.depth:.2f} | "
            f"Grid: {report.columns} x {report.rows} | "
            f"Actual cell: {report.actual_cell_width:.2f} x {report.actual_cell_depth:.2f}"
        ),
        f"Ground nodes: {', '.join(report.ground.source_nodes)}",
        "",
        "ASCII heatmap (+Z / north at top):",
        *heatmap,
        "Legend: 0=' ' 1='.' 2=':' 3='-' 4='=' 5='+' 6='*' 7='#' 8='%' 9+='@'",
        "",
        f"Props inside ground: {report.prop_count} | Outside ground: {report.outside_prop_count}",
        f"Densest cells: {densest_labels} ({densest_count} props)",
        f"Densest cell share: {report.densest_share * 100:.2f}%",
        f"Empty cells: {len(report.empty_cells)}/{len(report.cells)} ({report.empty_ratio * 100:.2f}%)",
        f"Mean: {report.mean:.3f} | Variance: {report.variance:.3f} | Std dev: {report.standard_deviation:.3f}",
        (
            f"Imbalance score: {report.imbalance_score:.2f}/100 | "
            f"Warning threshold: > {report.imbalance_threshold:g} | {status}"
        ),
    ]
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Analyze regional prop density on a Godot 4 scene ground."
    )
    parser.add_argument("scene", help="Path to the Godot 4 .tscn scene")
    parser.add_argument(
        "--cell-size",
        type=float,
        default=DEFAULT_CELL_SIZE,
        help=f"Target grid cell size in Godot units (default: {DEFAULT_CELL_SIZE:g})",
    )
    parser.add_argument(
        "--imbalance-threshold",
        type=float,
        default=DEFAULT_IMBALANCE_THRESHOLD,
        help=(
            "Warn above this 0-100 imbalance score "
            f"(default: {DEFAULT_IMBALANCE_THRESHOLD:g})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the density analyzer CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = analyze_density(
            args.scene,
            cell_size=args.cell_size,
            imbalance_threshold=args.imbalance_threshold,
        )
    except (DensityParseError, FileNotFoundError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
