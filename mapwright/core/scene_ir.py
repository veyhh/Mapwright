"""Mapwright Scene IR: the engine-independent description of a playable space.

Every adapter imports into this model and every analyzer reads only from it, so
no validator ever depends on Godot, Unity, Unreal, or Blender. The IR is flat
(world-space transforms, no node hierarchy), JSON-serializable, and
deterministic: the same scene always produces the same document.

Canonical space is right-handed, Y-up, metres, with Euler rotations in
intrinsic Y-X-Z order. ``coordinate_system`` declares the convention a document
was written in; :meth:`SceneIR.from_dict` converts ``Z_UP`` documents on load.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from mapwright.core.geometry import Basis, Bounds, Vec3, world_aabb


IR_VERSION = "0.2"
SUPPORTED_COORDINATE_SYSTEMS = ("Y_UP", "Z_UP")
SUPPORTED_UNITS = ("meters", "metres", "units")


class SceneIRError(ValueError):
    """Raised when a Scene IR document is malformed or inconsistent."""


class ObjectType(str, Enum):
    """What role an object plays in the level, independent of engine class."""

    PROP = "prop"
    STRUCTURE = "structure"
    GROUND = "ground"
    COVER = "cover"
    LANDMARK = "landmark"
    BLOCKER = "blocker"
    MARKER = "marker"
    LIGHT = "light"
    CAMERA = "camera"
    VOLUME = "volume"

    @classmethod
    def parse(cls, value: Any, context: str) -> ObjectType:
        """Parse an object type, raising a listing error on unknown values."""
        try:
            return cls(str(value))
        except ValueError as exc:
            supported = ", ".join(item.value for item in cls)
            raise SceneIRError(
                f"Unknown object type '{value}' in {context}. Supported: {supported}."
            ) from exc


#: Types that occupy space a player must walk around.
OBSTRUCTING_TYPES = frozenset(
    {
        ObjectType.PROP,
        ObjectType.STRUCTURE,
        ObjectType.COVER,
        ObjectType.LANDMARK,
        ObjectType.BLOCKER,
    }
)

#: Types that contribute walkable surface rather than obstruction.
SURFACE_TYPES = frozenset({ObjectType.GROUND})

#: Types excluded from spatial analysis entirely.
NON_SPATIAL_TYPES = frozenset({ObjectType.MARKER, ObjectType.LIGHT, ObjectType.CAMERA})


class PacingLevel(str, Enum):
    """Intended experiential intensity of a zone."""

    CALM = "calm"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INTENSE = "intense"

    @property
    def intensity(self) -> int:
        """Return a 0-4 ordinal used for pacing sequence maths."""
        return _PACING_INTENSITY[self]

    @classmethod
    def parse(cls, value: Any, context: str) -> PacingLevel:
        """Parse a pacing level, raising a listing error on unknown values."""
        try:
            return cls(str(value))
        except ValueError as exc:
            supported = ", ".join(item.value for item in cls)
            raise SceneIRError(
                f"Unknown pacing level '{value}' in {context}. Supported: {supported}."
            ) from exc


_PACING_INTENSITY = {
    PacingLevel.CALM: 0,
    PacingLevel.LOW: 1,
    PacingLevel.MEDIUM: 2,
    PacingLevel.HIGH: 3,
    PacingLevel.INTENSE: 4,
}


class ZoneType(str, Enum):
    """The design purpose a zone serves."""

    ENTRY = "entry"
    EXPLORATION = "exploration"
    TRAVERSAL = "traversal"
    TENSION = "tension"
    COMBAT = "combat"
    RELIEF = "relief"
    OBJECTIVE = "objective"
    HUB = "hub"
    EXIT = "exit"

    @classmethod
    def parse(cls, value: Any, context: str) -> ZoneType:
        """Parse a zone type, raising a listing error on unknown values."""
        try:
            return cls(str(value))
        except ValueError as exc:
            supported = ", ".join(item.value for item in cls)
            raise SceneIRError(
                f"Unknown zone type '{value}' in {context}. Supported: {supported}."
            ) from exc


#: Zone types where encounter analysis applies.
COMBAT_ZONE_TYPES = frozenset({ZoneType.COMBAT, ZoneType.OBJECTIVE})

#: Zone types that let the player recover after pressure.
RELIEF_ZONE_TYPES = frozenset({ZoneType.RELIEF, ZoneType.ENTRY, ZoneType.EXIT})


class MarkerKind(str, Enum):
    """What a point of interest represents."""

    ENTRY = "entry"
    SPAWN = "spawn"
    OBJECTIVE = "objective"
    EXIT = "exit"
    LANDMARK = "landmark"


@dataclass(frozen=True)
class SceneObject:
    """One placed object with a resolved world transform.

    ``bounds`` are local, object-space extents; props conventionally rest on
    their origin, so Y runs from zero upward. ``basis`` is derived from
    ``rotation`` and ``scale``.
    """

    id: str
    name: str
    type: ObjectType
    position: Vec3
    rotation: Vec3 = field(default_factory=Vec3.zero)
    scale: Vec3 = field(default_factory=Vec3.one)
    bounds: Bounds | None = None
    asset: str | None = None
    tags: tuple[str, ...] = ()
    zone: str | None = None
    source: tuple[tuple[str, str], ...] = ()

    @property
    def basis(self) -> Basis:
        """Return the world orientation-and-scale basis."""
        return Basis.from_euler_scale(self.rotation, self.scale)

    @property
    def measured(self) -> bool:
        """Return whether this object has usable extents."""
        return self.bounds is not None

    @property
    def obstructs(self) -> bool:
        """Return whether this object blocks traversal."""
        return self.type in OBSTRUCTING_TYPES

    def world_bounds(self) -> Bounds | None:
        """Return the conservative world AABB, or ``None`` when unmeasured."""
        if self.bounds is None:
            return None
        return world_aabb(self.bounds, self.position, self.basis)

    def world_size(self) -> Vec3:
        """Return the axis-aligned space this object occupies, orientation included."""
        box = self.world_bounds()
        return box.size if box is not None else Vec3.zero()

    def intrinsic_size(self) -> Vec3:
        """Return the object's own scaled dimensions, ignoring orientation.

        Distinct from :meth:`world_size` on purpose: rotating a tree by 45°
        widens its axis-aligned box but does not make the tree bigger. Size
        hierarchy reads this; clearance and occlusion read the world box.
        """
        if self.bounds is None:
            return Vec3.zero()
        size = self.bounds.size
        return Vec3(
            abs(size.x * self.scale.x),
            abs(size.y * self.scale.y),
            abs(size.z * self.scale.z),
        )

    def size_score(self) -> float:
        """Return the diagonal of the intrinsic size, the landmark heuristic."""
        return self.intrinsic_size().length()

    def source_value(self, key: str) -> str | None:
        """Return one engine-specific provenance value, when present."""
        return dict(self.source).get(key)

    def moved_to(self, position: Vec3) -> SceneObject:
        """Return a copy relocated to a new world position."""
        return replace(self, position=position)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "position": self.position.to_list(),
            "rotation": self.rotation.to_list(),
            "scale": self.scale.to_list(),
        }
        if self.asset is not None:
            data["asset"] = self.asset
        if self.bounds is not None:
            data["bounds"] = self.bounds.to_dict()
        if self.tags:
            data["tags"] = list(self.tags)
        if self.zone is not None:
            data["zone"] = self.zone
        if self.source:
            data["source"] = dict(self.source)
        return data

    @classmethod
    def from_dict(cls, data: Any, context: str) -> SceneObject:
        """Build an object from a Scene IR mapping."""
        if not isinstance(data, dict):
            raise SceneIRError(f"{context} must be a mapping.")
        identifier = _required_string(data, "id", context)
        item_context = f"object '{identifier}'"
        tags = data.get("tags", ())
        if not isinstance(tags, (list, tuple)):
            raise SceneIRError(f"{item_context} tags must be a list.")
        source = data.get("source", {})
        if not isinstance(source, dict):
            raise SceneIRError(f"{item_context} source must be a mapping.")
        bounds_data = data.get("bounds")
        return cls(
            id=identifier,
            name=str(data.get("name", identifier)),
            type=ObjectType.parse(data.get("type", "prop"), item_context),
            position=Vec3.from_sequence(
                data.get("position", [0.0, 0.0, 0.0]), f"{item_context} position"
            ),
            rotation=Vec3.from_sequence(
                data.get("rotation", [0.0, 0.0, 0.0]), f"{item_context} rotation"
            ),
            scale=Vec3.from_sequence(
                data.get("scale", [1.0, 1.0, 1.0]), f"{item_context} scale"
            ),
            bounds=(
                Bounds.from_dict(bounds_data, f"{item_context} bounds")
                if bounds_data is not None
                else None
            ),
            asset=None if data.get("asset") is None else str(data["asset"]),
            tags=tuple(str(tag) for tag in tags),
            zone=None if data.get("zone") is None else str(data["zone"]),
            source=tuple(sorted((str(key), str(value)) for key, value in source.items())),
        )


@dataclass(frozen=True)
class Zone:
    """A named region with an explicit design purpose.

    Zones carry the intent that geometry alone cannot express: what the space is
    for, and how intense it should feel.
    """

    name: str
    type: ZoneType
    pacing: PacingLevel
    bounds: Bounds | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def is_combat(self) -> bool:
        """Return whether encounter analysis applies to this zone."""
        return self.type in COMBAT_ZONE_TYPES

    @property
    def is_relief(self) -> bool:
        """Return whether this zone lets the player recover."""
        return self.type in RELIEF_ZONE_TYPES or self.pacing is PacingLevel.CALM

    def contains(self, point: Vec3) -> bool:
        """Return whether a world point lies inside the zone footprint."""
        return self.bounds is not None and self.bounds.contains_xz(point)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type.value,
            "pacing": self.pacing.value,
        }
        if self.bounds is not None:
            data["bounds"] = self.bounds.to_dict()
        if self.description is not None:
            data["description"] = self.description
        if self.tags:
            data["tags"] = list(self.tags)
        return data

    @classmethod
    def from_dict(cls, data: Any, context: str) -> Zone:
        """Build a zone from a Scene IR mapping."""
        if not isinstance(data, dict):
            raise SceneIRError(f"{context} must be a mapping.")
        name = _required_string(data, "name", context)
        item_context = f"zone '{name}'"
        bounds_data = data.get("bounds")
        tags = data.get("tags", ())
        if not isinstance(tags, (list, tuple)):
            raise SceneIRError(f"{item_context} tags must be a list.")
        return cls(
            name=name,
            type=ZoneType.parse(data.get("type", "exploration"), item_context),
            pacing=PacingLevel.parse(data.get("pacing", "medium"), item_context),
            bounds=(
                Bounds.from_dict(bounds_data, f"{item_context} bounds")
                if bounds_data is not None
                else None
            ),
            description=(
                None if data.get("description") is None else str(data["description"])
            ),
            tags=tuple(str(tag) for tag in tags),
        )


@dataclass(frozen=True)
class MarkerPoint:
    """A gameplay point of interest: an entry, spawn, objective, or exit."""

    id: str
    kind: MarkerKind
    position: Vec3
    name: str | None = None
    team: str | None = None
    zone: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Return the display name, falling back to the identifier."""
        return self.name or self.id

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        data: dict[str, Any] = {"id": self.id, "position": self.position.to_list()}
        if self.name is not None:
            data["name"] = self.name
        if self.team is not None:
            data["team"] = self.team
        if self.zone is not None:
            data["zone"] = self.zone
        if self.tags:
            data["tags"] = list(self.tags)
        return data

    @classmethod
    def from_dict(cls, data: Any, kind: MarkerKind, context: str) -> MarkerPoint:
        """Build a marker of a known kind from a Scene IR mapping."""
        if not isinstance(data, dict):
            raise SceneIRError(f"{context} must be a mapping.")
        identifier = _required_string(data, "id", context)
        item_context = f"{kind.value} point '{identifier}'"
        tags = data.get("tags", ())
        if not isinstance(tags, (list, tuple)):
            raise SceneIRError(f"{item_context} tags must be a list.")
        return cls(
            id=identifier,
            kind=kind,
            position=Vec3.from_sequence(
                data.get("position", [0.0, 0.0, 0.0]), f"{item_context} position"
            ),
            name=None if data.get("name") is None else str(data["name"]),
            team=None if data.get("team") is None else str(data["team"]),
            zone=None if data.get("zone") is None else str(data["zone"]),
            tags=tuple(str(tag) for tag in tags),
        )


@dataclass(frozen=True)
class SceneIR:
    """A complete engine-independent scene description."""

    scene: str
    objects: tuple[SceneObject, ...] = ()
    zones: tuple[Zone, ...] = ()
    entry_points: tuple[MarkerPoint, ...] = ()
    spawn_points: tuple[MarkerPoint, ...] = ()
    objectives: tuple[MarkerPoint, ...] = ()
    landmarks: tuple[MarkerPoint, ...] = ()
    exits: tuple[MarkerPoint, ...] = ()
    ground: Bounds | None = None
    units: str = "meters"
    source_engine: str = "generic"
    source_path: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        self.validate()

    # -- Derived collections -------------------------------------------------

    @property
    def obstacles(self) -> tuple[SceneObject, ...]:
        """Return measured objects that block traversal."""
        return tuple(
            obj for obj in self.objects if obj.obstructs and obj.bounds is not None
        )

    @property
    def props(self) -> tuple[SceneObject, ...]:
        """Return every object that counts as placed content."""
        return tuple(obj for obj in self.objects if obj.obstructs)

    @property
    def surfaces(self) -> tuple[SceneObject, ...]:
        """Return the walkable surface objects."""
        return tuple(obj for obj in self.objects if obj.type in SURFACE_TYPES)

    @property
    def all_markers(self) -> tuple[MarkerPoint, ...]:
        """Return every declared point of interest."""
        return (
            self.entry_points
            + self.spawn_points
            + self.objectives
            + self.exits
            + self.landmarks
        )

    @property
    def teams(self) -> tuple[str, ...]:
        """Return the distinct spawn teams in deterministic order."""
        return tuple(
            sorted({point.team for point in self.spawn_points if point.team is not None})
        )

    def object_by_id(self, identifier: str) -> SceneObject | None:
        """Return one object by identifier."""
        return next((obj for obj in self.objects if obj.id == identifier), None)

    def zone_by_name(self, name: str) -> Zone | None:
        """Return one zone by name."""
        return next((zone for zone in self.zones if zone.name == name), None)

    def objects_in_zone(self, zone: Zone) -> tuple[SceneObject, ...]:
        """Return objects assigned to, or geometrically inside, a zone."""
        return tuple(
            obj
            for obj in self.objects
            if obj.zone == zone.name or (obj.zone is None and zone.contains(obj.position))
        )

    def zone_for_point(self, point: Vec3) -> Zone | None:
        """Return the first zone whose footprint contains a world point."""
        return next((zone for zone in self.zones if zone.contains(point)), None)

    def ground_bounds(self) -> Bounds | None:
        """Return the playable area: the declared ground, or the surface union.

        Falls back to the union of every measured object when a scene declares
        neither, so generic documents stay analyzable.
        """
        if self.ground is not None:
            return self.ground
        surface_boxes = [
            box for obj in self.surfaces if (box := obj.world_bounds()) is not None
        ]
        if surface_boxes:
            return Bounds.enclosing(surface_boxes)
        object_boxes = [
            box for obj in self.objects if (box := obj.world_bounds()) is not None
        ]
        return Bounds.enclosing(object_boxes)

    # -- Mutation ------------------------------------------------------------

    def with_objects(self, objects: Iterable[SceneObject]) -> SceneIR:
        """Return a copy carrying a replacement object list."""
        return replace(self, objects=tuple(objects))

    def replace_object(self, obj: SceneObject) -> SceneIR:
        """Return a copy with one object substituted by identifier."""
        if self.object_by_id(obj.id) is None:
            raise SceneIRError(f"Cannot replace unknown object '{obj.id}'.")
        return self.with_objects(
            existing if existing.id != obj.id else obj for existing in self.objects
        )

    def remove_object(self, identifier: str) -> SceneIR:
        """Return a copy without one object."""
        if self.object_by_id(identifier) is None:
            raise SceneIRError(f"Cannot remove unknown object '{identifier}'.")
        return self.with_objects(
            obj for obj in self.objects if obj.id != identifier
        )

    # -- Validation and serialization ---------------------------------------

    def validate(self) -> None:
        """Raise :class:`SceneIRError` when the document is inconsistent."""
        if not self.scene:
            raise SceneIRError("Scene IR requires a non-empty 'scene' name.")
        if self.units not in SUPPORTED_UNITS:
            supported = ", ".join(SUPPORTED_UNITS)
            raise SceneIRError(
                f"Unsupported units '{self.units}'. Supported: {supported}."
            )
        _require_unique(
            (obj.id for obj in self.objects), "object id", SceneIRError
        )
        _require_unique((zone.name for zone in self.zones), "zone name", SceneIRError)
        _require_unique(
            (point.id for point in self.all_markers), "marker id", SceneIRError
        )
        zone_names = {zone.name for zone in self.zones}
        for obj in self.objects:
            if obj.zone is not None and obj.zone not in zone_names:
                raise SceneIRError(
                    f"Object '{obj.id}' references undefined zone '{obj.zone}'."
                )
        for point in self.all_markers:
            if point.zone is not None and point.zone not in zone_names:
                raise SceneIRError(
                    f"Marker '{point.id}' references undefined zone '{point.zone}'."
                )

    def to_dict(self) -> dict[str, Any]:
        """Return the full JSON-serializable Scene IR document."""
        data: dict[str, Any] = {
            "ir_version": IR_VERSION,
            "scene": self.scene,
            "units": self.units,
            "coordinate_system": "Y_UP",
            "source_engine": self.source_engine,
            "objects": [obj.to_dict() for obj in self.objects],
            "zones": [zone.to_dict() for zone in self.zones],
            "entry_points": [point.to_dict() for point in self.entry_points],
            "spawn_points": [point.to_dict() for point in self.spawn_points],
            "objectives": [point.to_dict() for point in self.objectives],
            "exits": [point.to_dict() for point in self.exits],
            "landmarks": [point.to_dict() for point in self.landmarks],
        }
        if self.ground is not None:
            data["ground"] = self.ground.to_dict()
        if self.source_path is not None:
            data["source_path"] = self.source_path
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data

    def to_json(self, indent: int = 2) -> str:
        """Return the Scene IR document as deterministic JSON text."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def write_json(self, path: str | Path, indent: int = 2) -> Path:
        """Write the Scene IR document to disk and return the path."""
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_json(indent) + "\n", encoding="utf-8")
        return destination

    @classmethod
    def from_dict(cls, data: Any) -> SceneIR:
        """Build a scene from a Scene IR mapping, converting Z-up documents."""
        if not isinstance(data, dict):
            raise SceneIRError("Scene IR document must be a JSON object.")
        coordinate_system = str(data.get("coordinate_system", "Y_UP")).upper()
        if coordinate_system not in SUPPORTED_COORDINATE_SYSTEMS:
            supported = ", ".join(SUPPORTED_COORDINATE_SYSTEMS)
            raise SceneIRError(
                f"Unsupported coordinate_system '{coordinate_system}'. "
                f"Supported: {supported}."
            )

        objects = tuple(
            SceneObject.from_dict(item, f"objects[{index}]")
            for index, item in enumerate(_sequence(data, "objects"))
        )
        zones = tuple(
            Zone.from_dict(item, f"zones[{index}]")
            for index, item in enumerate(_sequence(data, "zones"))
        )
        markers = {
            key: tuple(
                MarkerPoint.from_dict(item, kind, f"{key}[{index}]")
                for index, item in enumerate(_sequence(data, key))
            )
            for key, kind in (
                ("entry_points", MarkerKind.ENTRY),
                ("spawn_points", MarkerKind.SPAWN),
                ("objectives", MarkerKind.OBJECTIVE),
                ("exits", MarkerKind.EXIT),
                ("landmarks", MarkerKind.LANDMARK),
            )
        }
        ground_data = data.get("ground")
        ground = (
            Bounds.from_dict(ground_data, "ground") if ground_data is not None else None
        )
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SceneIRError("Scene IR 'metadata' must be a mapping.")

        scene = cls(
            scene=_required_string(data, "scene", "Scene IR document"),
            objects=objects,
            zones=zones,
            entry_points=markers["entry_points"],
            spawn_points=markers["spawn_points"],
            objectives=markers["objectives"],
            exits=markers["exits"],
            landmarks=markers["landmarks"],
            ground=ground,
            units=str(data.get("units", "meters")),
            source_engine=str(data.get("source_engine", "generic")),
            source_path=(
                None if data.get("source_path") is None else str(data["source_path"])
            ),
            metadata=tuple(
                sorted((str(key), str(value)) for key, value in metadata.items())
            ),
        )
        if coordinate_system == "Z_UP":
            scene = convert_z_up_to_y_up(scene)
        return scene

    @classmethod
    def from_json(cls, text: str) -> SceneIR:
        """Build a scene from Scene IR JSON text."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SceneIRError(f"Scene IR document is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def read_json(cls, path: str | Path) -> SceneIR:
        """Read a Scene IR document from disk."""
        source = Path(path).expanduser()
        try:
            text = source.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Scene IR file not found: {source}") from exc
        except UnicodeDecodeError as exc:
            raise SceneIRError(f"Scene IR file is not UTF-8 text: {source}") from exc
        scene = cls.from_json(text)
        if scene.source_path is None:
            scene = replace(scene, source_path=str(source))
        return scene


def convert_z_up_to_y_up(scene: SceneIR) -> SceneIR:
    """Convert a Z-up scene into canonical Y-up space.

    Positions map ``(x, y, z) -> (x, z, -y)``, which preserves handedness, and
    yaw about +Z becomes yaw about +Y. Pitch and roll cannot be remapped by
    component swap, so a document carrying them is rejected rather than
    silently mis-oriented.
    """
    tilted = [
        obj.id
        for obj in scene.objects
        if not math.isclose(obj.rotation.x, 0.0, abs_tol=1e-9)
        or not math.isclose(obj.rotation.y, 0.0, abs_tol=1e-9)
    ]
    if tilted:
        raise SceneIRError(
            "Z_UP scenes support yaw-only rotation; convert to Y_UP before import. "
            f"Pitched or rolled object(s): {', '.join(sorted(tilted)[:5])}"
        )

    def convert_point(point: Vec3) -> Vec3:
        return Vec3(point.x, point.z, -point.y)

    def convert_bounds(box: Bounds | None) -> Bounds | None:
        if box is None:
            return None
        first, second = convert_point(box.min), convert_point(box.max)
        return Bounds(
            Vec3(min(first.x, second.x), min(first.y, second.y), min(first.z, second.z)),
            Vec3(max(first.x, second.x), max(first.y, second.y), max(first.z, second.z)),
        )

    objects = tuple(
        replace(
            obj,
            position=convert_point(obj.position),
            rotation=Vec3(0.0, obj.rotation.z, 0.0),
            scale=Vec3(obj.scale.x, obj.scale.z, obj.scale.y),
            bounds=convert_bounds(obj.bounds),
        )
        for obj in scene.objects
    )
    zones = tuple(replace(zone, bounds=convert_bounds(zone.bounds)) for zone in scene.zones)
    markers = {
        name: tuple(
            replace(point, position=convert_point(point.position))
            for point in getattr(scene, name)
        )
        for name in ("entry_points", "spawn_points", "objectives", "exits", "landmarks")
    }
    return replace(
        scene,
        objects=objects,
        zones=zones,
        ground=convert_bounds(scene.ground),
        **markers,
    )


def _sequence(data: dict[str, Any], key: str) -> Sequence[Any]:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SceneIRError(f"Scene IR '{key}' must be a list.")
    return value


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if value is None or not str(value).strip():
        raise SceneIRError(f"{context} requires a non-empty '{key}'.")
    return str(value)


def _require_unique(values: Iterable[str], label: str, error: type[Exception]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise error(f"Duplicate {label}: '{value}'.")
        seen.add(value)
