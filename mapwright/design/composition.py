"""Visual mass distribution: where a level's bulk sits in the playable area.

This is a geometric stand-in for what an isometric capture would show. Mass is
measured as world AABB volume, and height is tracked separately because a tall
object dominates a skyline out of all proportion to its volume. Nothing here
looks at an image, so every number it produces is a proxy, not a render.

North is +Z and east is +X, matching the top-down occupancy map, so a reported
side always means the same thing as the side of the rendered view.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.metrics import Distribution
from mapwright.core.scene_ir import SceneObject

EAST = "east"
WEST = "west"
NORTH = "north"
SOUTH = "south"

#: The four sides, in a fixed reporting order.
SIDES = (EAST, WEST, NORTH, SOUTH)

#: The four quadrants, in a fixed reporting order.
QUADRANTS = ("north_east", "north_west", "south_east", "south_west")

#: The side a designer would move content toward to correct a bias.
OPPOSITE_SIDE = {EAST: WEST, WEST: EAST, NORTH: SOUTH, SOUTH: NORTH}

_EAST_WEST = "east_west"
_NORTH_SOUTH = "north_south"


@dataclass(frozen=True)
class MassSlice:
    """One part of the playable area and its share of the level's bulk."""

    name: str
    object_count: int
    tall_count: int
    volume: float
    volume_share: float
    height_sum: float
    height_share: float

    @property
    def empty(self) -> bool:
        """Return whether this part holds no measured content at all."""
        return self.object_count == 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "name": self.name,
            "object_count": self.object_count,
            "tall_count": self.tall_count,
            "volume": round(self.volume, 3),
            "volume_share": round(self.volume_share, 2),
            "height_sum": round(self.height_sum, 3),
            "height_share": round(self.height_share, 2),
        }


@dataclass(frozen=True)
class AxisSplit:
    """The two halves of the playable area along one axis, compared."""

    axis: str
    low: MassSlice
    high: MassSlice

    @property
    def heavier(self) -> MassSlice:
        """Return the half carrying the most volume, low side on a tie."""
        return self.high if self.high.volume > self.low.volume else self.low

    @property
    def lighter(self) -> MassSlice:
        """Return the half carrying the least volume."""
        return self.low if self.heavier is self.high else self.high

    @property
    def dominant_side(self) -> str:
        """Return the name of the heavier half."""
        return self.heavier.name

    @property
    def dominant_share(self) -> float:
        """Return the heavier half's percentage of total volume."""
        return self.heavier.volume_share

    @property
    def tall_total(self) -> int:
        """Return how many tall objects this axis split covers."""
        return self.low.tall_count + self.high.tall_count

    @property
    def tall_dominant(self) -> MassSlice:
        """Return the half holding the most tall objects, low side on a tie."""
        return self.high if self.high.tall_count > self.low.tall_count else self.low

    @property
    def tall_dominant_share(self) -> float:
        """Return the tall-object percentage held by the heavier-in-height half."""
        if self.tall_total == 0:
            return 0.0
        return 100.0 * self.tall_dominant.tall_count / self.tall_total

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "axis": self.axis,
            "low": self.low.to_dict(),
            "high": self.high.to_dict(),
            "dominant_side": self.dominant_side,
            "dominant_share": round(self.dominant_share, 2),
            "tall_dominant_side": self.tall_dominant.name,
            "tall_dominant_share": round(self.tall_dominant_share, 2),
        }


@dataclass(frozen=True)
class CompositionAnalysis:
    """How visual mass is distributed across halves and quadrants."""

    measured: bool
    reason: str | None
    area: Bounds | None
    object_count: int
    tall_count: int
    tall_height: float
    total_volume: float
    axes: tuple[AxisSplit, ...]
    quadrants: tuple[MassSlice, ...]
    quadrant_imbalance: float
    empty_quadrants: tuple[str, ...]
    tall_distribution: tuple[tuple[str, float], ...]
    dominant_side: str | None
    dominant_share: float

    def axis(self, name: str) -> AxisSplit | None:
        """Return one axis split by name."""
        return next((split for split in self.axes if split.axis == name), None)

    def tall_share(self, side: str) -> float:
        """Return the percentage of tall objects standing on one side."""
        return dict(self.tall_distribution).get(side, 0.0)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "measured": self.measured,
            "reason": self.reason,
            "area": self.area.to_dict() if self.area is not None else None,
            "object_count": self.object_count,
            "tall_count": self.tall_count,
            "tall_height": round(self.tall_height, 3),
            "total_volume": round(self.total_volume, 3),
            "axes": [split.to_dict() for split in self.axes],
            "quadrants": [part.to_dict() for part in self.quadrants],
            "quadrant_imbalance": round(self.quadrant_imbalance, 2),
            "empty_quadrants": list(self.empty_quadrants),
            "tall_distribution": {
                side: round(share, 2) for side, share in self.tall_distribution
            },
            "dominant_side": self.dominant_side,
            "dominant_share": round(self.dominant_share, 2),
        }


def analyze_composition(context: AnalysisContext) -> CompositionAnalysis:
    """Measure how object mass and height are spread across the playable area."""
    area = context.ground
    if area is None:
        return _unmeasured("the scene declares no playable area to divide")

    tall_height = context.config.thresholds.cover_height_max
    placed = tuple(
        sorted(
            (
                (obj, box)
                for obj in context.scene.props
                if (box := obj.world_bounds()) is not None
            ),
            key=lambda item: item[0].id,
        )
    )
    if not placed:
        return _unmeasured(
            "the scene places no measured objects, so it has no visual mass",
            area=area,
            tall_height=tall_height,
        )

    total_volume = sum(_volume(box) for _, box in placed)
    total_height = sum(box.height for _, box in placed)
    tall = tuple(item for item in placed if item[1].height >= tall_height)
    center = area.center

    def slice_of(name: str, keep: Callable[[Bounds], bool]) -> MassSlice:
        members = [(obj, box) for obj, box in placed if keep(box)]
        volume = sum(_volume(box) for _, box in members)
        height_sum = sum(box.height for _, box in members)
        return MassSlice(
            name=name,
            object_count=len(members),
            tall_count=sum(1 for _, box in members if box.height >= tall_height),
            volume=volume,
            volume_share=_share(volume, total_volume),
            height_sum=height_sum,
            height_share=_share(height_sum, total_height),
        )

    sides = {
        EAST: slice_of(EAST, lambda box: box.center.x >= center.x),
        WEST: slice_of(WEST, lambda box: box.center.x < center.x),
        NORTH: slice_of(NORTH, lambda box: box.center.z >= center.z),
        SOUTH: slice_of(SOUTH, lambda box: box.center.z < center.z),
    }
    axes = (
        AxisSplit(axis=_EAST_WEST, low=sides[WEST], high=sides[EAST]),
        AxisSplit(axis=_NORTH_SOUTH, low=sides[SOUTH], high=sides[NORTH]),
    )
    quadrants = tuple(
        slice_of(name, keep)
        for name, keep in (
            ("north_east", lambda box: box.center.x >= center.x and box.center.z >= center.z),
            ("north_west", lambda box: box.center.x < center.x and box.center.z >= center.z),
            ("south_east", lambda box: box.center.x >= center.x and box.center.z < center.z),
            ("south_west", lambda box: box.center.x < center.x and box.center.z < center.z),
        )
    )
    dominant = max(
        (split.heavier for split in axes),
        key=lambda part: (part.volume_share, part.name),
    )
    return CompositionAnalysis(
        measured=True,
        reason=None,
        area=area,
        object_count=len(placed),
        tall_count=len(tall),
        tall_height=tall_height,
        total_volume=total_volume,
        axes=axes,
        quadrants=quadrants,
        quadrant_imbalance=Distribution.of(
            [part.object_count for part in quadrants]
        ).imbalance,
        empty_quadrants=tuple(part.name for part in quadrants if part.empty),
        tall_distribution=tuple(
            (side, _share(sides[side].tall_count, len(tall))) for side in SIDES
        ),
        dominant_side=dominant.name,
        dominant_share=dominant.volume_share,
    )


def _unmeasured(
    reason: str, area: Bounds | None = None, tall_height: float = 0.0
) -> CompositionAnalysis:
    """Return an empty analysis carrying the reason nothing could be measured."""
    return CompositionAnalysis(
        measured=False,
        reason=reason,
        area=area,
        object_count=0,
        tall_count=0,
        tall_height=tall_height,
        total_volume=0.0,
        axes=(),
        quadrants=(),
        quadrant_imbalance=0.0,
        empty_quadrants=(),
        tall_distribution=tuple((side, 0.0) for side in SIDES),
        dominant_side=None,
        dominant_share=0.0,
    )


def _volume(box: Bounds) -> float:
    """Return the world AABB volume used as the visual-mass proxy."""
    return box.width * box.height * box.depth


def _share(part: float, total: float) -> float:
    """Return a part's percentage of a total, zero when there is no total."""
    return 100.0 * part / total if total > 0 else 0.0
