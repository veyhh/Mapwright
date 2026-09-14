"""Route graph derivation and topology measurement."""

from __future__ import annotations

import math

import pytest

from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.graph import (
    GraphError,
    NodeKind,
    RouteEdge,
    RouteGraph,
    RouteNode,
    build_route_graph,
    journey_pairs,
)
from mapwright.core.metrics import build_occupancy_grid
from mapwright.core.scene_ir import (
    MarkerKind,
    MarkerPoint,
    ObjectType,
    SceneIR,
    SceneObject,
)


def node(identifier: str, kind: NodeKind = NodeKind.CORRIDOR) -> RouteNode:
    return RouteNode(
        id=identifier,
        kind=kind,
        position=Vec3(0.0, 0.0, 0.0),
        cell=(0, 0),
        label=identifier,
    )


def edge(source: str, target: str, distance: float = 1.0, width: float = 3.0) -> RouteEdge:
    return RouteEdge(
        source=source,
        target=target,
        distance=distance,
        width=width,
        travel_time=distance / 4.5,
        elevation_change=0.0,
        directness=1.0,
    )


def line_graph(count: int) -> RouteGraph:
    nodes = tuple(node(f"n{index}") for index in range(count))
    edges = tuple(edge(f"n{index}", f"n{index + 1}") for index in range(count - 1))
    return RouteGraph(nodes=nodes, edges=edges)


def ring_graph(count: int) -> RouteGraph:
    nodes = tuple(node(f"n{index}") for index in range(count))
    edges = tuple(
        edge(f"n{index}", f"n{(index + 1) % count}") for index in range(count)
    )
    return RouteGraph(nodes=nodes, edges=edges)


def test_graph_rejects_edges_to_unknown_nodes():
    with pytest.raises(GraphError, match="unknown node"):
        RouteGraph(nodes=(node("a"),), edges=(edge("a", "ghost"),))


def test_a_line_is_all_bridges_and_has_no_loops():
    graph = line_graph(4)
    assert graph.cycle_count == 0
    assert len(graph.bridges()) == 3
    assert graph.edge_disjoint_paths("n0", "n3") == 1


def test_a_ring_has_one_loop_and_no_bridges():
    graph = ring_graph(4)
    assert graph.cycle_count == 1
    assert graph.bridges() == ()
    assert graph.edge_disjoint_paths("n0", "n2") == 2


def test_parallel_routes_are_not_bridges():
    graph = RouteGraph(
        nodes=(node("a"), node("b")),
        edges=(edge("a", "b", 5.0), edge("a", "b", 7.0)),
    )
    assert graph.bridges() == ()
    assert graph.edge_disjoint_paths("a", "b") == 2


def test_disconnected_halves_are_separate_components():
    graph = RouteGraph(
        nodes=(node("a"), node("b"), node("c")),
        edges=(edge("a", "b"),),
    )
    assert len(graph.components()) == 2
    assert graph.shortest_path("a", "c") == ((), math.inf)


def test_shortest_path_follows_distance_not_hop_count():
    graph = RouteGraph(
        nodes=(node("a"), node("b"), node("c")),
        edges=(edge("a", "c", 50.0), edge("a", "b", 1.0), edge("b", "c", 1.0)),
    )
    path, distance = graph.shortest_path("a", "c")
    assert path == ("a", "b", "c")
    assert distance == pytest.approx(2.0)


def test_edge_loads_report_the_busiest_route_share():
    graph = RouteGraph(
        nodes=(node("start", NodeKind.ENTRY), node("mid"), node("goal", NodeKind.OBJECTIVE)),
        edges=(edge("start", "mid"), edge("mid", "goal")),
    )
    loads, routed = graph.edge_loads(journey_pairs(graph))
    assert routed == 1
    assert max(loads.values()) == pytest.approx(1.0)


# -- Derivation from a scene ------------------------------------------------


def scene_with_corridor(gap_count: int) -> SceneIR:
    """A 20x12 room split by a wall pierced by one or two doorways."""
    objects = [
        SceneObject(
            id="ground",
            name="Ground",
            type=ObjectType.GROUND,
            position=Vec3(0.0, 0.0, 0.0),
            bounds=Bounds(Vec3(-10, -0.1, -6), Vec3(10, 0, 6)),
        )
    ]
    if gap_count == 1:
        spans = [(-10.0, -1.0), (1.0, 10.0)]
    else:
        spans = [(-10.0, -6.0), (-4.0, 4.0), (6.0, 10.0)]
    for index, (start, end) in enumerate(spans):
        objects.append(
            SceneObject(
                id=f"wall_{index}",
                name=f"Wall{index}",
                type=ObjectType.STRUCTURE,
                position=Vec3((start + end) / 2, 0.0, 0.0),
                bounds=Bounds.from_size(Vec3(end - start, 3.0, 0.4)),
            )
        )
    return SceneIR(
        scene="corridor",
        objects=tuple(objects),
        entry_points=(MarkerPoint("entry", MarkerKind.ENTRY, Vec3(-8, 0, -4)),),
        objectives=(MarkerPoint("goal", MarkerKind.OBJECTIVE, Vec3(8, 0, 4)),),
    )


def derive(scene: SceneIR) -> RouteGraph:
    grid = build_occupancy_grid(scene, 0.25, 0.45)
    return build_route_graph(scene, grid, walk_speed=4.5, clearance=grid.clearance_field())


def test_one_doorway_leaves_a_single_independent_route():
    graph = derive(scene_with_corridor(gap_count=1))
    assert graph.edge_disjoint_paths("entry", "goal") == 1


def test_a_second_doorway_creates_a_genuine_alternative():
    graph = derive(scene_with_corridor(gap_count=2))
    assert graph.edge_disjoint_paths("entry", "goal") >= 2


def test_declared_markers_keep_their_kind_and_suppress_sampling():
    scene = scene_with_corridor(gap_count=2)
    graph = derive(scene)
    assert graph.node("entry").kind is NodeKind.ENTRY
    assert graph.node("goal").kind is NodeKind.OBJECTIVE


def test_an_unannotated_scene_is_marked_derived():
    scene = SceneIR(
        scene="bare",
        objects=(
            SceneObject(
                id="ground",
                name="Ground",
                type=ObjectType.GROUND,
                position=Vec3(0.0, 0.0, 0.0),
                bounds=Bounds(Vec3(-10, -0.1, -6), Vec3(10, 0, 6)),
            ),
        ),
    )
    graph = derive(scene)
    assert graph.derived
    assert all(item.derived for item in graph.nodes)


def test_derived_edges_carry_measured_width_and_travel_time():
    graph = derive(scene_with_corridor(gap_count=1))
    for item in graph.edges:
        assert item.width > 0.0
        assert item.travel_time == pytest.approx(item.distance / 4.5)
        assert 0.0 < item.directness <= 1.0


def test_journey_pairs_prefer_declared_starts_and_goals():
    graph = derive(scene_with_corridor(gap_count=1))
    assert journey_pairs(graph) == (("entry", "goal"),)
