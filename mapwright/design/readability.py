"""Whether a space reads: silhouette variety, clutter, emptiness, landmarks.

These are geometric stand-ins for what a reviewer would see in a capture. A
region that reads well has objects of visibly different heights, room to move
between them, no large dead patches, and one element clearly larger than its
neighbours to steer by. Each of those is measurable from the Scene IR alone;
none of them is a substitute for looking at an image.

Measurement happens per declared zone, because a zone states what a space is
for. When a scene declares none, the playable area is split into glance-sized
regions instead so an unannotated level is still measurable.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.metrics import OccupancyGrid
from mapwright.core.scene_ir import SceneObject
from mapwright.core.zones import ZoneSummary

#: Target span of a derived region: roughly what a player takes in at a glance.
REGION_TARGET_SPAN = 30.0

#: Most regions per axis, so a large map stays reportable.
MAXIMUM_REGION_SPLIT = 3

#: Emptiness is counted over patches this many density cells across. A patch
#: the size of one prop is empty on any map; a patch several strides wide with
#: nothing in it is dead ground.
EMPTY_PATCH_CELLS = 3

_COLUMN_LABELS = {1: ("",), 2: ("west", "east"), 3: ("west", "center", "east")}
_ROW_LABELS = {1: ("",), 2: ("south", "north"), 3: ("south", "center", "north")}


@dataclass(frozen=True)
class RegionReadability:
    """How legible one zone or derived region is, measured geometrically."""

    name: str
    declared: bool
    bounds: Bounds | None
    object_count: int
    tall_count: int
    walkable_area: float
    walkable_measured: bool
    prop_density: float
    clutter_index: float
    height_mean: float
    height_min: float
    height_max: float
    silhouette_variety: float
    empty_ratio: float
    cell_count: int
    empty_cells: int
    median_size: float
    landmark_strength: float
    landmark_id: str | None
    landmark_name: str | None
    repeat_share: float
    repeat_count: int
    repeat_label: str | None

    @property
    def populated(self) -> bool:
        """Return whether the region holds any measured content."""
        return self.object_count > 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "name": self.name,
            "declared": self.declared,
            "bounds": self.bounds.to_dict() if self.bounds is not None else None,
            "object_count": self.object_count,
            "tall_count": self.tall_count,
            "walkable_area": round(self.walkable_area, 2),
            "walkable_measured": self.walkable_measured,
            "prop_density": round(self.prop_density, 4),
            "clutter_index": round(self.clutter_index, 4),
            "height_mean": round(self.height_mean, 3),
            "height_min": round(self.height_min, 3),
            "height_max": round(self.height_max, 3),
            "silhouette_variety": round(self.silhouette_variety, 4),
            "empty_ratio": round(self.empty_ratio, 2),
            "cell_count": self.cell_count,
            "empty_cells": self.empty_cells,
            "median_size": round(self.median_size, 3),
            "landmark_strength": round(self.landmark_strength, 3),
            "landmark_id": self.landmark_id,
            "landmark_name": self.landmark_name,
            "repeat_share": round(self.repeat_share, 2),
            "repeat_count": self.repeat_count,
            "repeat_label": self.repeat_label,
        }


@dataclass(frozen=True)
class ReadabilityAnalysis:
    """Per-region legibility measurements for one scene."""

    measured: bool
    reason: str | None
    source: str
    regions: tuple[RegionReadability, ...]
    player_footprint: float
    cell_size: float
    walkable_measured: bool

    def region(self, name: str) -> RegionReadability | None:
        """Return one region by name."""
        return next((item for item in self.regions if item.name == name), None)

    @property
    def populated_regions(self) -> tuple[RegionReadability, ...]:
        """Return only the regions that hold measured content."""
        return tuple(item for item in self.regions if item.populated)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "measured": self.measured,
            "reason": self.reason,
            "source": self.source,
            "player_footprint": round(self.player_footprint, 4),
            "cell_size": round(self.cell_size, 3),
            "walkable_measured": self.walkable_measured,
            "regions": [item.to_dict() for item in self.regions],
        }


def analyze_readability(context: AnalysisContext) -> ReadabilityAnalysis:
    """Measure silhouette, clutter, emptiness, and landmark strength per region."""
    scene = context.scene
    player = context.config.player
    thresholds = context.config.thresholds
    footprint = math.pi * player.radius**2
    cell_size = max(EMPTY_PATCH_CELLS * thresholds.density_cell_size, player.diameter)
    grid = context.grid

    tall_height = thresholds.cover_height_max

    if scene.zones:
        regions = tuple(
            _measure_zone(summary, grid, footprint, cell_size, tall_height)
            for summary in context.zone_summaries
        )
        source = "zones"
    else:
        area = context.ground
        if area is None:
            return ReadabilityAnalysis(
                measured=False,
                reason="the scene declares no playable area to divide into regions",
                source="none",
                regions=(),
                player_footprint=footprint,
                cell_size=cell_size,
                walkable_measured=grid is not None,
            )
        placed = _placed_objects(scene.props)
        regions = tuple(
            _measure_region(name, bounds, placed, grid, footprint, cell_size, tall_height)
            for name, bounds in _derive_regions(area)
        )
        source = "grid_regions"

    return ReadabilityAnalysis(
        measured=bool(regions),
        reason=None if regions else "the scene declares no zones and no playable area",
        source=source,
        regions=tuple(sorted(regions, key=lambda item: item.name)),
        player_footprint=footprint,
        cell_size=cell_size,
        walkable_measured=grid is not None,
    )


def _measure_zone(
    summary: ZoneSummary,
    grid: OccupancyGrid | None,
    footprint: float,
    cell_size: float,
    tall_height: float,
) -> RegionReadability:
    """Measure one declared zone, reusing the walkable area already counted."""
    placed = _placed_objects(summary.objects)
    walkable = summary.free_area
    measured = grid is not None and walkable > 0
    if not measured and summary.bounds is not None:
        walkable = summary.bounds.area_xz
    return _build(
        name=summary.name,
        declared=True,
        bounds=summary.bounds,
        placed=placed,
        walkable_area=walkable,
        walkable_measured=measured,
        footprint=footprint,
        cell_size=cell_size,
        tall_height=tall_height,
    )


def _measure_region(
    name: str,
    bounds: Bounds,
    placed: Sequence[tuple[SceneObject, Bounds]],
    grid: OccupancyGrid | None,
    footprint: float,
    cell_size: float,
    tall_height: float,
) -> RegionReadability:
    """Measure one derived region of the playable area."""
    members = [item for item in placed if bounds.contains_xz(item[1].center)]
    walkable = bounds.area_xz
    measured = False
    if grid is not None:
        free_cells = sum(
            1
            for row in range(grid.rows)
            for column in range(grid.columns)
            if not grid.blocked[row][column]
            and bounds.contains_xz(grid.cell_center(column, row))
        )
        if free_cells:
            walkable = free_cells * grid.cell_area
            measured = True
    return _build(
        name=name,
        declared=False,
        bounds=bounds,
        placed=members,
        walkable_area=walkable,
        walkable_measured=measured,
        footprint=footprint,
        cell_size=cell_size,
        tall_height=tall_height,
    )


def _build(
    name: str,
    declared: bool,
    bounds: Bounds | None,
    placed: Sequence[tuple[SceneObject, Bounds]],
    walkable_area: float,
    walkable_measured: bool,
    footprint: float,
    cell_size: float,
    tall_height: float,
) -> RegionReadability:
    """Turn one region's object list into its readability measurements."""
    heights = [box.height for _, box in placed]
    sizes = [obj.size_score() for obj, _ in placed]
    density = len(placed) / walkable_area if walkable_area > 0 else 0.0
    mean_height = statistics.fmean(heights) if heights else 0.0
    deviation = statistics.pstdev(heights) if len(heights) > 1 else 0.0
    median_size = statistics.median(sizes) if sizes else 0.0
    largest = max(placed, key=lambda item: (item[0].size_score(), item[0].id), default=None)
    cell_count, empty_cells = _empty_cells(bounds, placed, cell_size)
    repeat_label, repeat_count = _dominant_copy(placed)
    return RegionReadability(
        name=name,
        declared=declared,
        bounds=bounds,
        object_count=len(placed),
        tall_count=sum(1 for height in heights if height >= tall_height),
        walkable_area=walkable_area,
        walkable_measured=walkable_measured,
        prop_density=density,
        clutter_index=density * footprint,
        height_mean=mean_height,
        height_min=min(heights) if heights else 0.0,
        height_max=max(heights) if heights else 0.0,
        silhouette_variety=deviation / mean_height if mean_height > 0 else 0.0,
        empty_ratio=100.0 * empty_cells / cell_count if cell_count else 0.0,
        cell_count=cell_count,
        empty_cells=empty_cells,
        median_size=median_size,
        landmark_strength=(
            largest[0].size_score() / median_size
            if largest is not None and median_size > 0
            else 0.0
        ),
        landmark_id=largest[0].id if largest is not None else None,
        landmark_name=largest[0].name if largest is not None else None,
        repeat_share=100.0 * repeat_count / len(placed) if placed else 0.0,
        repeat_count=repeat_count,
        repeat_label=repeat_label,
    )


def _placed_objects(
    objects: Sequence[SceneObject],
) -> tuple[tuple[SceneObject, Bounds], ...]:
    """Return measured, space-occupying objects paired with their world boxes."""
    return tuple(
        sorted(
            (
                (obj, box)
                for obj in objects
                if obj.obstructs and (box := obj.world_bounds()) is not None
            ),
            key=lambda item: item[0].id,
        )
    )


def _derive_regions(area: Bounds) -> tuple[tuple[str, Bounds], ...]:
    """Split the playable area into named, glance-sized regions."""
    columns = _split_count(area.width)
    rows = _split_count(area.depth)
    width = area.width / columns
    depth = area.depth / rows
    regions: list[tuple[str, Bounds]] = []
    for row in range(rows):
        for column in range(columns):
            bounds = Bounds(
                Vec3(
                    area.min.x + column * width,
                    area.min.y,
                    area.min.z + row * depth,
                ),
                Vec3(
                    area.min.x + (column + 1) * width,
                    area.max.y,
                    area.min.z + (row + 1) * depth,
                ),
            )
            regions.append((_region_name(rows, row, columns, column), bounds))
    return tuple(regions)


def _split_count(span: float) -> int:
    """Return how many regions one axis is divided into."""
    return max(1, min(MAXIMUM_REGION_SPLIT, round(span / REGION_TARGET_SPAN)))


def _region_name(rows: int, row: int, columns: int, column: int) -> str:
    """Return a compass name for one derived region."""
    parts = [_ROW_LABELS[rows][row], _COLUMN_LABELS[columns][column]]
    named = [part for part in parts if part]
    if not named:
        return "playable_area"
    if named == ["center", "center"]:
        return "center"
    return "_".join(dict.fromkeys(named))


def _empty_cells(
    bounds: Bounds | None,
    placed: Sequence[tuple[SceneObject, Bounds]],
    cell_size: float,
) -> tuple[int, int]:
    """Return how many cells tile a region and how many hold nothing.

    Emptiness is counted per cell rather than as uncovered floor area: a level
    is sparse when whole patches hold nothing, not when the floor between props
    is visible.
    """
    if bounds is None or cell_size <= 0 or bounds.area_xz <= 0:
        return 0, 0
    columns = max(1, math.ceil(bounds.width / cell_size))
    rows = max(1, math.ceil(bounds.depth / cell_size))
    width = bounds.width / columns
    depth = bounds.depth / rows
    empty = 0
    for row in range(rows):
        low_z = bounds.min.z + row * depth
        for column in range(columns):
            low_x = bounds.min.x + column * width
            cell = Bounds(
                Vec3(low_x, bounds.min.y, low_z),
                Vec3(low_x + width, bounds.max.y, low_z + depth),
            )
            if not any(cell.overlaps_xz(box) for _, box in placed):
                empty += 1
    return columns * rows, empty


def _dominant_copy(
    placed: Sequence[tuple[SceneObject, Bounds]]
) -> tuple[str | None, int]:
    """Return the most repeated visually identical element and its count.

    Two objects count as copies when they use the same asset at the same world
    dimensions. Rotation is ignored: a turned copy of the same crate still
    reads as the same element in a capture.
    """
    groups: dict[str, int] = {}
    labels: dict[str, str] = {}
    for obj, box in placed:
        size = f"{box.width:.1f}x{box.height:.1f}x{box.depth:.1f}"
        key = f"{obj.asset or obj.type.value}|{size}"
        groups[key] = groups.get(key, 0) + 1
        labels.setdefault(key, f"{obj.asset or obj.type.value} at {size} m")
    if not groups:
        return None, 0
    key = max(sorted(groups), key=lambda item: groups[item])
    return labels[key], groups[key]
