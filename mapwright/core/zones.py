"""Zone membership and per-zone spatial summaries.

A level is not only geometry: a zone states what a space is for and how it
should feel. These helpers turn that declared intent into the per-zone
measurements the pacing, encounter, and visual analyzers read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.metrics import OccupancyGrid, clearance_at
from mapwright.core.scene_ir import MarkerPoint, SceneIR, SceneObject, Zone


#: Smallest top surface that reads as a position rather than a foothold.
#: A one-metre crate clears the height test; standing on it is not high ground.
MINIMUM_STAND_AREA = 2.0


@dataclass(frozen=True)
class ZoneSummary:
    """What one zone actually contains, measured rather than declared."""

    zone: Zone
    objects: tuple[SceneObject, ...]
    markers: tuple[MarkerPoint, ...]
    bounds: Bounds | None
    free_cells: int
    cell_area: float
    cover_objects: tuple[SceneObject, ...]
    tall_objects: tuple[SceneObject, ...]
    ground_height: float
    highest_stand: float

    @property
    def name(self) -> str:
        """Return the zone name."""
        return self.zone.name

    @property
    def free_area(self) -> float:
        """Return the walkable area inside the zone."""
        return self.free_cells * self.cell_area

    @property
    def object_count(self) -> int:
        """Return how many objects sit in the zone."""
        return len(self.objects)

    @property
    def prop_density(self) -> float:
        """Return objects per square metre of walkable space."""
        return self.object_count / self.free_area if self.free_area > 0 else 0.0

    @property
    def engagement_distance(self) -> float:
        """Return the longest sightline the zone footprint can support."""
        if self.bounds is None:
            return 0.0
        return (self.bounds.width**2 + self.bounds.depth**2) ** 0.5

    @property
    def has_high_ground(self) -> bool:
        """Return whether the zone offers a meaningfully raised position."""
        return self.highest_stand > self.ground_height


def summarize_zone(
    scene: SceneIR,
    zone: Zone,
    grid: OccupancyGrid | None,
    cover_height_range: tuple[float, float],
    high_ground_delta: float,
) -> ZoneSummary:
    """Measure one zone's contents against the player-scale thresholds."""
    objects = scene.objects_in_zone(zone)
    markers = tuple(
        point
        for point in scene.all_markers
        if point.zone == zone.name or (point.zone is None and zone.contains(point.position))
    )
    bounds = zone.bounds
    if bounds is None:
        boxes = [box for obj in objects if (box := obj.world_bounds()) is not None]
        bounds = Bounds.enclosing(boxes)

    free_cells = 0
    cell_area = 0.0
    if grid is not None and bounds is not None:
        cell_area = grid.cell_area
        free_cells = sum(
            1
            for row in range(grid.rows)
            for column in range(grid.columns)
            if not grid.blocked[row][column]
            and bounds.contains_xz(grid.cell_center(column, row))
        )

    minimum_cover, maximum_cover = cover_height_range
    cover: list[SceneObject] = []
    tall: list[SceneObject] = []
    ground_height = 0.0
    heights = [
        box.min.y for obj in objects if (box := obj.world_bounds()) is not None
    ]
    if heights:
        ground_height = min(heights)
    highest_stand = ground_height
    for obj in objects:
        box = obj.world_bounds()
        if box is None or not obj.obstructs:
            continue
        height = box.height
        if minimum_cover <= height <= maximum_cover:
            cover.append(obj)
        if height >= maximum_cover:
            tall.append(obj)
        if (
            box.max.y - ground_height >= high_ground_delta
            and box.area_xz >= MINIMUM_STAND_AREA
        ):
            highest_stand = max(highest_stand, box.max.y)

    return ZoneSummary(
        zone=zone,
        objects=objects,
        markers=markers,
        bounds=bounds,
        free_cells=free_cells,
        cell_area=cell_area,
        cover_objects=tuple(cover),
        tall_objects=tuple(tall),
        ground_height=ground_height,
        highest_stand=highest_stand,
    )


def summarize_zones(
    scene: SceneIR,
    grid: OccupancyGrid | None,
    cover_height_range: tuple[float, float],
    high_ground_delta: float,
) -> tuple[ZoneSummary, ...]:
    """Measure every declared zone, in declaration order."""
    return tuple(
        summarize_zone(scene, zone, grid, cover_height_range, high_ground_delta)
        for zone in scene.zones
    )


def zone_entrance_width(
    grid: OccupancyGrid, bounds: Bounds, samples: Sequence[Vec3]
) -> float:
    """Return the narrowest physical gap among sampled zone boundary crossings."""
    if not samples:
        return 0.0
    return 2.0 * min(
        clearance_at(point.x, point.z, grid.footprints, grid.ground) for point in samples
    )
