"""Analyze spacing between positioned objects in Godot 4 text scenes."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_MINIMUM_DISTANCE = 1.5

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_TAG_PATTERN = re.compile(
    r"^\s*\[(?P<name>[a-z_]+)(?P<body>.*?)\]\s*(?:;.*)?$"
)
_VECTOR3_PATTERN = re.compile(
    rf"(?:^|\n)\s*position\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_ROTATION_PATTERN = re.compile(
    rf"(?:^|\n)\s*rotation\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_SCALE_PATTERN = re.compile(
    rf"(?:^|\n)\s*scale\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_TRANSFORM_PATTERN = re.compile(
    r"(?:^|\n)\s*transform\s*=\s*Transform3D\s*\((.*?)\)",
    re.MULTILINE | re.DOTALL,
)
_SUPPORT_NAME_PATTERN = re.compile(
    r"(?:^|[_\-\s])(ground|floor|terrain|road|path|nav|navmesh|navigation)(?:$|[_\-\s])"
)
_VISUAL_GEOMETRY_TYPES = {
    "MeshInstance3D",
    "MultiMeshInstance3D",
    "Sprite3D",
    "AnimatedSprite3D",
    "Decal",
    "CSGBox3D",
    "CSGCylinder3D",
    "CSGMesh3D",
    "CSGPolygon3D",
    "CSGSphere3D",
    "CSGTorus3D",
}
_EXCLUDED_3D_TYPES = {
    "Camera3D",
    "DirectionalLight3D",
    "LightmapProbe",
    "Marker3D",
    "OmniLight3D",
    "Path3D",
    "PathFollow3D",
    "ReflectionProbe",
    "SpotLight3D",
    "VisibleOnScreenNotifier3D",
}


class SpacingParseError(ValueError):
    """Raised when node positions cannot be parsed from a Godot 4 scene."""


@dataclass(frozen=True)
class Vector3:
    """A dependency-free 3D vector used by the static TSCN parser."""

    x: float
    y: float
    z: float

    def __add__(self, other: Vector3) -> Vector3:
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __mul__(self, scalar: float) -> Vector3:
        return Vector3(self.x * scalar, self.y * scalar, self.z * scalar)

    def distance_to(self, other: Vector3) -> float:
        """Return the Euclidean distance to another position."""
        return math.sqrt(
            (self.x - other.x) ** 2
            + (self.y - other.y) ** 2
            + (self.z - other.z) ** 2
        )

    def length(self) -> float:
        """Return the vector magnitude."""
        return math.sqrt(self.x**2 + self.y**2 + self.z**2)


@dataclass(frozen=True)
class Transform:
    """A Godot-compatible affine transform with basis axes and origin."""

    x_axis: Vector3
    y_axis: Vector3
    z_axis: Vector3
    origin: Vector3

    @classmethod
    def identity(cls) -> Transform:
        return cls(
            Vector3(1.0, 0.0, 0.0),
            Vector3(0.0, 1.0, 0.0),
            Vector3(0.0, 0.0, 1.0),
            Vector3(0.0, 0.0, 0.0),
        )

    def basis_transform(self, vector: Vector3) -> Vector3:
        return (
            self.x_axis * vector.x
            + self.y_axis * vector.y
            + self.z_axis * vector.z
        )

    def transform_point(self, point: Vector3) -> Vector3:
        return self.basis_transform(point) + self.origin

    def compose(self, child: Transform) -> Transform:
        """Compose this parent transform with a child local transform."""
        return Transform(
            self.basis_transform(child.x_axis),
            self.basis_transform(child.y_axis),
            self.basis_transform(child.z_axis),
            self.transform_point(child.origin),
        )


@dataclass(frozen=True)
class NodePosition:
    """A resolved global position for one 3D scene node."""

    name: str
    node_path: str
    node_type: str
    position: Vector3
    analyzed: bool
    filter_reason: str
    scale: Vector3 = Vector3(1.0, 1.0, 1.0)
    basis_x: Vector3 = Vector3(1.0, 0.0, 0.0)
    basis_y: Vector3 = Vector3(0.0, 1.0, 0.0)
    basis_z: Vector3 = Vector3(0.0, 0.0, 1.0)


@dataclass(frozen=True)
class SpacingIssue:
    """A pair of analyzed objects closer than the configured threshold."""

    first: NodePosition
    second: NodePosition
    distance: float
    too_close: bool = True


@dataclass(frozen=True)
class SpacingReport:
    """Complete spacing analysis for a scene."""

    scene_path: str
    threshold: float
    nodes: tuple[NodePosition, ...]
    issues: tuple[SpacingIssue, ...]

    @property
    def analyzed_count(self) -> int:
        return sum(node.analyzed for node in self.nodes)


@dataclass
class _ParsedNode:
    name: str
    node_path: str
    parent_path: str | None
    node_type: str
    is_instance: bool
    local_transform: Transform
    top_level: bool


def _decode_quoted_string(raw_value: str, context: str) -> str:
    try:
        return json.loads(f'"{raw_value}"')
    except json.JSONDecodeError as exc:
        raise SpacingParseError(f"Invalid quoted string in {context}.") from exc


def _optional_quoted_attribute(tag_body: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*\"((?:\\.|[^\"\\])*)\"",
        tag_body,
    )
    if match is None:
        return None
    return _decode_quoted_string(match.group(1), f"node '{name}' attribute")


def _required_quoted_attribute(tag_body: str, name: str, line_number: int) -> str:
    value = _optional_quoted_attribute(tag_body, name)
    if value is None:
        raise SpacingParseError(
            f"Missing '{name}' in node declaration on line {line_number}."
        )
    return value


def _scene_format(tag_body: str) -> int:
    match = re.search(r"(?:^|\s)format\s*=\s*(\d+)(?:\s|$)", tag_body)
    if match is None:
        raise SpacingParseError("Missing 'format' in gd_scene header.")
    return int(match.group(1))


def _numbers(value: str, expected: int, context: str) -> list[float]:
    parsed = [float(number) for number in re.findall(_NUMBER, value)]
    if len(parsed) != expected:
        raise SpacingParseError(
            f"Expected {expected} numeric values in {context}, found {len(parsed)}."
        )
    return parsed


def _vector_from_match(match: re.Match[str] | None, default: Vector3) -> Vector3:
    if match is None:
        return default
    return Vector3(float(match.group(1)), float(match.group(2)), float(match.group(3)))


def _matrix_multiply(
    left: tuple[tuple[float, float, float], ...],
    right: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3))
        for row in range(3)
    )


def _basis_from_euler(rotation: Vector3, scale: Vector3) -> tuple[Vector3, Vector3, Vector3]:
    """Build Godot's default Y-X-Z Euler basis, with local scale."""
    cx, sx = math.cos(rotation.x), math.sin(rotation.x)
    cy, sy = math.cos(rotation.y), math.sin(rotation.y)
    cz, sz = math.cos(rotation.z), math.sin(rotation.z)
    rotate_x = ((1.0, 0.0, 0.0), (0.0, cx, -sx), (0.0, sx, cx))
    rotate_y = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    rotate_z = ((cz, -sz, 0.0), (sz, cz, 0.0), (0.0, 0.0, 1.0))
    matrix = _matrix_multiply(_matrix_multiply(rotate_y, rotate_x), rotate_z)
    return (
        Vector3(matrix[0][0], matrix[1][0], matrix[2][0]) * scale.x,
        Vector3(matrix[0][1], matrix[1][1], matrix[2][1]) * scale.y,
        Vector3(matrix[0][2], matrix[1][2], matrix[2][2]) * scale.z,
    )


def _parse_local_transform(properties: str, node_path: str) -> Transform:
    transform_match = _TRANSFORM_PATTERN.search(properties)
    position_match = _VECTOR3_PATTERN.search(properties)

    if transform_match is not None:
        values = _numbers(
            transform_match.group(1), 12, f"Transform3D for node '{node_path}'"
        )
        transform = Transform(
            Vector3(*values[0:3]),
            Vector3(*values[3:6]),
            Vector3(*values[6:9]),
            Vector3(*values[9:12]),
        )
        if position_match is not None:
            return Transform(
                transform.x_axis,
                transform.y_axis,
                transform.z_axis,
                _vector_from_match(position_match, transform.origin),
            )
        return transform

    position = _vector_from_match(position_match, Vector3(0.0, 0.0, 0.0))
    rotation = _vector_from_match(
        _ROTATION_PATTERN.search(properties), Vector3(0.0, 0.0, 0.0)
    )
    scale = _vector_from_match(
        _SCALE_PATTERN.search(properties), Vector3(1.0, 1.0, 1.0)
    )
    x_axis, y_axis, z_axis = _basis_from_euler(rotation, scale)
    return Transform(x_axis, y_axis, z_axis, position)


def _is_top_level(properties: str) -> bool:
    return bool(
        re.search(
            r"(?:^|\n)\s*top_level\s*=\s*true(?:\s|$)",
            properties,
            re.MULTILINE,
        )
    )


def _is_3d_node(node: _ParsedNode) -> bool:
    return (
        node.is_instance
        or node.node_type == "Node3D"
        or node.node_type.endswith("3D")
        or node.node_type in _VISUAL_GEOMETRY_TYPES
    )


def _is_support_geometry_name(name: str) -> bool:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return bool(_SUPPORT_NAME_PATTERN.search(normalized))


def _filter_node(node: _ParsedNode, is_scene_root: bool) -> tuple[bool, str]:
    if is_scene_root:
        return False, "scene root"
    if node.node_type in _EXCLUDED_3D_TYPES:
        return False, f"support type {node.node_type}"
    if node.is_instance:
        return True, "external scene instance"
    if node.node_type in _VISUAL_GEOMETRY_TYPES:
        if _is_support_geometry_name(node.name):
            return False, "ground/path support geometry"
        return True, "visual geometry"
    return False, "non-prop 3D node"


def parse_scene_positions(scene_text: str) -> tuple[NodePosition, ...]:
    """Parse and globally resolve all 3D node positions in Godot 4 TSCN text."""
    node_blocks: list[tuple[int, str, str]] = []
    current_header: tuple[int, str] | None = None
    current_properties: list[str] = []
    header_checked = False

    for line_number, line in enumerate(scene_text.lstrip("\ufeff").splitlines(), 1):
        stripped = line.strip()
        tag_match = _TAG_PATTERN.match(line) if stripped and not stripped.startswith(";") else None

        if tag_match is not None:
            if current_header is not None:
                node_blocks.append(
                    (current_header[0], current_header[1], "\n".join(current_properties))
                )
                current_header = None
                current_properties = []

            tag_name = tag_match.group("name")
            tag_body = tag_match.group("body")
            if not header_checked:
                if tag_name != "gd_scene":
                    raise SpacingParseError(
                        "The first TSCN entry must be a gd_scene header."
                    )
                scene_format = _scene_format(tag_body)
                if scene_format != 3:
                    raise SpacingParseError(
                        f"Unsupported TSCN format {scene_format}; Godot 4 format 3 is required."
                    )
                header_checked = True
            elif tag_name == "node":
                current_header = (line_number, tag_body)
            continue

        if current_header is not None:
            current_properties.append(line)

    if current_header is not None:
        node_blocks.append(
            (current_header[0], current_header[1], "\n".join(current_properties))
        )
    if not header_checked:
        raise SpacingParseError("TSCN scene is empty or has no gd_scene header.")
    if not node_blocks:
        return ()

    root_name = _required_quoted_attribute(node_blocks[0][1], "name", node_blocks[0][0])
    parsed_nodes: list[_ParsedNode] = []
    for line_number, tag_body, properties in node_blocks:
        name = _required_quoted_attribute(tag_body, "name", line_number)
        parent = _optional_quoted_attribute(tag_body, "parent")
        node_type = _optional_quoted_attribute(tag_body, "type") or "InstancedScene"
        is_instance = bool(
            re.search(r"(?:^|\s)instance\s*=\s*ExtResource\s*\(", tag_body)
        )

        if parent is None:
            node_path = name
            parent_path = None
        elif parent == ".":
            node_path = f"{root_name}/{name}"
            parent_path = root_name
        else:
            normalized_parent = parent.strip("/")
            node_path = f"{root_name}/{normalized_parent}/{name}"
            parent_path = f"{root_name}/{normalized_parent}"

        parsed_nodes.append(
            _ParsedNode(
                name=name,
                node_path=node_path,
                parent_path=parent_path,
                node_type=node_type,
                is_instance=is_instance,
                local_transform=_parse_local_transform(properties, node_path),
                top_level=_is_top_level(properties),
            )
        )

    global_transforms: dict[str, Transform] = {}
    positions: list[NodePosition] = []
    for index, node in enumerate(parsed_nodes):
        parent_transform = global_transforms.get(node.parent_path, Transform.identity())
        global_transform = (
            node.local_transform
            if node.top_level
            else parent_transform.compose(node.local_transform)
        )
        global_transforms[node.node_path] = global_transform
        if not _is_3d_node(node):
            continue
        analyzed, reason = _filter_node(node, index == 0)
        positions.append(
            NodePosition(
                name=node.name,
                node_path=node.node_path,
                node_type=node.node_type,
                position=global_transform.origin,
                analyzed=analyzed,
                filter_reason=reason,
                scale=Vector3(
                    global_transform.x_axis.length(),
                    global_transform.y_axis.length(),
                    global_transform.z_axis.length(),
                ),
                basis_x=global_transform.x_axis,
                basis_y=global_transform.y_axis,
                basis_z=global_transform.z_axis,
            )
        )

    return tuple(positions)


def analyze_spacing(
    path: str, threshold: float = DEFAULT_MINIMUM_DISTANCE
) -> SpacingReport:
    """Analyze pairwise distances between prop-like nodes in a Godot 4 scene."""
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("Minimum spacing threshold must be a positive finite number.")

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

    nodes = parse_scene_positions(scene_text)
    analyzed_nodes = [node for node in nodes if node.analyzed]
    issues = []
    for first_index, first in enumerate(analyzed_nodes):
        for second in analyzed_nodes[first_index + 1 :]:
            distance = first.position.distance_to(second.position)
            if distance < threshold and not math.isclose(
                distance, threshold, rel_tol=1e-9, abs_tol=1e-9
            ):
                issues.append(SpacingIssue(first, second, distance))
    issues.sort(key=lambda issue: (issue.distance, issue.first.node_path, issue.second.node_path))
    return SpacingReport(
        scene_path=str(scene_path),
        threshold=threshold,
        nodes=nodes,
        issues=tuple(issues),
    )


def _render_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    return [render(headers), "  ".join("-" * width for width in widths)] + [
        render(row) for row in rows
    ]


def format_report(report: SpacingReport) -> str:
    """Format a spacing report as terminal-friendly position and issue tables."""
    position_rows = [
        (
            node.node_path,
            node.node_type,
            f"{node.position.x:.3f}",
            f"{node.position.y:.3f}",
            f"{node.position.z:.3f}",
            "analyzed" if node.analyzed else f"excluded: {node.filter_reason}",
        )
        for node in report.nodes
    ]
    issue_rows = [
        (
            issue.first.node_path,
            issue.second.node_path,
            f"{issue.distance:.3f}",
            "TOO_CLOSE",
        )
        for issue in report.issues
    ]

    lines = [
        f"Mapwright spacing report: {report.scene_path}",
        (
            f"3D positions: {len(report.nodes)} | Analyzed props: {report.analyzed_count} "
            f"| Minimum distance: {report.threshold:g}"
        ),
        "",
        "Node positions:",
    ]
    lines.extend(
        _render_table(("Node", "Type", "X", "Y", "Z", "Scope"), position_rows)
    )
    lines.extend(["", "Too-close pairs:"])
    if issue_rows:
        lines.extend(_render_table(("First", "Second", "Distance", "Status"), issue_rows))
    else:
        lines.append("None")
    lines.append("")
    lines.append(f"Too-close pairs: {len(report.issues)}")
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Analyze spacing between positioned props in a Godot 4 .tscn scene."
    )
    parser.add_argument("scene", help="Path to the Godot 4 .tscn scene")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_MINIMUM_DISTANCE,
        help=(
            "Flag object centers closer than this many Godot units "
            f"(default: {DEFAULT_MINIMUM_DISTANCE:g})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the spacing analyzer CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = analyze_spacing(args.scene, args.threshold)
    except (FileNotFoundError, OSError, SpacingParseError, ValueError) as exc:
        parser.error(str(exc))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
