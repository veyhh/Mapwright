"""Import Godot 4 ``.tscn`` text scenes into Scene IR.

Mapwright v0.1 grew four independent TSCN readers, one per validator. They
agreed on the format but re-derived node paths, transforms, and sizes
separately, so a correction in one never reached the others. This module is the
single reader: one block scan yields external resources, nodes, resolved world
transforms, dimensions, and ground geometry, and everything above it sees only
the engine-independent Scene IR.

Godot is Y-up, right-handed, and rotates in intrinsic Y-X-Z Euler order, which
is Mapwright's canonical space, so no coordinate conversion happens here.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from mapwright.adapters.base import AdapterError, SceneImporter
from mapwright.core.config import MapwrightConfig
from mapwright.core.geometry import Basis, Bounds, Vec3
from mapwright.core.scene_ir import (
    MarkerKind,
    MarkerPoint,
    ObjectType,
    SceneIR,
    SceneObject,
)


SUPPORTED_SCENE_FORMAT = 3

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_TAG_PATTERN = re.compile(r"^\s*\[(?P<name>[a-z_]+)(?P<body>.*?)\]\s*(?:;.*)?$")
POSITION_PATTERN = re.compile(
    rf"(?:^|\n)\s*position\s*=\s*Vector3\s*\(\s*({NUMBER})\s*,\s*({NUMBER})\s*,"
    rf"\s*({NUMBER})\s*\)",
    re.MULTILINE,
)
_ROTATION_PATTERN = re.compile(
    rf"(?:^|\n)\s*rotation\s*=\s*Vector3\s*\(\s*({NUMBER})\s*,\s*({NUMBER})\s*,"
    rf"\s*({NUMBER})\s*\)",
    re.MULTILINE,
)
_SCALE_PATTERN = re.compile(
    rf"(?:^|\n)\s*scale\s*=\s*Vector3\s*\(\s*({NUMBER})\s*,\s*({NUMBER})\s*,"
    rf"\s*({NUMBER})\s*\)",
    re.MULTILINE,
)
TRANSFORM_PATTERN = re.compile(
    r"(?:^|\n)\s*transform\s*=\s*Transform3D\s*\((.*?)\)",
    re.MULTILINE | re.DOTALL,
)
_TOP_LEVEL_PATTERN = re.compile(
    r"(?:^|\n)\s*top_level\s*=\s*true(?:\s|$)", re.MULTILINE
)
_VECTOR2_PROPERTY = re.compile(
    rf"(?:^|\n)\s*(?P<name>[a-z_]+)\s*=\s*Vector2\s*\(\s*({NUMBER})\s*,"
    rf"\s*({NUMBER})\s*\)",
    re.MULTILINE,
)
_VECTOR3_PROPERTY = re.compile(
    rf"(?:^|\n)\s*(?P<name>[a-z_]+)\s*=\s*Vector3\s*\(\s*({NUMBER})\s*,"
    rf"\s*({NUMBER})\s*,\s*({NUMBER})\s*\)",
    re.MULTILINE,
)
_AABB_PATTERN = re.compile(
    rf"(?:^|\n)\s*custom_aabb\s*=\s*AABB\s*\(\s*"
    rf"{NUMBER}\s*,\s*{NUMBER}\s*,\s*{NUMBER}\s*,\s*"
    rf"({NUMBER})\s*,\s*({NUMBER})\s*,\s*({NUMBER})\s*\)",
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
_INSTANCE_PATTERN = re.compile(r"(?:^|\s)instance\s*=\s*ExtResource\s*\(")

_SUPPORT_NAME_PATTERN = re.compile(
    r"(?:^|[_\-\s])(ground|floor|terrain|road|path|nav|navmesh|navigation)(?:$|[_\-\s])"
)
_GROUND_NAME_PATTERN = re.compile(r"(?:^|[_\-\s])(ground|floor|terrain)(?:$|[_\-\s])")
_STRUCTURE_NAME_PATTERN = re.compile(
    r"(?:^|[_\-\s])(?:wall|building|structure|arch|ruin)s?(?:$|[_\-\s])"
)
_TEAM_PATTERN = re.compile(r"(?:^|[_\-\s])team[_\-\s]?([a-z0-9]+)(?:$|[_\-\s])")
_TEAM_COLOR_PATTERN = re.compile(
    r"(?:^|[_\-\s])(blue|green|red|yellow)(?:$|[_\-\s])"
)

#: Node name tokens that turn an otherwise excluded node into a marker. The
#: first matching kind wins, so a name carrying two tokens is stable.
_MARKER_NAME_TOKENS: tuple[tuple[MarkerKind, tuple[str, ...]], ...] = (
    (MarkerKind.ENTRY, ("entry", "entrance")),
    (MarkerKind.SPAWN, ("spawn", "start")),
    (MarkerKind.OBJECTIVE, ("objective", "goal", "checkpoint")),
    (MarkerKind.EXIT, ("exit",)),
)
_MARKER_PATTERNS: tuple[tuple[MarkerKind, re.Pattern[str]], ...] = tuple(
    (
        kind,
        re.compile(rf"(?:^|[_\-\s])(?:{'|'.join(tokens)})(?:$|[_\-\s])"),
    )
    for kind, tokens in _MARKER_NAME_TOKENS
)
_MARKER_NODE_TYPES = frozenset({"Marker3D", "Node3D"})
#: Words that can follow "team" in a name while naming a role, not a team, so
#: "BlueTeamStart" reads as team "blue" rather than team "start".
_ROLE_TOKENS = frozenset(
    {token for _, tokens in _MARKER_NAME_TOKENS for token in tokens}
    | {"marker", "node", "point", "position"}
)

_VISUAL_GEOMETRY_TYPES = frozenset(
    {
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
)
_EXCLUDED_3D_TYPES = frozenset(
    {
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
)
#: Mesh primitives v0.1 accepted as ground geometry.
_GROUND_MESH_TYPES = frozenset({"BoxMesh", "PlaneMesh"})
#: Node type recorded for a node declared only by ``instance = ExtResource(...)``.
INSTANCED_SCENE_TYPE = "InstancedScene"


@dataclass(frozen=True)
class Transform:
    """An affine transform: a basis with scale, plus a world origin."""

    basis: Basis
    origin: Vec3

    @classmethod
    def identity(cls) -> Transform:
        """Return the unrotated, unit-scaled transform at the origin."""
        return cls(Basis.identity(), Vec3.zero())

    def transform_point(self, point: Vec3) -> Vec3:
        """Map a local point into this transform's parent space."""
        return self.basis.transform(point) + self.origin

    def compose(self, child: Transform) -> Transform:
        """Compose this parent transform with a child local transform."""
        return Transform(
            Basis(
                self.basis.transform(child.basis.x_axis),
                self.basis.transform(child.basis.y_axis),
                self.basis.transform(child.basis.z_axis),
            ),
            self.transform_point(child.origin),
        )

    def inverse_point(self, point: Vec3) -> Vec3 | None:
        """Map a parent-space point into local space, or ``None`` when singular."""
        return _solve(self.basis, point - self.origin)


@dataclass(frozen=True)
class ExternalResource:
    """An ``ext_resource`` declaration from a TSCN file."""

    resource_id: str
    path: str
    resource_type: str


@dataclass(frozen=True)
class PrimitiveResource:
    """A ``sub_resource`` mesh whose serialized size is a usable size proxy."""

    resource_id: str
    resource_type: str
    dimensions: Vec3


@dataclass(frozen=True)
class GodotNode:
    """One ``[node]`` block with its resolved world transform and source span."""

    name: str
    node_path: str
    parent_path: str | None
    node_type: str
    is_instance: bool
    resource_id: str | None
    top_level: bool
    local: Transform
    world: Transform
    tag_body: str
    properties: str
    header_index: int
    property_end: int

    @property
    def is_3d(self) -> bool:
        """Return whether this node carries a 3D transform Mapwright can use."""
        return (
            self.is_instance
            or self.node_type == "Node3D"
            or self.node_type.endswith("3D")
            or self.node_type in _VISUAL_GEOMETRY_TYPES
        )


@dataclass(frozen=True)
class ParsedScene:
    """Everything one pass over a TSCN file yields."""

    root_name: str
    resources: tuple[ExternalResource, ...]
    primitives: tuple[PrimitiveResource, ...]
    nodes: tuple[GodotNode, ...]

    def resource_by_id(self, resource_id: str) -> ExternalResource | None:
        """Return one external resource declaration by id."""
        return next(
            (item for item in self.resources if item.resource_id == resource_id), None
        )

    def primitive_map(self) -> dict[str, PrimitiveResource]:
        """Return the sub-resource size table keyed by resource id."""
        return {item.resource_id: item for item in self.primitives}


def parse_godot_scene(scene_text: str) -> ParsedScene:
    """Parse TSCN text into resources, nodes, and resolved world transforms."""
    lines = scene_text.lstrip("\ufeff").splitlines()
    blocks = _parse_blocks(lines)
    _check_header(blocks, lines)
    resources = _external_resources(blocks)
    _check_references(blocks, resources)
    node_blocks = tuple(block for block in blocks if block.tag_name == "node")
    if not node_blocks:
        raise AdapterError("TSCN scene has no node declarations.")
    return ParsedScene(
        root_name=_required_quoted_attribute(
            node_blocks[0].tag_body, "name", node_blocks[0].line_number
        ),
        resources=tuple(resources.values()),
        primitives=tuple(_primitive_resources(blocks).values()),
        nodes=_resolve_nodes(node_blocks),
    )


class GodotImporter(SceneImporter):
    """Reads a Godot 4 ``.tscn`` text scene into Scene IR."""

    engine = "godot"
    extensions = (".tscn",)

    def import_scene(self, path: str | Path, config: MapwrightConfig) -> SceneIR:
        """Read a Godot 4 text scene and return its engine-independent form."""
        scene_path = Path(path).expanduser()
        if scene_path.suffix.lower() != ".tscn":
            raise AdapterError(f"Expected a .tscn scene file: {scene_path}")
        try:
            scene_text = scene_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AdapterError(f"Godot scene file not found: {scene_path}") from exc
        except UnicodeDecodeError as exc:
            raise AdapterError(
                f"Godot scene is not a UTF-8 text scene: {scene_path}"
            ) from exc
        except OSError as exc:
            raise AdapterError(f"Could not read Godot scene {scene_path}: {exc}") from exc

        parsed = parse_godot_scene(scene_text)
        project_root = _project_root(scene_path.resolve(), config.engine.project_path)
        return _build_scene(parsed, scene_path, project_root)


# -- Scene IR construction ---------------------------------------------------


def _build_scene(parsed: ParsedScene, scene_path: Path, project_root: Path) -> SceneIR:
    """Turn parsed nodes into props, ground, and markers, in document order."""
    primitives = parsed.primitive_map()
    cache: dict[Path, Vec3 | None] = {}
    objects: list[SceneObject] = []
    ground_boxes: list[Bounds] = []
    markers: dict[MarkerKind, list[MarkerPoint]] = {kind: [] for kind in MarkerKind}

    for index, node in enumerate(parsed.nodes):
        if not node.is_3d:
            continue
        is_root = index == 0
        rotation, scale = node.world.basis.to_euler_scale()
        if _is_prop(node, is_root):
            dimensions = _resolve_dimensions(
                node, parsed, primitives, scene_path, project_root, cache
            )
            objects.append(_prop_object(node, rotation, scale, dimensions, parsed))
            continue
        ground = (
            _ground_dimensions(node.node_type, node.properties, primitives)
            if _is_ground_name(node.name)
            else None
        )
        if ground is not None:
            obj = _ground_object(node, rotation, scale, ground)
            objects.append(obj)
            box = obj.world_bounds()
            if box is not None:
                ground_boxes.append(box)
            continue
        marker = None if is_root else _marker_point(node)
        if marker is not None:
            markers[marker.kind].append(marker)

    return SceneIR(
        scene=parsed.root_name,
        objects=tuple(objects),
        entry_points=tuple(markers[MarkerKind.ENTRY]),
        spawn_points=tuple(markers[MarkerKind.SPAWN]),
        objectives=tuple(markers[MarkerKind.OBJECTIVE]),
        exits=tuple(markers[MarkerKind.EXIT]),
        landmarks=tuple(markers[MarkerKind.LANDMARK]),
        ground=Bounds.enclosing(ground_boxes),
        source_engine="godot",
        source_path=str(scene_path),
    )


def _prop_object(
    node: GodotNode,
    rotation: Vec3,
    scale: Vec3,
    dimensions: Vec3 | None,
    parsed: ParsedScene,
) -> SceneObject:
    resource = (
        parsed.resource_by_id(node.resource_id)
        if node.resource_id is not None
        else None
    )
    bounds = Bounds.from_size(dimensions) if dimensions is not None else None
    return SceneObject(
        id=node.node_path,
        name=node.name,
        type=(
            ObjectType.STRUCTURE
            if _STRUCTURE_NAME_PATTERN.search(_normalize_name(node.name))
            else ObjectType.PROP
        ),
        position=node.world.origin,
        rotation=rotation,
        scale=scale,
        bounds=bounds,
        asset=resource.path if resource is not None else None,
        tags=_tags(node.node_type, bounds is None),
        source=_source(node),
    )


def _ground_object(
    node: GodotNode, rotation: Vec3, scale: Vec3, dimensions: Vec3
) -> SceneObject:
    return SceneObject(
        id=node.node_path,
        name=node.name,
        type=ObjectType.GROUND,
        position=node.world.origin,
        rotation=rotation,
        scale=scale,
        bounds=Bounds.from_size(dimensions),
        tags=_tags(node.node_type, False),
        source=_source(node),
    )


def _marker_point(node: GodotNode) -> MarkerPoint | None:
    if node.node_type not in _MARKER_NODE_TYPES:
        return None
    normalized = _normalize_name(node.name)
    kind = next(
        (kind for kind, pattern in _MARKER_PATTERNS if pattern.search(normalized)), None
    )
    if kind is None:
        return None
    return MarkerPoint(
        id=node.node_path,
        kind=kind,
        position=node.world.origin,
        name=node.name,
        team=_team(normalized),
        tags=(f"godot:{node.node_type}",),
    )


def _team(normalized_name: str) -> str | None:
    """Return the team a marker name declares, by ``team_*`` token or colour."""
    match = _TEAM_PATTERN.search(normalized_name)
    if match is not None and match.group(1) not in _ROLE_TOKENS:
        return match.group(1)
    color = _TEAM_COLOR_PATTERN.search(normalized_name)
    return color.group(1) if color is not None else None


def _tags(node_type: str, unmeasured: bool) -> tuple[str, ...]:
    return (f"godot:{node_type}",) + (("unmeasured",) if unmeasured else ())


def _source(node: GodotNode) -> tuple[tuple[str, str], ...]:
    return (("engine", "godot"), ("node_path", node.node_path))


def _is_prop(node: GodotNode, is_root: bool) -> bool:
    """Return whether a node counts as placed content, as v0.1 decided it."""
    if is_root or node.node_type in _EXCLUDED_3D_TYPES:
        return False
    if node.is_instance:
        return True
    if node.node_type in _VISUAL_GEOMETRY_TYPES:
        return not _SUPPORT_NAME_PATTERN.search(_normalize_name(node.name))
    return False


def _normalize_name(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# -- Dimensions --------------------------------------------------------------


def _resolve_dimensions(
    node: GodotNode,
    parsed: ParsedScene,
    primitives: Mapping[str, PrimitiveResource],
    scene_path: Path,
    project_root: Path,
    cache: dict[Path, Vec3 | None],
) -> Vec3 | None:
    """Return a node's local size, following instances into their asset file."""
    dimensions = _measure_properties(node.node_type, node.properties, primitives)
    if dimensions is None and node.resource_id is not None:
        resource = parsed.resource_by_id(node.resource_id)
        if resource is not None:
            dimensions = _measure_asset(resource.path, scene_path, project_root, cache)
    if dimensions is None or dimensions.length() <= 0:
        return None
    return dimensions


def _measure_properties(
    node_type: str, properties: str, primitives: Mapping[str, PrimitiveResource]
) -> Vec3 | None:
    explicit = _vector3_property(properties, "dimensions")
    if explicit is not None:
        return explicit
    custom = _aabb_dimensions(properties)
    if custom is not None:
        return custom
    csg = _csg_dimensions(node_type, properties)
    if csg is not None:
        return csg
    mesh_match = _MESH_REFERENCE_PATTERN.search(properties)
    if mesh_match is None:
        return None
    mesh_id = _decode_quoted_string(mesh_match.group(1), "mesh reference")
    primitive = primitives.get(mesh_id)
    return primitive.dimensions if primitive is not None else None


def _measure_asset(
    resource_path: str,
    scene_path: Path,
    project_root: Path,
    cache: dict[Path, Vec3 | None],
) -> Vec3 | None:
    resolved = _resolve_resource_path(resource_path, scene_path, project_root)
    if resolved is None:
        return None
    resolved = resolved.resolve()
    if resolved not in cache:
        try:
            asset_text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            cache[resolved] = None
        else:
            cache[resolved] = _measure_scene_text(asset_text)
    return cache[resolved]


def _measure_scene_text(scene_text: str) -> Vec3 | None:
    """Return the largest serialized local-size proxy available in a TSCN."""
    blocks = _parse_blocks(scene_text.lstrip("\ufeff").splitlines())
    primitives = _primitive_resources(blocks)
    measured = [
        dimensions
        for block in blocks
        if block.tag_name == "node"
        and (
            dimensions := _measure_properties(
                _optional_quoted_attribute(block.tag_body, "type") or "",
                block.properties,
                primitives,
            )
        )
        is not None
    ]
    usable = [item for item in measured if item.length() > 0]
    return max(usable, key=lambda item: item.length()) if usable else None


def _primitive_dimensions(resource_type: str, properties: str) -> Vec3 | None:
    custom = _aabb_dimensions(properties)
    if custom is not None:
        return custom
    if resource_type == "BoxMesh":
        return _vector3_property(properties, "size") or Vec3(1.0, 1.0, 1.0)
    if resource_type == "PlaneMesh":
        size = _vector2_property(properties, "size") or (2.0, 2.0)
        return Vec3(size[0], 0.0, size[1])
    if resource_type == "QuadMesh":
        size = _vector2_property(properties, "size") or (1.0, 1.0)
        return Vec3(size[0], size[1], 0.0)
    if resource_type in {"CylinderMesh", "CapsuleMesh"}:
        radius = max(
            _float_property(properties, "radius", 0.5),
            _float_property(properties, "top_radius", 0.5),
            _float_property(properties, "bottom_radius", 0.5),
        )
        height = _float_property(properties, "height", 2.0)
        return Vec3(radius * 2.0, height, radius * 2.0)
    if resource_type == "SphereMesh":
        radius = _float_property(properties, "radius", 0.5)
        height = _float_property(properties, "height", radius * 2.0)
        return Vec3(radius * 2.0, height, radius * 2.0)
    return None


def _csg_dimensions(node_type: str, properties: str) -> Vec3 | None:
    if node_type == "CSGBox3D":
        return _vector3_property(properties, "size") or Vec3(2.0, 2.0, 2.0)
    if node_type == "CSGCylinder3D":
        radius = _float_property(properties, "radius", 1.0)
        height = _float_property(properties, "height", 2.0)
        return Vec3(radius * 2.0, height, radius * 2.0)
    if node_type == "CSGSphere3D":
        radius = _float_property(properties, "radius", 1.0)
        return Vec3(radius * 2.0, radius * 2.0, radius * 2.0)
    return None


def _ground_dimensions(
    node_type: str, properties: str, primitives: Mapping[str, PrimitiveResource]
) -> Vec3 | None:
    """Return ground size, limited to the primitives v0.1 recognised as ground."""
    if node_type == "CSGBox3D":
        return _vector3_property(properties, "size")
    if node_type != "MeshInstance3D":
        return None
    mesh_match = _MESH_REFERENCE_PATTERN.search(properties)
    if mesh_match is None:
        return None
    mesh_id = _decode_quoted_string(mesh_match.group(1), "mesh reference")
    primitive = primitives.get(mesh_id)
    if primitive is None or primitive.resource_type not in _GROUND_MESH_TYPES:
        return None
    return primitive.dimensions


def _is_ground_name(name: str) -> bool:
    return bool(_GROUND_NAME_PATTERN.search(_normalize_name(name)))


def _project_root(scene_path: Path, configured: str | None) -> Path:
    """Return the ``res://`` root: the nearest project.godot, else configuration."""
    for candidate in (scene_path.parent, *scene_path.parents):
        if (candidate / "project.godot").is_file():
            return candidate
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_dir():
            return expanded
    return scene_path.parent


def _resolve_resource_path(
    resource_path: str, scene_path: Path, project_root: Path
) -> Path | None:
    normalized = resource_path.replace("\\", "/")
    if normalized.startswith("uid://"):
        return None
    if normalized.startswith("res://"):
        relative = PurePosixPath(normalized[6:])
        return project_root.joinpath(*relative.parts)
    return scene_path.parent.joinpath(*PurePosixPath(normalized).parts)


# -- Block scanning ----------------------------------------------------------


@dataclass(frozen=True)
class _SceneBlock:
    """One ``[tag]`` header and the property lines that follow it."""

    index: int
    end_index: int
    tag_name: str
    tag_body: str
    header: str
    properties: str

    @property
    def line_number(self) -> int:
        """Return the 1-based line number of the tag."""
        return self.index + 1


def _parse_blocks(lines: Sequence[str]) -> tuple[_SceneBlock, ...]:
    blocks: list[_SceneBlock] = []
    current: tuple[int, str, re.Match[str]] | None = None
    properties: list[str] = []

    def close(end_index: int) -> None:
        index, header, match = current
        blocks.append(
            _SceneBlock(
                index=index,
                end_index=end_index,
                tag_name=match.group("name"),
                tag_body=match.group("body"),
                header=header,
                properties="\n".join(properties),
            )
        )

    for index, line in enumerate(lines):
        stripped = line.strip()
        tag_match = (
            _TAG_PATTERN.match(line)
            if stripped and not stripped.startswith(";")
            else None
        )
        if tag_match is not None:
            if current is not None:
                close(index)
            current = (index, line, tag_match)
            properties = []
        elif current is not None:
            properties.append(line)

    if current is not None:
        close(len(lines))
    return tuple(blocks)


def _check_header(blocks: Sequence[_SceneBlock], lines: Sequence[str]) -> None:
    if not blocks:
        raise AdapterError("TSCN scene is empty or has no gd_scene header.")
    preamble = lines[: blocks[0].index]
    if blocks[0].tag_name != "gd_scene" or any(
        line.strip() and not line.strip().startswith(";") for line in preamble
    ):
        raise AdapterError("The first TSCN entry must be a gd_scene header.")
    match = re.search(r"(?:^|\s)format\s*=\s*(\d+)(?:\s|$)", blocks[0].tag_body)
    if match is None:
        raise AdapterError("Missing 'format' in gd_scene header.")
    scene_format = int(match.group(1))
    if scene_format != SUPPORTED_SCENE_FORMAT:
        raise AdapterError(
            f"Unsupported TSCN format {scene_format}; Godot 4 format 3 is required."
        )


def _external_resources(blocks: Sequence[_SceneBlock]) -> dict[str, ExternalResource]:
    resources: dict[str, ExternalResource] = {}
    for block in blocks:
        if block.tag_name != "ext_resource":
            continue
        context = f"ext_resource declaration on line {block.line_number}"
        resource_id = _quoted_attribute(block.tag_body, "id", context)
        if resource_id in resources:
            raise AdapterError(
                f"Duplicate ext_resource id '{resource_id}' on line {block.line_number}."
            )
        resources[resource_id] = ExternalResource(
            resource_id=resource_id,
            path=_quoted_attribute(block.tag_body, "path", context),
            resource_type=_quoted_attribute(block.tag_body, "type", context),
        )
    return resources


def _check_references(
    blocks: Sequence[_SceneBlock], resources: Mapping[str, ExternalResource]
) -> None:
    """Reject node data that references an undeclared external resource."""
    references = [
        resource_id
        for block in blocks
        if block.tag_name == "node"
        for resource_id in _find_ext_resource_references(
            f"{block.header}\n{block.properties}"
        )
    ]
    unknown = sorted(set(references).difference(resources))
    if unknown:
        raise AdapterError(
            f"Node data references undefined ext_resource ID(s): {', '.join(unknown)}"
        )


def _find_ext_resource_references(text: str) -> list[str]:
    """Find ExtResource IDs while ignoring comments and ordinary strings."""
    references: list[str] = []
    index = 0

    while index < len(text):
        character = text[index]

        if character == ";":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline + 1
            continue

        if character == '"':
            index = _skip_quoted_string(text, index)
            continue

        token = "ExtResource"
        if text.startswith(token, index):
            before_is_identifier = index > 0 and (
                text[index - 1].isalnum() or text[index - 1] == "_"
            )
            cursor = index + len(token)
            after_is_identifier = cursor < len(text) and (
                text[cursor].isalnum() or text[cursor] == "_"
            )
            if before_is_identifier or after_is_identifier:
                index += 1
                continue

            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text) or text[cursor] != "(":
                index += len(token)
                continue

            cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1

            if cursor >= len(text) or text[cursor] != '"':
                raise AdapterError(
                    "ExtResource references must use a quoted Godot 4 resource ID."
                )

            end = _skip_quoted_string(text, cursor)
            resource_id = _decode_quoted_string(
                text[cursor + 1 : end - 1], "ExtResource reference"
            )
            cursor = end
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text) or text[cursor] != ")":
                raise AdapterError("Malformed ExtResource reference in node data.")

            references.append(resource_id)
            index = cursor + 1
            continue

        index += 1

    return references


def _skip_quoted_string(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return index + 1
        else:
            index += 1
    raise AdapterError("Unterminated quoted string in node data.")


def _primitive_resources(
    blocks: Sequence[_SceneBlock],
) -> dict[str, PrimitiveResource]:
    resources: dict[str, PrimitiveResource] = {}
    for block in blocks:
        if block.tag_name != "sub_resource":
            continue
        resource_id = _optional_quoted_attribute(block.tag_body, "id")
        resource_type = _optional_quoted_attribute(block.tag_body, "type") or ""
        dimensions = _primitive_dimensions(resource_type, block.properties)
        if resource_id is not None and dimensions is not None:
            resources[resource_id] = PrimitiveResource(
                resource_id=resource_id,
                resource_type=resource_type,
                dimensions=dimensions,
            )
    return resources


# -- Node hierarchy and transforms -------------------------------------------


def _resolve_nodes(node_blocks: Sequence[_SceneBlock]) -> tuple[GodotNode, ...]:
    """Build every node with its world transform composed from its ancestors."""
    root_name = _required_quoted_attribute(
        node_blocks[0].tag_body, "name", node_blocks[0].line_number
    )
    nodes: list[GodotNode] = []
    world_transforms: dict[str, Transform] = {}

    for block in node_blocks:
        name = _required_quoted_attribute(block.tag_body, "name", block.line_number)
        parent = _optional_quoted_attribute(block.tag_body, "parent")
        if parent is None:
            node_path, parent_path = name, None
        elif parent == ".":
            node_path, parent_path = f"{root_name}/{name}", root_name
        else:
            normalized_parent = parent.strip("/")
            node_path = f"{root_name}/{normalized_parent}/{name}"
            parent_path = f"{root_name}/{normalized_parent}"

        local = _parse_local_transform(block.properties, node_path)
        top_level = bool(_TOP_LEVEL_PATTERN.search(block.properties))
        parent_transform = world_transforms.get(parent_path, Transform.identity())
        world = local if top_level else parent_transform.compose(local)
        world_transforms[node_path] = world

        resource_match = _INSTANCE_REFERENCE_PATTERN.search(block.tag_body)
        nodes.append(
            GodotNode(
                name=name,
                node_path=node_path,
                parent_path=parent_path,
                node_type=_optional_quoted_attribute(block.tag_body, "type")
                or INSTANCED_SCENE_TYPE,
                is_instance=bool(_INSTANCE_PATTERN.search(block.tag_body)),
                resource_id=(
                    _decode_quoted_string(
                        resource_match.group(1), "node instance reference"
                    )
                    if resource_match is not None
                    else None
                ),
                top_level=top_level,
                local=local,
                world=world,
                tag_body=block.tag_body,
                properties=block.properties,
                header_index=block.index,
                property_end=block.end_index,
            )
        )
    return tuple(nodes)


def _parse_local_transform(properties: str, node_path: str) -> Transform:
    """Read a node's local transform, preferring Transform3D over Euler properties."""
    transform_match = TRANSFORM_PATTERN.search(properties)
    position_match = POSITION_PATTERN.search(properties)

    if transform_match is not None:
        values = _numbers(
            transform_match.group(1), 12, f"Transform3D for node '{node_path}'"
        )
        basis = Basis(
            Vec3(*values[0:3]), Vec3(*values[3:6]), Vec3(*values[6:9])
        )
        origin = (
            _vector_from_match(position_match)
            if position_match is not None
            else Vec3(*values[9:12])
        )
        return Transform(basis, origin)

    position = (
        _vector_from_match(position_match) if position_match is not None else Vec3.zero()
    )
    rotation_match = _ROTATION_PATTERN.search(properties)
    scale_match = _SCALE_PATTERN.search(properties)
    return Transform(
        Basis.from_euler_scale(
            _vector_from_match(rotation_match) if rotation_match else Vec3.zero(),
            _vector_from_match(scale_match) if scale_match else Vec3.one(),
        ),
        position,
    )


def _vector_from_match(match: re.Match[str]) -> Vec3:
    return Vec3(float(match.group(1)), float(match.group(2)), float(match.group(3)))


def _solve(basis: Basis, target: Vec3) -> Vec3 | None:
    """Return the local vector a basis maps onto ``target``, by Cramer's rule."""
    x, y, z = basis.x_axis, basis.y_axis, basis.z_axis
    determinant = _determinant(x, y, z)
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        return None
    return Vec3(
        _determinant(target, y, z) / determinant,
        _determinant(x, target, z) / determinant,
        _determinant(x, y, target) / determinant,
    )


def _determinant(x: Vec3, y: Vec3, z: Vec3) -> float:
    return (
        x.x * (y.y * z.z - y.z * z.y)
        - y.x * (x.y * z.z - x.z * z.y)
        + z.x * (x.y * y.z - x.z * y.y)
    )


# -- Property readers --------------------------------------------------------


def _vector3_property(properties: str, name: str) -> Vec3 | None:
    for match in _VECTOR3_PROPERTY.finditer(properties):
        if match.group("name") == name:
            return Vec3(
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
        _FLOAT_PROPERTY_TEMPLATE.format(name=re.escape(name), number=NUMBER),
        re.MULTILINE,
    )
    match = pattern.search(properties)
    return abs(float(match.group(1))) if match else default


def _aabb_dimensions(properties: str) -> Vec3 | None:
    match = _AABB_PATTERN.search(properties)
    if match is None:
        return None
    dimensions = Vec3(
        abs(float(match.group(1))),
        abs(float(match.group(2))),
        abs(float(match.group(3))),
    )
    return None if math.isclose(dimensions.length(), 0.0) else dimensions


def _numbers(value: str, expected: int, context: str) -> list[float]:
    parsed = [float(number) for number in re.findall(NUMBER, value)]
    if len(parsed) != expected:
        raise AdapterError(
            f"Expected {expected} numeric values in {context}, found {len(parsed)}."
        )
    return parsed


def _decode_quoted_string(raw_value: str, context: str) -> str:
    try:
        return json.loads(f'"{raw_value}"')
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Invalid quoted string in {context}.") from exc


def _optional_quoted_attribute(tag_body: str, name: str) -> str | None:
    match = re.search(
        rf'(?:^|\s){re.escape(name)}\s*=\s*"((?:\\.|[^"\\])*)"', tag_body
    )
    if match is None:
        return None
    return _decode_quoted_string(match.group(1), f"'{name}' attribute")


def _quoted_attribute(tag_body: str, name: str, context: str) -> str:
    value = _optional_quoted_attribute(tag_body, name)
    if value is None:
        raise AdapterError(f"Missing '{name}' in {context}.")
    return value


def _required_quoted_attribute(tag_body: str, name: str, line_number: int) -> str:
    value = _optional_quoted_attribute(tag_body, name)
    if value is None:
        raise AdapterError(
            f"Missing '{name}' in node declaration on line {line_number}."
        )
    return value
