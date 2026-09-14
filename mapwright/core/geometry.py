"""Engine-independent 3D geometry primitives used by every Mapwright analyzer.

Mapwright works in a single canonical space: right-handed, Y-up, metres, with
Euler rotations applied in Godot's intrinsic Y-X-Z order. Adapters convert into
this space on import so no analyzer ever needs engine-specific maths.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Vec3:
    """A 3D vector in canonical Y-up world space."""

    x: float
    y: float
    z: float

    def __add__(self, other: Vec3) -> Vec3:
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: Vec3) -> Vec3:
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> Vec3:
        return Vec3(self.x * scalar, self.y * scalar, self.z * scalar)

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z

    def dot(self, other: Vec3) -> float:
        """Return the dot product with another vector."""
        return self.x * other.x + self.y * other.y + self.z * other.z

    def length(self) -> float:
        """Return the vector magnitude."""
        return math.sqrt(self.x**2 + self.y**2 + self.z**2)

    def distance_to(self, other: Vec3) -> float:
        """Return the Euclidean distance to another point."""
        return (self - other).length()

    def distance_xz(self, other: Vec3) -> float:
        """Return the horizontal distance, ignoring height."""
        return math.hypot(self.x - other.x, self.z - other.z)

    def normalized(self) -> Vec3:
        """Return a unit vector, or a zero vector when the length is zero."""
        length = self.length()
        if length == 0.0:
            return Vec3(0.0, 0.0, 0.0)
        return Vec3(self.x / length, self.y / length, self.z / length)

    def to_list(self) -> list[float]:
        """Return the vector as a JSON-serializable list."""
        return [self.x, self.y, self.z]

    @classmethod
    def from_sequence(cls, values: Sequence[float], context: str = "vector") -> Vec3:
        """Build a vector from a three-element numeric sequence."""
        if len(values) != 3:
            raise ValueError(f"Expected 3 numbers in {context}, found {len(values)}.")
        numbers = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Non-numeric component in {context}: {value!r}")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"Non-finite component in {context}: {value!r}")
            numbers.append(number)
        return cls(*numbers)

    @classmethod
    def zero(cls) -> Vec3:
        """Return the origin vector."""
        return cls(0.0, 0.0, 0.0)

    @classmethod
    def one(cls) -> Vec3:
        """Return the unit-scale vector."""
        return cls(1.0, 1.0, 1.0)


@dataclass(frozen=True)
class Basis:
    """A 3x3 orientation-and-scale basis stored as three column axes."""

    x_axis: Vec3
    y_axis: Vec3
    z_axis: Vec3

    @classmethod
    def identity(cls) -> Basis:
        """Return the unrotated, unit-scaled basis."""
        return cls(Vec3(1.0, 0.0, 0.0), Vec3(0.0, 1.0, 0.0), Vec3(0.0, 0.0, 1.0))

    @classmethod
    def from_euler_scale(cls, rotation: Vec3, scale: Vec3) -> Basis:
        """Build a basis from intrinsic Y-X-Z Euler radians and per-axis scale."""
        cx, sx = math.cos(rotation.x), math.sin(rotation.x)
        cy, sy = math.cos(rotation.y), math.sin(rotation.y)
        cz, sz = math.cos(rotation.z), math.sin(rotation.z)
        rotate_x = ((1.0, 0.0, 0.0), (0.0, cx, -sx), (0.0, sx, cx))
        rotate_y = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
        rotate_z = ((cz, -sz, 0.0), (sz, cz, 0.0), (0.0, 0.0, 1.0))
        matrix = _matrix_multiply(_matrix_multiply(rotate_y, rotate_x), rotate_z)
        return cls(
            Vec3(matrix[0][0], matrix[1][0], matrix[2][0]) * scale.x,
            Vec3(matrix[0][1], matrix[1][1], matrix[2][1]) * scale.y,
            Vec3(matrix[0][2], matrix[1][2], matrix[2][2]) * scale.z,
        )

    def transform(self, vector: Vec3) -> Vec3:
        """Rotate and scale a local vector into world space."""
        return (
            self.x_axis * vector.x + self.y_axis * vector.y + self.z_axis * vector.z
        )

    def scale_lengths(self) -> Vec3:
        """Return the magnitude of each basis axis."""
        return Vec3(self.x_axis.length(), self.y_axis.length(), self.z_axis.length())

    def to_euler_scale(self) -> tuple[Vec3, Vec3]:
        """Decompose into intrinsic Y-X-Z Euler radians and per-axis scale.

        Shear is not representable and is discarded; a negative determinant is
        folded into the X scale so the rotation stays proper.
        """
        scale = self.scale_lengths()
        if _determinant(self) < 0:
            scale = Vec3(-scale.x, scale.y, scale.z)
        columns = [
            tuple(axis * (1.0 / factor)) if factor else (0.0, 0.0, 0.0)
            for axis, factor in (
                (self.x_axis, scale.x),
                (self.y_axis, scale.y),
                (self.z_axis, scale.z),
            )
        ]
        # Row-major rotation matrix rebuilt from the normalized column axes.
        matrix = tuple(
            (columns[0][index], columns[1][index], columns[2][index])
            for index in range(3)
        )
        sin_x = -matrix[1][2]
        if sin_x >= 1.0 - 1e-9:
            rotation = Vec3(math.pi * 0.5, math.atan2(matrix[0][1], matrix[0][0]), 0.0)
        elif sin_x <= -1.0 + 1e-9:
            rotation = Vec3(-math.pi * 0.5, math.atan2(matrix[0][1], matrix[0][0]), 0.0)
        else:
            rotation = Vec3(
                math.asin(sin_x),
                math.atan2(matrix[0][2], matrix[2][2]),
                math.atan2(matrix[1][0], matrix[1][1]),
            )
        return rotation, scale


@dataclass(frozen=True)
class Bounds:
    """An axis-aligned box, used for both local object extents and world areas."""

    min: Vec3
    max: Vec3

    def __post_init__(self) -> None:
        if (
            self.max.x < self.min.x
            or self.max.y < self.min.y
            or self.max.z < self.min.z
        ):
            raise ValueError("Bounds maximum must be greater than or equal to minimum.")

    @property
    def center(self) -> Vec3:
        """Return the box centre."""
        return Vec3(
            (self.min.x + self.max.x) * 0.5,
            (self.min.y + self.max.y) * 0.5,
            (self.min.z + self.max.z) * 0.5,
        )

    @property
    def size(self) -> Vec3:
        """Return the full width, height, and depth."""
        return self.max - self.min

    @property
    def width(self) -> float:
        """Return the extent along X."""
        return self.max.x - self.min.x

    @property
    def height(self) -> float:
        """Return the extent along Y."""
        return self.max.y - self.min.y

    @property
    def depth(self) -> float:
        """Return the extent along Z."""
        return self.max.z - self.min.z

    @property
    def area_xz(self) -> float:
        """Return the horizontal footprint area."""
        return self.width * self.depth

    def contains_xz(self, point: Vec3) -> bool:
        """Return whether a point lies within the horizontal bounds."""
        return self.min.x <= point.x <= self.max.x and self.min.z <= point.z <= self.max.z

    def overlaps_xz(self, other: Bounds) -> bool:
        """Return whether two boxes overlap horizontally."""
        return (
            self.min.x <= other.max.x
            and other.min.x <= self.max.x
            and self.min.z <= other.max.z
            and other.min.z <= self.max.z
        )

    def union(self, other: Bounds) -> Bounds:
        """Return the smallest box containing both boxes."""
        return Bounds(
            Vec3(
                min(self.min.x, other.min.x),
                min(self.min.y, other.min.y),
                min(self.min.z, other.min.z),
            ),
            Vec3(
                max(self.max.x, other.max.x),
                max(self.max.y, other.max.y),
                max(self.max.z, other.max.z),
            ),
        )

    def to_dict(self) -> dict[str, list[float]]:
        """Return a JSON-serializable mapping."""
        return {"min": self.min.to_list(), "max": self.max.to_list()}

    @classmethod
    def from_dict(cls, data: dict, context: str = "bounds") -> Bounds:
        """Build bounds from a ``{"min": [...], "max": [...]}`` mapping."""
        if not isinstance(data, dict) or "min" not in data or "max" not in data:
            raise ValueError(f"{context} must be a mapping with 'min' and 'max'.")
        return cls(
            Vec3.from_sequence(data["min"], f"{context}.min"),
            Vec3.from_sequence(data["max"], f"{context}.max"),
        )

    @classmethod
    def from_size(cls, size: Vec3, base_at_origin: bool = True) -> Bounds:
        """Build local bounds for a prop of the given dimensions.

        Props rest on their origin by default, matching how level assets are
        authored: X and Z are centred, Y runs upward from zero.
        """
        half_x, half_z = abs(size.x) * 0.5, abs(size.z) * 0.5
        if base_at_origin:
            return cls(Vec3(-half_x, 0.0, -half_z), Vec3(half_x, abs(size.y), half_z))
        half_y = abs(size.y) * 0.5
        return cls(Vec3(-half_x, -half_y, -half_z), Vec3(half_x, half_y, half_z))

    @classmethod
    def enclosing(cls, boxes: Iterable[Bounds]) -> Bounds | None:
        """Return the union of every box, or ``None`` when the input is empty."""
        result: Bounds | None = None
        for box in boxes:
            result = box if result is None else result.union(box)
        return result


def _matrix_multiply(
    left: tuple[tuple[float, float, float], ...],
    right: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3))
        for row in range(3)
    )


def _determinant(basis: Basis) -> float:
    x, y, z = basis.x_axis, basis.y_axis, basis.z_axis
    return (
        x.x * (y.y * z.z - y.z * z.y)
        - y.x * (x.y * z.z - x.z * z.y)
        + z.x * (x.y * y.z - x.z * y.y)
    )


def world_aabb(local: Bounds, position: Vec3, basis: Basis) -> Bounds:
    """Project a transformed local box onto a conservative world-aligned box.

    The result is the tight AABB of the rotated box, which never under-reports
    the space an object occupies.
    """
    center = position + basis.transform(local.center)
    extents = local.size * 0.5
    half_x = (
        abs(basis.x_axis.x) * extents.x
        + abs(basis.y_axis.x) * extents.y
        + abs(basis.z_axis.x) * extents.z
    )
    half_y = (
        abs(basis.x_axis.y) * extents.x
        + abs(basis.y_axis.y) * extents.y
        + abs(basis.z_axis.y) * extents.z
    )
    half_z = (
        abs(basis.x_axis.z) * extents.x
        + abs(basis.y_axis.z) * extents.y
        + abs(basis.z_axis.z) * extents.z
    )
    return Bounds(
        Vec3(center.x - half_x, center.y - half_y, center.z - half_z),
        Vec3(center.x + half_x, center.y + half_y, center.z + half_z),
    )


def segment_intersects_aabb(
    start: Vec3, end: Vec3, bounds: Bounds, endpoint_epsilon: float = 1e-6
) -> bool:
    """Return whether the open segment passes through a box, using slab clipping."""
    direction = end - start
    minimum_t = 0.0
    maximum_t = 1.0
    for origin, delta, lower, upper in (
        (start.x, direction.x, bounds.min.x, bounds.max.x),
        (start.y, direction.y, bounds.min.y, bounds.max.y),
        (start.z, direction.z, bounds.min.z, bounds.max.z),
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
