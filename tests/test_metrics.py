"""Occupancy grid, clearance, region, and path measurement."""

from __future__ import annotations

import math
import random

import pytest

from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.metrics import (
    Distribution,
    GridError,
    _squared_distance_transform,
    build_occupancy_grid,
    clearance_at,
    measure_passage_width,
)
from mapwright.core.scene_ir import ObjectType, SceneIR, SceneObject


def ground(width: float = 20.0, depth: float = 12.0) -> SceneObject:
    return SceneObject(
        id="ground",
        name="Ground",
        type=ObjectType.GROUND,
        position=Vec3(0.0, 0.0, 0.0),
        bounds=Bounds(Vec3(-width / 2, -0.1, -depth / 2), Vec3(width / 2, 0.0, depth / 2)),
    )


def block(identifier: str, x: float, z: float, width: float, depth: float) -> SceneObject:
    return SceneObject(
        id=identifier,
        name=identifier,
        type=ObjectType.STRUCTURE,
        position=Vec3(x, 0.0, z),
        bounds=Bounds.from_size(Vec3(width, 3.0, depth)),
    )


def split_scene(gap: float = 2.0) -> SceneIR:
    """A 20x12 room divided by a wall with a gap of the requested width."""
    side = (20.0 - gap) / 2
    offset = gap / 2 + side / 2
    return SceneIR(
        scene="split",
        objects=(
            ground(),
            block("wall_west", -offset, 0.0, side, 0.4),
            block("wall_east", offset, 0.0, side, 0.4),
        ),
    )


def test_grid_insets_by_the_player_radius():
    grid = build_occupancy_grid(SceneIR(scene="s", objects=(ground(),)), 0.5, 0.45)
    assert grid.origin_x == pytest.approx(-10.0 + 0.45)
    assert grid.bounds.width == pytest.approx(20.0 - 0.9)


def test_grid_refuses_ground_smaller_than_the_player():
    scene = SceneIR(scene="s", objects=(ground(width=0.5, depth=0.5),))
    with pytest.raises(GridError, match="smaller than the player diameter"):
        build_occupancy_grid(scene, 0.25, 0.45)


def test_a_wall_with_a_doorway_stays_one_region():
    grid = build_occupancy_grid(split_scene(gap=2.0), 0.25, 0.45)
    _, regions = grid.connected_regions(minimum_region_area=1.0)
    assert sum(region.significant for region in regions) == 1


def test_a_wall_without_a_doorway_splits_the_space():
    scene = SceneIR(
        scene="sealed",
        objects=(ground(), block("wall", 0.0, 0.0, 20.0, 0.4)),
    )
    grid = build_occupancy_grid(scene, 0.25, 0.45)
    _, regions = grid.connected_regions(minimum_region_area=1.0)
    assert sum(region.significant for region in regions) == 2


def test_a_gap_narrower_than_the_player_is_not_passable():
    grid = build_occupancy_grid(split_scene(gap=0.6), 0.25, 0.45)
    _, regions = grid.connected_regions(minimum_region_area=1.0)
    assert sum(region.significant for region in regions) == 2


def test_measured_passage_width_matches_the_modelled_doorway():
    scene = split_scene(gap=2.0)
    grid = build_occupancy_grid(scene, 0.25, 0.45)
    field = grid.clearance_field()
    start = grid.nearest_free_cell(Vec3(-8.0, 0.0, -4.0))
    goal = grid.nearest_free_cell(Vec3(8.0, 0.0, 4.0))
    cells, _ = grid.widest_path(start, goal, field)
    assert measure_passage_width(grid, cells) == pytest.approx(2.0, abs=0.05)


def test_clearance_is_measured_from_geometry_not_grid_cells():
    scene = split_scene(gap=3.0)
    grid = build_occupancy_grid(scene, 0.25, 0.45)
    assert clearance_at(0.0, 0.0, grid.footprints, grid.ground) == pytest.approx(1.5)


def test_path_detours_through_the_doorway_instead_of_the_straight_line():
    scene = split_scene(gap=2.0)
    grid = build_occupancy_grid(scene, 0.25, 0.45)
    start = grid.nearest_free_cell(Vec3(-8.0, 0.0, -4.0))
    goal = grid.nearest_free_cell(Vec3(8.0, 0.0, 4.0))
    path = grid.path(start, goal)
    assert path.reachable
    straight = Vec3(-8.0, 0.0, -4.0).distance_xz(Vec3(8.0, 0.0, 4.0))
    assert path.length > straight + 1.0
    assert any(abs(grid.cell_center(*cell).x) < 1.0 for cell in path.cells)


def test_unreachable_cells_report_no_path():
    scene = SceneIR(
        scene="sealed", objects=(ground(), block("wall", 0.0, 0.0, 20.0, 0.4))
    )
    grid = build_occupancy_grid(scene, 0.25, 0.45)
    start = grid.nearest_free_cell(Vec3(-8.0, 0.0, -4.0))
    goal = grid.nearest_free_cell(Vec3(8.0, 0.0, 4.0))
    assert not grid.path(start, goal).reachable


def test_distance_transform_matches_brute_force():
    random.seed(11)
    for _ in range(4):
        columns, rows = random.randint(4, 12), random.randint(4, 12)
        cell_width, cell_depth = 0.4, 0.25
        blocked = [
            [random.random() < 0.2 for _ in range(columns)] for _ in range(rows)
        ]
        blocked[0][0] = True
        result = _squared_distance_transform(
            blocked, columns, rows, cell_width, cell_depth
        )
        for row in range(rows):
            for column in range(columns):
                if blocked[row][column]:
                    assert result[row][column] == 0.0
                    continue
                nearest = min(
                    ((column - j) * cell_width) ** 2 + ((row - i) * cell_depth) ** 2
                    for i in range(rows)
                    for j in range(columns)
                    if blocked[i][j]
                )
                edge = min(
                    (column + 0.5) * cell_width,
                    (columns - column - 0.5) * cell_width,
                    (row + 0.5) * cell_depth,
                    (rows - row - 0.5) * cell_depth,
                )
                assert result[row][column] == pytest.approx(min(nearest, edge * edge))


def test_voronoi_partition_assigns_every_free_cell_to_a_source():
    grid = build_occupancy_grid(split_scene(gap=2.0), 0.5, 0.45)
    sources = [
        grid.nearest_free_cell(Vec3(-8.0, 0.0, 0.0)),
        grid.nearest_free_cell(Vec3(8.0, 0.0, 0.0)),
    ]
    owner, distance = grid.voronoi_labels(sources)
    for row in range(grid.rows):
        for column in range(grid.columns):
            if grid.blocked[row][column]:
                continue
            assert owner[row][column] in (0, 1)
            assert math.isfinite(distance[row][column])


def test_distribution_scores_even_and_concentrated_layouts():
    assert Distribution.of([3, 3, 3, 3]).imbalance == pytest.approx(0.0)
    assert Distribution.of([0, 0, 0, 12]).imbalance > 60.0
    assert Distribution.of([0, 0, 0, 12]).imbalance > Distribution.of([1, 2, 4, 5]).imbalance
    assert Distribution.of([]).imbalance == 100.0
    assert Distribution.of([0, 0, 0]).imbalance == 100.0
