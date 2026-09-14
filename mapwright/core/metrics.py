"""Grid-based spatial measurement: occupancy, clearance, regions, and paths.

This is the engine-independent substitute for a baked navigation mesh. It
rasterizes obstacle footprints onto a top-down grid padded by the player
radius, then answers the questions every downstream analyzer asks: what is
reachable, how wide is it, and how far apart are two places along walkable
ground.
"""

from __future__ import annotations

import heapq
import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Sequence

from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.scene_ir import SceneIR


MAXIMUM_GRID_CELLS = 2_000_000
_BLOCKED = -2
_UNVISITED = -1
_DIAGONAL = math.sqrt(2.0)


class GridError(ValueError):
    """Raised when an occupancy grid cannot be constructed as requested."""


@dataclass(frozen=True)
class Footprint:
    """The top-down axis-aligned area one object denies to the player."""

    object_id: str
    name: str
    center_x: float
    center_z: float
    half_width: float
    half_depth: float
    height: float

    def distance_to(self, x: float, z: float) -> float:
        """Return the horizontal distance from a point to this footprint."""
        dx = max(abs(x - self.center_x) - self.half_width, 0.0)
        dz = max(abs(z - self.center_z) - self.half_depth, 0.0)
        return math.hypot(dx, dz)


@dataclass(frozen=True)
class Region:
    """One connected component of free space."""

    component_id: int
    cell_count: int
    area: float
    bounds: Bounds
    significant: bool


@dataclass(frozen=True)
class GridPath:
    """A walkable route between two cells."""

    cells: tuple[tuple[int, int], ...]
    length: float
    minimum_clearance: float

    @property
    def reachable(self) -> bool:
        """Return whether the destination was reached at all."""
        return bool(self.cells)


@dataclass(frozen=True)
class OccupancyGrid:
    """A top-down walkability grid over the playable area."""

    origin_x: float
    origin_z: float
    cell_width: float
    cell_depth: float
    columns: int
    rows: int
    blocked: tuple[tuple[bool, ...], ...]
    agent_radius: float
    footprints: tuple[Footprint, ...]
    ground: Bounds

    @property
    def cell_area(self) -> float:
        """Return the ground area one cell covers."""
        return self.cell_width * self.cell_depth

    @property
    def free_cell_count(self) -> int:
        """Return how many cells a player could stand in."""
        return sum(not cell for row in self.blocked for cell in row)

    @property
    def blocked_cell_count(self) -> int:
        """Return how many cells are denied by obstacles."""
        return self.columns * self.rows - self.free_cell_count

    @property
    def bounds(self) -> Bounds:
        """Return the horizontal area the grid covers."""
        return Bounds(
            Vec3(self.origin_x, 0.0, self.origin_z),
            Vec3(
                self.origin_x + self.columns * self.cell_width,
                0.0,
                self.origin_z + self.rows * self.cell_depth,
            ),
        )

    def is_free(self, column: int, row: int) -> bool:
        """Return whether a cell exists and is walkable."""
        return (
            0 <= column < self.columns
            and 0 <= row < self.rows
            and not self.blocked[row][column]
        )

    def cell_center(self, column: int, row: int) -> Vec3:
        """Return the world position at the centre of a cell."""
        return Vec3(
            self.origin_x + (column + 0.5) * self.cell_width,
            0.0,
            self.origin_z + (row + 0.5) * self.cell_depth,
        )

    def cell_of(self, point: Vec3) -> tuple[int, int]:
        """Return the cell containing a world point, clamped to the grid."""
        column = int((point.x - self.origin_x) / self.cell_width)
        row = int((point.z - self.origin_z) / self.cell_depth)
        return (
            min(max(column, 0), self.columns - 1),
            min(max(row, 0), self.rows - 1),
        )

    def nearest_free_cell(self, point: Vec3) -> tuple[int, int] | None:
        """Return the closest walkable cell to a world point.

        Markers are often authored slightly inside geometry or off the ground,
        so snapping is the difference between a usable graph node and a
        spurious "unreachable" finding.
        """
        start = self.cell_of(point)
        if self.is_free(*start):
            return start
        seen = {start}
        pending = deque([start])
        while pending:
            column, row = pending.popleft()
            for next_column, next_row in (
                (column + 1, row),
                (column - 1, row),
                (column, row + 1),
                (column, row - 1),
            ):
                if (next_column, next_row) in seen:
                    continue
                if not (0 <= next_column < self.columns and 0 <= next_row < self.rows):
                    continue
                seen.add((next_column, next_row))
                if self.is_free(next_column, next_row):
                    return next_column, next_row
                pending.append((next_column, next_row))
        return None

    def neighbors(self, column: int, row: int) -> tuple[tuple[int, int, float], ...]:
        """Return walkable 8-connected neighbours and their step costs.

        Diagonal moves are refused when they would cut a corner between two
        blocked cells, so a path never squeezes through a gap a body cannot.
        """
        result: list[tuple[int, int, float]] = []
        for delta_column, delta_row in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ):
            next_column, next_row = column + delta_column, row + delta_row
            if not self.is_free(next_column, next_row):
                continue
            if delta_column and delta_row:
                if not self.is_free(column + delta_column, row) or not self.is_free(
                    column, row + delta_row
                ):
                    continue
                cost = math.hypot(self.cell_width, self.cell_depth)
            else:
                cost = abs(delta_column) * self.cell_width + abs(delta_row) * self.cell_depth
            result.append((next_column, next_row, cost))
        return tuple(result)

    # -- Derived analyses ----------------------------------------------------

    def connected_regions(
        self, minimum_region_area: float
    ) -> tuple[tuple[tuple[int, ...], ...], tuple[Region, ...]]:
        """Label every connected free-space component."""
        labels = [
            [_BLOCKED if self.blocked[row][column] else _UNVISITED for column in range(self.columns)]
            for row in range(self.rows)
        ]
        regions: list[Region] = []
        for start_row in range(self.rows):
            for start_column in range(self.columns):
                if labels[start_row][start_column] != _UNVISITED:
                    continue
                component_id = len(regions)
                labels[start_row][start_column] = component_id
                pending = deque([(start_column, start_row)])
                cells: list[tuple[int, int]] = []
                while pending:
                    column, row = pending.popleft()
                    cells.append((column, row))
                    for next_column, next_row, _ in self.neighbors(column, row):
                        if labels[next_row][next_column] != _UNVISITED:
                            continue
                        labels[next_row][next_column] = component_id
                        pending.append((next_column, next_row))
                min_column = min(column for column, _ in cells)
                max_column = max(column for column, _ in cells)
                min_row = min(row for _, row in cells)
                max_row = max(row for _, row in cells)
                area = len(cells) * self.cell_area
                regions.append(
                    Region(
                        component_id=component_id,
                        cell_count=len(cells),
                        area=area,
                        bounds=Bounds(
                            Vec3(
                                self.origin_x + min_column * self.cell_width,
                                0.0,
                                self.origin_z + min_row * self.cell_depth,
                            ),
                            Vec3(
                                self.origin_x + (max_column + 1) * self.cell_width,
                                0.0,
                                self.origin_z + (max_row + 1) * self.cell_depth,
                            ),
                        ),
                        significant=area >= minimum_region_area,
                    )
                )
        return tuple(tuple(row) for row in labels), tuple(regions)

    def clearance_field(self) -> tuple[tuple[float, ...], ...]:
        """Return each free cell's distance to the nearest obstacle or edge.

        This is the exact Euclidean distance transform, so a reported corridor
        width is a measurement rather than a grid-step approximation. Space
        outside the grid counts as blocked: the playable area ends there.
        """
        squared = _squared_distance_transform(
            self.blocked, self.columns, self.rows, self.cell_width, self.cell_depth
        )
        return tuple(
            tuple(math.sqrt(value) for value in row) for row in squared
        )

    def path(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        clearance: Sequence[Sequence[float]] | None = None,
    ) -> GridPath:
        """Return the shortest walkable route between two cells."""
        if not self.is_free(*start) or not self.is_free(*goal):
            return GridPath((), math.inf, 0.0)
        if start == goal:
            return GridPath(
                (start,), 0.0, clearance[start[1]][start[0]] if clearance else 0.0
            )
        distances = {start: 0.0}
        previous: dict[tuple[int, int], tuple[int, int]] = {}
        queue: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
        visited: set[tuple[int, int]] = set()
        while queue:
            distance, cell = heapq.heappop(queue)
            if cell in visited:
                continue
            visited.add(cell)
            if cell == goal:
                break
            for next_column, next_row, cost in self.neighbors(*cell):
                candidate = distance + cost
                key = (next_column, next_row)
                if candidate < distances.get(key, math.inf):
                    distances[key] = candidate
                    previous[key] = cell
                    heapq.heappush(queue, (candidate, key))
        if goal not in distances:
            return GridPath((), math.inf, 0.0)
        cells = [goal]
        while cells[-1] != start:
            cells.append(previous[cells[-1]])
        cells.reverse()
        minimum_clearance = (
            min(clearance[row][column] for column, row in cells) if clearance else 0.0
        )
        return GridPath(tuple(cells), distances[goal], minimum_clearance)

    def widest_path(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        clearance: Sequence[Sequence[float]],
    ) -> tuple[tuple[tuple[int, int], ...], float]:
        """Return the route between two cells with the most generous bottleneck.

        The shortest path hugs corners, so measuring width along it reports the
        corner rather than the corridor. Maximising the minimum clearance
        instead answers the question a designer asks: at its narrowest, how
        much room does the best way through actually leave?

        Equally wide routes are separated by length, not by cell index. Index
        order runs west-to-east, so on a mirrored map the two sides would break
        ties in opposite directions and a symmetric layout would report an
        asymmetry it does not have.
        """
        if not self.is_free(*start) or not self.is_free(*goal):
            return (), 0.0
        start_width = clearance[start[1]][start[0]]
        best: dict[tuple[int, int], tuple[float, float]] = {start: (start_width, 0.0)}
        previous: dict[tuple[int, int], tuple[int, int]] = {}
        queue: list[tuple[float, float, tuple[int, int]]] = [
            (-start_width, 0.0, start)
        ]
        visited: set[tuple[int, int]] = set()
        while queue:
            negated, travelled, cell = heapq.heappop(queue)
            if cell in visited:
                continue
            visited.add(cell)
            bottleneck = -negated
            if cell == goal:
                cells = [goal]
                while cells[-1] != start:
                    cells.append(previous[cells[-1]])
                cells.reverse()
                return tuple(cells), bottleneck
            for next_column, next_row, cost in self.neighbors(*cell):
                key = (next_column, next_row)
                candidate = (
                    min(bottleneck, clearance[next_row][next_column]),
                    travelled + cost,
                )
                known = best.get(key)
                if known is None or (-candidate[0], candidate[1]) < (
                    -known[0],
                    known[1],
                ):
                    best[key] = candidate
                    previous[key] = cell
                    heapq.heappush(queue, (-candidate[0], candidate[1], key))
        return (), 0.0

    def voronoi_labels(
        self, sources: Sequence[tuple[int, int]]
    ) -> tuple[tuple[tuple[int, ...], ...], tuple[tuple[float, ...], ...]]:
        """Partition free space by which source is closest along walkable ground.

        Two sources whose partitions touch are genuinely adjacent in the level,
        which is what makes the derived route graph reflect real movement
        instead of straight-line guesses.
        """
        owner = [[_UNVISITED for _ in range(self.columns)] for _ in range(self.rows)]
        distance = [[math.inf for _ in range(self.columns)] for _ in range(self.rows)]
        queue: list[tuple[float, int, int, int]] = []
        for index, (column, row) in enumerate(sources):
            if not self.is_free(column, row):
                continue
            if 0.0 < distance[row][column]:
                distance[row][column] = 0.0
                owner[row][column] = index
                heapq.heappush(queue, (0.0, index, column, row))
        while queue:
            current, index, column, row = heapq.heappop(queue)
            if current > distance[row][column]:
                continue
            for next_column, next_row, cost in self.neighbors(column, row):
                candidate = current + cost
                if candidate < distance[next_row][next_column]:
                    distance[next_row][next_column] = candidate
                    owner[next_row][next_column] = index
                    heapq.heappush(queue, (candidate, index, next_column, next_row))
        return (
            tuple(tuple(row) for row in owner),
            tuple(tuple(row) for row in distance),
        )


def build_footprints(scene: SceneIR) -> tuple[Footprint, ...]:
    """Return the top-down footprint of every measured obstructing object."""
    footprints: list[Footprint] = []
    for obj in scene.obstacles:
        box = obj.world_bounds()
        if box is None:
            continue
        footprints.append(
            Footprint(
                object_id=obj.id,
                name=obj.name,
                center_x=box.center.x,
                center_z=box.center.z,
                half_width=box.width * 0.5,
                half_depth=box.depth * 0.5,
                height=box.height,
            )
        )
    return tuple(footprints)


def build_occupancy_grid(
    scene: SceneIR,
    cell_size: float,
    agent_radius: float,
    area: Bounds | None = None,
) -> OccupancyGrid:
    """Rasterize a scene into a walkability grid inset by the player radius.

    The playable area is shrunk by one radius on every side because a body
    cannot stand with its centre on the very edge of the ground.
    """
    if not math.isfinite(cell_size) or cell_size <= 0:
        raise GridError("Grid cell size must be a positive finite number.")
    if not math.isfinite(agent_radius) or agent_radius < 0:
        raise GridError("Agent radius must be a non-negative finite number.")

    ground = area if area is not None else scene.ground_bounds()
    if ground is None:
        raise GridError(
            "Scene declares no ground and no measured objects, so there is no "
            "playable area to analyze."
        )
    minimum_x = ground.min.x + agent_radius
    maximum_x = ground.max.x - agent_radius
    minimum_z = ground.min.z + agent_radius
    maximum_z = ground.max.z - agent_radius
    if maximum_x <= minimum_x or maximum_z <= minimum_z:
        raise GridError(
            f"Playable area {ground.width:.2f} x {ground.depth:.2f} is smaller than "
            f"the player diameter {agent_radius * 2:.2f}."
        )

    columns = max(1, math.ceil((maximum_x - minimum_x) / cell_size))
    rows = max(1, math.ceil((maximum_z - minimum_z) / cell_size))
    if columns * rows > MAXIMUM_GRID_CELLS:
        raise GridError(
            f"Occupancy grid would contain {columns * rows:,} cells; increase the "
            "navigation cell size."
        )
    cell_width = (maximum_x - minimum_x) / columns
    cell_depth = (maximum_z - minimum_z) / rows
    footprints = build_footprints(scene)
    blocked = [[False] * columns for _ in range(rows)]

    for footprint in footprints:
        low_x = footprint.center_x - footprint.half_width - agent_radius
        high_x = footprint.center_x + footprint.half_width + agent_radius
        low_z = footprint.center_z - footprint.half_depth - agent_radius
        high_z = footprint.center_z + footprint.half_depth + agent_radius
        first_column = max(0, int((low_x - minimum_x) / cell_width - 1.0))
        last_column = min(columns - 1, int((high_x - minimum_x) / cell_width + 1.0))
        first_row = max(0, int((low_z - minimum_z) / cell_depth - 1.0))
        last_row = min(rows - 1, int((high_z - minimum_z) / cell_depth + 1.0))
        for row in range(first_row, last_row + 1):
            center_z = minimum_z + (row + 0.5) * cell_depth
            if abs(center_z - footprint.center_z) > footprint.half_depth + agent_radius:
                continue
            for column in range(first_column, last_column + 1):
                if blocked[row][column]:
                    continue
                center_x = minimum_x + (column + 0.5) * cell_width
                if abs(center_x - footprint.center_x) <= footprint.half_width + agent_radius:
                    blocked[row][column] = True

    return OccupancyGrid(
        origin_x=minimum_x,
        origin_z=minimum_z,
        cell_width=cell_width,
        cell_depth=cell_depth,
        columns=columns,
        rows=rows,
        blocked=tuple(tuple(row) for row in blocked),
        agent_radius=agent_radius,
        footprints=footprints,
        ground=ground,
    )


def clearance_at(
    x: float, z: float, footprints: Sequence[Footprint], area: Bounds
) -> float:
    """Return the true distance from a point to the nearest obstacle or edge.

    Measured against object geometry rather than the rasterized grid, so a
    reported width is a physical measurement with no cell-size bias and no
    player-radius inset folded into it.
    """
    best = min(x - area.min.x, area.max.x - x, z - area.min.z, area.max.z - z)
    for footprint in footprints:
        best = min(best, footprint.distance_to(x, z))
    return max(best, 0.0)


def measure_passage_width(
    grid: OccupancyGrid,
    cells: Sequence[tuple[int, int]],
    endpoint_margin: float = 0.0,
) -> float:
    """Return the narrowest physical width along a route, in metres.

    ``endpoint_margin`` metres are ignored at each end. A route's endpoints are
    sample points — a zone's centre, a marker someone dropped next to a rock —
    and their own surroundings say nothing about the passage between them. Left
    in, one node sitting in a crevice makes every route touching it look like a
    squeeze. Very short routes keep their full length rather than measuring
    nothing.
    """
    if not cells:
        return 0.0
    interior = _trim_endpoints(grid, cells, endpoint_margin)
    narrowest = min(
        clearance_at(
            grid.cell_center(column, row).x,
            grid.cell_center(column, row).z,
            grid.footprints,
            grid.ground,
        )
        for column, row in interior
    )
    # Clearance is exact, but it is sampled at cell centres, so the width is
    # only known to the grid's resolution. Reporting the raw figure would
    # claim precision the sampling does not have — and on a mirrored map it
    # shows up as a fraction of a percent of phantom asymmetry, because two
    # mirror-image routes break path ties differently. Floor rather than
    # round: never claim more room than was measured.
    resolution = min(grid.cell_width, grid.cell_depth)
    if resolution <= 0:
        return narrowest * 2.0
    return math.floor(narrowest * 2.0 / resolution) * resolution


def _trim_endpoints(
    grid: OccupancyGrid, cells: Sequence[tuple[int, int]], margin: float
) -> Sequence[tuple[int, int]]:
    """Drop the cells within ``margin`` metres of either end of a route.

    Selected by world distance rather than by index: two mirror-image routes
    must yield mirror-image interiors, and trimming a fixed number of cells
    does not survive a grid whose discretization differs by one step between
    the two sides.
    """
    if margin <= 0.0 or len(cells) < 3:
        return cells
    start = grid.cell_center(*cells[0])
    end = grid.cell_center(*cells[-1])
    interior = tuple(
        cell
        for cell in cells
        if min(
            grid.cell_center(*cell).distance_xz(start),
            grid.cell_center(*cell).distance_xz(end),
        )
        >= margin
    )
    return interior or (cells[len(cells) // 2],)


def render_occupancy_map(
    grid: OccupancyGrid,
    labels: Sequence[Sequence[int]],
    main_component_id: int | None,
    significant_ids: Iterable[int] = (),
    max_width: int = 64,
    max_height: int = 32,
) -> list[str]:
    """Render a compact ASCII map of reachable space, north at top."""
    step = max(
        1, math.ceil(grid.columns / max_width), math.ceil(grid.rows / max_height)
    )
    disconnected = {
        component_id: chr(ord("A") + index % 26)
        for index, component_id in enumerate(
            sorted(item for item in significant_ids if item != main_component_id)
        )
    }
    rendered: list[str] = []
    for row_start in reversed(range(0, grid.rows, step)):
        characters = []
        for column_start in range(0, grid.columns, step):
            values = [
                labels[row][column]
                for row in range(row_start, min(row_start + step, grid.rows))
                for column in range(column_start, min(column_start + step, grid.columns))
            ]
            free = [value for value in values if value >= 0]
            if not free:
                characters.append("#")
                continue
            component_id = max(set(free), key=free.count)
            if component_id == main_component_id:
                characters.append(".")
            elif component_id in disconnected:
                characters.append(disconnected[component_id])
            else:
                characters.append("o")
        rendered.append("".join(characters))
    width = max((len(row) for row in rendered), default=0)
    border = "+" + "-" * width + "+"
    return [border, *(f"|{row:<{width}}|" for row in rendered), border]


@dataclass(frozen=True)
class Distribution:
    """Summary statistics for a set of per-cell counts."""

    mean: float
    variance: float
    standard_deviation: float
    imbalance: float

    @classmethod
    def of(cls, counts: Sequence[int]) -> Distribution:
        """Summarize counts, scoring imbalance on a 0-100 scale.

        The score is the coefficient ``sd / (sd + mean)``: zero when every cell
        holds the same number, rising toward 100 as content concentrates.
        """
        if not counts:
            return cls(0.0, 0.0, 0.0, 100.0)
        mean = statistics.fmean(counts)
        variance = statistics.pvariance(counts)
        deviation = math.sqrt(variance)
        if sum(counts) == 0:
            imbalance = 100.0
        elif deviation == 0:
            imbalance = 0.0
        else:
            imbalance = 100.0 * deviation / (deviation + mean)
        return cls(mean, variance, deviation, imbalance)


def _squared_distance_transform(
    blocked: Sequence[Sequence[bool]],
    columns: int,
    rows: int,
    cell_width: float,
    cell_depth: float,
) -> list[list[float]]:
    """Exact squared Euclidean distance to the nearest blocked cell or edge.

    Two separable passes of Felzenszwalb's lower-envelope transform. Unreached
    cells carry a sentinel larger than any real distance in this grid rather
    than infinity, which keeps the parabola arithmetic finite.
    """
    span = (columns * cell_width) ** 2 + (rows * cell_depth) ** 2
    sentinel = 4.0 * span + 1.0
    grid = [
        [0.0 if blocked[row][column] else sentinel for column in range(columns)]
        for row in range(rows)
    ]
    for column in range(columns):
        transformed = _transform_1d(
            [grid[row][column] for row in range(rows)], cell_depth
        )
        for row in range(rows):
            grid[row][column] = transformed[row]
    for row in range(rows):
        grid[row] = _transform_1d(grid[row], cell_width)
    # Space beyond the grid is not walkable either, so clamp by edge distance.
    for row in range(rows):
        for column in range(columns):
            if blocked[row][column]:
                grid[row][column] = 0.0
                continue
            edge = min(
                (column + 0.5) * cell_width,
                (columns - column - 0.5) * cell_width,
                (row + 0.5) * cell_depth,
                (rows - row - 0.5) * cell_depth,
            )
            grid[row][column] = min(grid[row][column], edge * edge)
    return grid


def _transform_1d(values: Sequence[float], spacing: float) -> list[float]:
    """Felzenszwalb's exact 1D squared-distance transform along one axis."""
    count = len(values)
    if count == 0:
        return []
    envelope = [0] * count
    boundaries = [0.0] * (count + 1)
    boundaries[0] = -math.inf
    boundaries[1] = math.inf
    top = 0
    for position in range(1, count):
        while True:
            previous = envelope[top]
            intersection = (
                (values[position] + (position * spacing) ** 2)
                - (values[previous] + (previous * spacing) ** 2)
            ) / (2.0 * spacing * spacing * (position - previous))
            if intersection <= boundaries[top] and top > 0:
                top -= 1
                continue
            break
        top += 1
        envelope[top] = position
        boundaries[top] = intersection
        boundaries[top + 1] = math.inf
    result = [0.0] * count
    top = 0
    for position in range(count):
        while boundaries[top + 1] < position:
            top += 1
        source = envelope[top]
        result[position] = ((position - source) * spacing) ** 2 + values[source]
    return result
