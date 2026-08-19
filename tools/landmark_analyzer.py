"""Estimate landmark hierarchy from object dimensions in Godot 4 text scenes."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

if __package__:
    from tools.repetition_detector import TscnParseError, parse_tscn
    from tools.spacing_analyzer import (
        NodePosition,
        SpacingParseError,
        Vector3,
        parse_scene_positions,
    )
else:
    from repetition_detector import TscnParseError, parse_tscn
    from spacing_analyzer import (
        NodePosition,
        SpacingParseError,
        Vector3,
        parse_scene_positions,
    )


DEFAULT_CANDIDATE_RATIO = 1.15
DEFAULT_HIERARCHY_FACTOR = 1.5

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_TAG_PATTERN = re.compile(
    r"^\s*\[(?P<name>[a-z_]+)(?P<body>.*?)\]\s*(?:;.*)?$"
)
_VECTOR2_PROPERTY = re.compile(
    rf"(?:^|\n)\s*(?P<name>[a-z_]+)\s*=\s*Vector2\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_VECTOR3_PROPERTY = re.compile(
    rf"(?:^|\n)\s*(?P<name>[a-z_]+)\s*=\s*Vector3\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_AABB_PATTERN = re.compile(
    rf"(?:^|\n)\s*custom_aabb\s*=\s*AABB\s*\(\s*"
    rf"{_NUMBER}\s*,\s*{_NUMBER}\s*,\s*{_NUMBER}\s*,\s*"
    rf"({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
    re.MULTILINE,
)
_FLOAT_PROPERTY_TEMPLATE = r"(?:^|\n)\s*{name}\s*=\s*({number})(?:\s|$)"
_MESH_REFERENCE_PATTERN = re.compile(
    r'(?:^|\n)\s*mesh\s*=\s*SubResource\s*\(\s*"((?:\\.|[^"\\])*)"\s*\)',
    re.MULTILINE,
)
_INSTANCE_REFERENCE_PATTERN = re.compile(
    r'(?:^|\s)instance\s*=\s*ExtResource\s*\(\s*"((?:\\.|[^"\\])*)"\s*\)'
)


class LandmarkParseError(ValueError):
    """Raised when landmark dimensions cannot be derived from a scene."""


@dataclass(frozen=True)
class ObjectSize:
    """One measured prop and its heuristic landmark score."""

    name: str
    node_path: str
    asset_path: str
    position: Vector3
    base_dimensions: Vector3
    world_scale: Vector3
    dimensions: Vector3
    size_score: float
    relative_score: float
    large: bool
    landmark_candidate: bool


@dataclass(frozen=True)
class UnresolvedObject:
    """A prop whose serialized data has no usable size information."""

    name: str
    node_path: str
    reason: str


@dataclass(frozen=True)
class LandmarkReport:
    """Landmark hierarchy analysis for a scene or zone."""

    scene_path: str
    candidate_ratio: float
    hierarchy_factor: float
    median_size_score: float
    objects: tuple[ObjectSize, ...]
    unresolved: tuple[UnresolvedObject, ...]
    competing_landmarks: bool
    no_clear_hierarchy: bool

    @property
    def candidates(self) -> tuple[ObjectSize, ...]:
        return tuple(obj for obj in self.objects if obj.landmark_candidate)


@dataclass(frozen=True)
class _SceneBlock:
    line_number: int
    tag_name: str
    tag_body: str
    properties: str


def _decode_quoted_string(raw_value: str, context: str) -> str:
    try:
        return json.loads(f'"{raw_value}"')
    except json.JSONDecodeError as exc:
        raise LandmarkParseError(f"Invalid quoted string in {context}.") from exc


def _optional_quoted_attribute(tag_body: str, name: str) -> str | None:
    match = re.search(
        rf'(?:^|\s){re.escape(name)}\s*=\s*"((?:\\.|[^"\\])*)"',
        tag_body,
    )
    if match is None:
        return None
    return _decode_quoted_string(match.group(1), f"'{name}' attribute")


def _parse_blocks(scene_text: str) -> tuple[_SceneBlock, ...]:
    blocks: list[_SceneBlock] = []
    current: tuple[int, str, str] | None = None
    properties: list[str] = []

    for line_number, line in enumerate(scene_text.lstrip("\ufeff").splitlines(), 1):
        stripped = line.strip()
        tag_match = (
            _TAG_PATTERN.match(line)
            if stripped and not stripped.startswith(";")
            else None
        )
        if tag_match is not None:
            if current is not None:
                blocks.append(_SceneBlock(*current, properties="\n".join(properties)))
            current = (line_number, tag_match.group("name"), tag_match.group("body"))
            properties = []
        elif current is not None:
            properties.append(line)

    if current is not None:
        blocks.append(_SceneBlock(*current, properties="\n".join(properties)))
    return tuple(blocks)


def _node_path(tag_body: str, root_name: str, line_number: int) -> tuple[str, str]:
    name = _optional_quoted_attribute(tag_body, "name")
    if name is None:
        raise LandmarkParseError(
            f"Missing 'name' in node declaration on line {line_number}."
        )
    parent = _optional_quoted_attribute(tag_body, "parent")
    if parent is None:
        return name, name
    if parent == ".":
        return name, f"{root_name}/{name}"
    return name, f"{root_name}/{parent.strip('/')}/{name}"


def _vector3_property(properties: str, name: str) -> Vector3 | None:
    for match in _VECTOR3_PROPERTY.finditer(properties):
        if match.group("name") == name:
            return Vector3(
                abs(float(match.group(2))),
                abs(float(match.group(3))),
                abs(float(match.group(4))),
            )
    return None


def _vector2_property(properties: str, name: str) -> tuple[float, float] | None:
    for match in _VECTOR2_PROPERTY.finditer(properties):
        if match.group("name") == name:
            return abs(float(match.group(2))), abs(float(match.group(3)))
    return None


def _float_property(properties: str, name: str, default: float) -> float:
    pattern = re.compile(
        _FLOAT_PROPERTY_TEMPLATE.format(name=re.escape(name), number=_NUMBER),
        re.MULTILINE,
    )
    match = pattern.search(properties)
    return abs(float(match.group(1))) if match else default


def _aabb_dimensions(properties: str) -> Vector3 | None:
    match = _AABB_PATTERN.search(properties)
    if match is None:
        return None
    dimensions = Vector3(
        abs(float(match.group(1))),
        abs(float(match.group(2))),
        abs(float(match.group(3))),
    )
    if math.isclose(dimensions.length(), 0.0):
        return None
    return dimensions


def _primitive_dimensions(resource_type: str, properties: str) -> Vector3 | None:
    custom = _aabb_dimensions(properties)
    if custom is not None:
        return custom
    if resource_type == "BoxMesh":
        return _vector3_property(properties, "size") or Vector3(1.0, 1.0, 1.0)
    if resource_type == "PlaneMesh":
        size = _vector2_property(properties, "size") or (2.0, 2.0)
        return Vector3(size[0], 0.0, size[1])
    if resource_type == "QuadMesh":
        size = _vector2_property(properties, "size") or (1.0, 1.0)
        return Vector3(size[0], size[1], 0.0)
    if resource_type in {"CylinderMesh", "CapsuleMesh"}:
        radius = max(
            _float_property(properties, "radius", 0.5),
            _float_property(properties, "top_radius", 0.5),
            _float_property(properties, "bottom_radius", 0.5),
        )
        height = _float_property(properties, "height", 2.0)
        return Vector3(radius * 2.0, height, radius * 2.0)
    if resource_type == "SphereMesh":
        radius = _float_property(properties, "radius", 0.5)
        height = _float_property(properties, "height", radius * 2.0)
        return Vector3(radius * 2.0, height, radius * 2.0)
    return None


def _csg_dimensions(node_type: str, properties: str) -> Vector3 | None:
    if node_type == "CSGBox3D":
        return _vector3_property(properties, "size") or Vector3(2.0, 2.0, 2.0)
    if node_type == "CSGCylinder3D":
        radius = _float_property(properties, "radius", 1.0)
        height = _float_property(properties, "height", 2.0)
        return Vector3(radius * 2.0, height, radius * 2.0)
    if node_type == "CSGSphere3D":
        radius = _float_property(properties, "radius", 1.0)
        return Vector3(radius * 2.0, radius * 2.0, radius * 2.0)
    return None


def _largest_dimensions(dimensions: Sequence[Vector3]) -> Vector3 | None:
    usable = [item for item in dimensions if item.length() > 0]
    return max(usable, key=lambda item: item.length()) if usable else None


def _primitive_resources(blocks: Sequence[_SceneBlock]) -> dict[str, Vector3]:
    resources: dict[str, Vector3] = {}
    for block in blocks:
        if block.tag_name != "sub_resource":
            continue
        resource_id = _optional_quoted_attribute(block.tag_body, "id")
        resource_type = _optional_quoted_attribute(block.tag_body, "type") or ""
        dimensions = _primitive_dimensions(resource_type, block.properties)
        if resource_id is not None and dimensions is not None:
            resources[resource_id] = dimensions
    return resources


def _measure_node_block(
    block: _SceneBlock, resources: dict[str, Vector3]
) -> Vector3 | None:
    explicit = _vector3_property(block.properties, "dimensions")
    if explicit is not None:
        return explicit
    custom = _aabb_dimensions(block.properties)
    if custom is not None:
        return custom
    node_type = _optional_quoted_attribute(block.tag_body, "type") or ""
    csg = _csg_dimensions(node_type, block.properties)
    if csg is not None:
        return csg
    mesh_match = _MESH_REFERENCE_PATTERN.search(block.properties)
    if mesh_match is None:
        return None
    mesh_id = _decode_quoted_string(mesh_match.group(1), "mesh reference")
    return resources.get(mesh_id)


def _measure_scene_text(scene_text: str) -> Vector3 | None:
    """Return the best serialized local-size proxy available in a TSCN."""
    blocks = _parse_blocks(scene_text)
    node_blocks = [block for block in blocks if block.tag_name == "node"]
    resources = _primitive_resources(blocks)
    measured = [
        dimensions
        for block in node_blocks
        if (dimensions := _measure_node_block(block, resources)) is not None
    ]
    return _largest_dimensions(measured)


def _find_project_root(scene_path: Path) -> Path:
    for candidate in (scene_path.parent, *scene_path.parents):
        if (candidate / "project.godot").is_file():
            return candidate
    return scene_path.parent


def _resolve_resource_path(resource_path: str, scene_path: Path, project_root: Path) -> Path | None:
    normalized = resource_path.replace("\\", "/")
    if normalized.startswith("uid://"):
        return None
    if normalized.startswith("res://"):
        relative = PurePosixPath(normalized[6:])
        return project_root.joinpath(*relative.parts)
    return scene_path.parent.joinpath(*PurePosixPath(normalized).parts)


def _instance_resource_id(tag_body: str) -> str | None:
    match = _INSTANCE_REFERENCE_PATTERN.search(tag_body)
    if match is None:
        return None
    return _decode_quoted_string(match.group(1), "node instance reference")


def _world_dimensions(base: Vector3, scale: Vector3) -> Vector3:
    return Vector3(
        abs(base.x * scale.x),
        abs(base.y * scale.y),
        abs(base.z * scale.z),
    )


def analyze_landmarks(
    path: str,
    candidate_ratio: float = DEFAULT_CANDIDATE_RATIO,
    hierarchy_factor: float = DEFAULT_HIERARCHY_FACTOR,
) -> LandmarkReport:
    """Analyze size hierarchy among prop-like objects in a Godot 4 scene."""
    if not math.isfinite(candidate_ratio) or candidate_ratio <= 1.0:
        raise ValueError("Candidate ratio must be a finite number greater than 1.")
    if not math.isfinite(hierarchy_factor) or hierarchy_factor <= 1.0:
        raise ValueError("Hierarchy factor must be a finite number greater than 1.")

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
        resources, _ = parse_tscn(scene_text)
        positions = parse_scene_positions(scene_text)
    except (TscnParseError, SpacingParseError) as exc:
        raise LandmarkParseError(str(exc)) from exc

    blocks = _parse_blocks(scene_text)
    node_blocks = [block for block in blocks if block.tag_name == "node"]
    if not node_blocks:
        raise LandmarkParseError("TSCN scene has no node declarations.")
    root_name = _optional_quoted_attribute(node_blocks[0].tag_body, "name")
    if root_name is None:
        raise LandmarkParseError("Scene root node has no name.")

    blocks_by_path: dict[str, _SceneBlock] = {}
    for block in node_blocks:
        _, node_path = _node_path(block.tag_body, root_name, block.line_number)
        blocks_by_path[node_path] = block
    scene_primitive_resources = _primitive_resources(blocks)

    project_root = _find_project_root(scene_path.resolve())
    size_cache: dict[Path, Vector3 | None] = {}
    measured: list[tuple[NodePosition, str, Vector3, Vector3, float]] = []
    unresolved: list[UnresolvedObject] = []

    for node in positions:
        if not node.analyzed:
            continue
        block = blocks_by_path.get(node.node_path)
        if block is None:
            unresolved.append(
                UnresolvedObject(node.name, node.node_path, "node declaration not found")
            )
            continue

        base_dimensions = _measure_node_block(block, scene_primitive_resources)
        asset_path = str(scene_path)
        if base_dimensions is None:
            resource_id = _instance_resource_id(block.tag_body)
            if resource_id is not None:
                resource = resources.get(resource_id)
                if resource is None:
                    unresolved.append(
                        UnresolvedObject(
                            node.name,
                            node.node_path,
                            f"undefined external resource '{resource_id}'",
                        )
                    )
                    continue
                asset_path = resource.path
                resolved = _resolve_resource_path(resource.path, scene_path, project_root)
                if resolved is None:
                    unresolved.append(
                        UnresolvedObject(
                            node.name,
                            node.node_path,
                            f"UID-only resource cannot be resolved statically: {resource.path}",
                        )
                    )
                    continue
                resolved = resolved.resolve()
                if resolved not in size_cache:
                    try:
                        asset_text = resolved.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        size_cache[resolved] = None
                    else:
                        size_cache[resolved] = _measure_scene_text(asset_text)
                base_dimensions = size_cache[resolved]
        if base_dimensions is None or base_dimensions.length() <= 0:
            unresolved.append(
                UnresolvedObject(
                    node.name,
                    node.node_path,
                    "no dimensions, custom_aabb, or supported primitive mesh size",
                )
            )
            continue

        dimensions = _world_dimensions(base_dimensions, node.scale)
        size_score = dimensions.length()
        if size_score <= 0:
            unresolved.append(
                UnresolvedObject(node.name, node.node_path, "world size is zero")
            )
            continue
        measured.append((node, asset_path, base_dimensions, dimensions, size_score))

    if not measured:
        raise LandmarkParseError(
            "No measurable prop objects found. Provide serialized dimensions, custom_aabb, "
            "or supported primitive mesh sizes."
        )

    median_score = statistics.median(item[4] for item in measured)
    largest_score = max(item[4] for item in measured)
    no_clear_hierarchy = largest_score < median_score * hierarchy_factor
    candidate_minimum = largest_score / candidate_ratio
    large_minimum = median_score * hierarchy_factor

    objects = tuple(
        sorted(
            (
                ObjectSize(
                    name=node.name,
                    node_path=node.node_path,
                    asset_path=asset_path,
                    position=node.position,
                    base_dimensions=base_dimensions,
                    world_scale=node.scale,
                    dimensions=dimensions,
                    size_score=size_score,
                    relative_score=size_score / median_score,
                    large=size_score >= large_minimum,
                    landmark_candidate=size_score >= candidate_minimum,
                )
                for node, asset_path, base_dimensions, dimensions, size_score in measured
            ),
            key=lambda obj: (-obj.size_score, obj.name.casefold(), obj.node_path),
        )
    )
    competing = (
        not no_clear_hierarchy
        and sum(obj.landmark_candidate and obj.large for obj in objects) > 1
    )
    return LandmarkReport(
        scene_path=str(scene_path),
        candidate_ratio=candidate_ratio,
        hierarchy_factor=hierarchy_factor,
        median_size_score=median_score,
        objects=objects,
        unresolved=tuple(unresolved),
        competing_landmarks=competing,
        no_clear_hierarchy=no_clear_hierarchy,
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


def format_report(report: LandmarkReport) -> str:
    """Format landmark candidates and hierarchy warnings for a terminal."""
    rows = []
    for obj in report.objects:
        status = []
        if obj.landmark_candidate:
            status.append("CANDIDATE")
        if obj.large:
            status.append("LARGE")
        rows.append(
            (
                obj.name,
                f"{obj.dimensions.x:.2f}x{obj.dimensions.y:.2f}x{obj.dimensions.z:.2f}",
                f"{obj.size_score:.3f}",
                f"{obj.relative_score:.2f}x",
                ", ".join(status) or "-",
            )
        )

    if report.no_clear_hierarchy:
        conclusion = "WARNING: NO CLEAR SIZE HIERARCHY"
    elif report.competing_landmarks:
        conclusion = "WARNING: COMPETING LANDMARK CANDIDATES"
    else:
        conclusion = "CLEAR SIZE HIERARCHY"

    candidates = ", ".join(obj.name for obj in report.candidates)
    lines = [
        f"Mapwright landmark report: {report.scene_path}",
        (
            f"Measured props: {len(report.objects)} | Unresolved: {len(report.unresolved)} | "
            f"Median size score: {report.median_size_score:.3f}"
        ),
        (
            f"Large threshold: >= {report.hierarchy_factor:g}x median | "
            f"Candidate band: largest / score <= {report.candidate_ratio:g}"
        ),
        "",
        *_format_table(rows, ("Object", "World dimensions", "Score", "Relative", "Status")),
        "",
        f"Landmark candidate(s): {candidates}",
        f"Result: {conclusion}",
    ]
    if report.unresolved:
        lines.extend(["", "Unresolved objects:"])
        lines.extend(
            f"- {item.name}: {item.reason}" for item in report.unresolved
        )
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Estimate landmark size hierarchy in a Godot 4 text scene."
    )
    parser.add_argument("scene", help="Path to the Godot 4 .tscn scene")
    parser.add_argument(
        "--candidate-ratio",
        type=float,
        default=DEFAULT_CANDIDATE_RATIO,
        help=(
            "Largest-to-object maximum ratio for landmark candidates "
            f"(default: {DEFAULT_CANDIDATE_RATIO:g})"
        ),
    )
    parser.add_argument(
        "--hierarchy-factor",
        type=float,
        default=DEFAULT_HIERARCHY_FACTOR,
        help=(
            "Minimum largest-to-median factor for a size hierarchy "
            f"(default: {DEFAULT_HIERARCHY_FACTOR:g})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the landmark analyzer CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = analyze_landmarks(
            args.scene,
            candidate_ratio=args.candidate_ratio,
            hierarchy_factor=args.hierarchy_factor,
        )
    except (LandmarkParseError, FileNotFoundError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
