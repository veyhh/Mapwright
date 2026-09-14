"""Design analysis: traversal flow, intensity pacing, and encounter space.

Every scene here is built by hand so each test states its own premise: a 20x12
room, a wall across the middle, and whatever doors, zones, and props the
behaviour under test needs.
"""

from __future__ import annotations

import json

import pytest

from mapwright.core.config import MapwrightConfig
from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.graph import journey_pairs
from mapwright.core.issues import Severity
from mapwright.core.scene_ir import (
    MarkerKind,
    MarkerPoint,
    ObjectType,
    PacingLevel,
    SceneIR,
    SceneObject,
    Zone,
    ZoneType,
)
from mapwright.design.encounters import analyze_encounters
from mapwright.design.flow import analyze_flow
from mapwright.design.pacing import analyze_pacing
from mapwright.validators.encounter import validate_encounter
from mapwright.validators.flow import validate_flow
from mapwright.validators.pacing import validate_pacing


def ground(width: float = 20.0, depth: float = 12.0) -> SceneObject:
    return SceneObject(
        id="ground",
        name="Ground",
        type=ObjectType.GROUND,
        position=Vec3(0.0, 0.0, 0.0),
        bounds=Bounds(
            Vec3(-width / 2, -0.1, -depth / 2), Vec3(width / 2, 0.0, depth / 2)
        ),
    )


def slab(
    identifier: str,
    x0: float,
    x1: float,
    z0: float,
    z1: float,
    height: float = 3.0,
    kind: ObjectType = ObjectType.STRUCTURE,
) -> SceneObject:
    """An axis-aligned block covering the given footprint."""
    return SceneObject(
        id=identifier,
        name=identifier,
        type=kind,
        position=Vec3((x0 + x1) / 2, 0.0, (z0 + z1) / 2),
        bounds=Bounds.from_size(Vec3(x1 - x0, height, z1 - z0)),
    )


def context(scene: SceneIR) -> AnalysisContext:
    return AnalysisContext(scene=scene, config=MapwrightConfig())


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def path_routes(graph, source: str, target: str) -> set[tuple[str, str]]:
    """Return the undirected routes the shortest journey between two places uses."""
    path, _ = graph.shortest_path(source, target)
    return {
        (first, second) if first <= second else (second, first)
        for first, second in zip(path, path[1:])
    }


def corridor_scene(doorways: int) -> SceneIR:
    """A 20x12 room split by a wall pierced by one or two doorways.

    Only an entry and an objective are declared, so the rest of the topology is
    inferred from sampled waypoints.
    """
    spans = (
        [(-10.0, -1.0), (1.0, 10.0)]
        if doorways == 1
        else [(-10.0, -6.0), (-4.0, 4.0), (6.0, 10.0)]
    )
    walls = [
        slab(f"wall_{index}", start, end, -0.2, 0.2)
        for index, (start, end) in enumerate(spans)
    ]
    return SceneIR(
        scene="corridor",
        objects=(ground(), *walls),
        entry_points=(MarkerPoint("entry", MarkerKind.ENTRY, Vec3(-8, 0, -4)),),
        objectives=(MarkerPoint("goal", MarkerKind.OBJECTIVE, Vec3(8, 0, 4)),),
    )


def gate(name: str, x: float, z: float) -> Zone:
    """A one-metre zone marking one mouth of a corridor through the wall."""
    return Zone(
        name,
        ZoneType.TRAVERSAL,
        PacingLevel.MEDIUM,
        Bounds(Vec3(x - 0.5, 0.0, z - 0.5), Vec3(x + 0.5, 3.0, z + 0.5)),
    )


def crossing_scene(doorways: int) -> SceneIR:
    """Two entries south and two objectives north of a three-metre-thick wall.

    Each corridor through the wall is named at both mouths, so the route every
    journey takes across the wall is a single declared edge.
    """
    if doorways == 1:
        spans = [(-10.0, -1.0), (1.0, 10.0)]
        gates = (gate("gate_south", 0.0, -2.5), gate("gate_north", 0.0, 2.5))
    else:
        spans = [(-10.0, -6.0), (-4.0, 4.0), (6.0, 10.0)]
        gates = (
            gate("west_gate_south", -5.0, -2.5),
            gate("west_gate_north", -5.0, 2.5),
            gate("east_gate_south", 5.0, -2.5),
            gate("east_gate_north", 5.0, 2.5),
        )
    walls = [
        slab(f"wall_{index}", start, end, -1.5, 1.5)
        for index, (start, end) in enumerate(spans)
    ]
    return SceneIR(
        scene="crossing",
        objects=(ground(), *walls),
        zones=gates,
        entry_points=(
            MarkerPoint("entry_west", MarkerKind.ENTRY, Vec3(-8, 0, -4)),
            MarkerPoint("entry_east", MarkerKind.ENTRY, Vec3(8, 0, -4)),
        ),
        objectives=(
            MarkerPoint("goal_west", MarkerKind.OBJECTIVE, Vec3(-8, 0, 4)),
            MarkerPoint("goal_east", MarkerKind.OBJECTIVE, Vec3(8, 0, 4)),
        ),
    )


def sealed_scene() -> SceneIR:
    """A scene whose playable area is smaller than the player, so no grid exists."""
    return SceneIR(
        scene="cupboard",
        objects=(ground(width=0.5, depth=0.5),),
    )


def citadel_scene(west_door: bool = False, cover: str = "spread") -> SceneIR:
    """A walled combat room in the north of a 20x12 level.

    The room is entered from the south field; ``west_door`` cuts a second way
    in from the west field, which reaches the room without using the first.
    """
    objects = [
        ground(),
        slab("south_wall_west", -5.0, -1.0, 0.8, 1.2),
        slab("south_wall_east", 1.0, 5.0, 0.8, 1.2),
        slab("east_wall", 4.8, 5.2, 1.0, 6.0),
        slab("west_wall", -5.2, -4.8, 1.0, 3.0 if west_door else 6.0),
    ]
    spots = (
        [(-2.0, 2.3), (2.0, 2.3), (-2.0, 4.8), (2.0, 4.8)]
        if cover == "spread"
        else [(-3.0, 2.0), (-2.0, 2.0), (-3.0, 3.0), (-2.0, 3.0)]
    )
    objects.extend(
        slab(
            f"crate_{index}",
            x - 0.5,
            x + 0.5,
            z - 0.5,
            z + 0.5,
            height=1.0,
            kind=ObjectType.COVER,
        )
        for index, (x, z) in enumerate(spots)
    )
    return SceneIR(
        scene="citadel",
        objects=tuple(objects),
        zones=(
            Zone(
                "arena",
                ZoneType.COMBAT,
                PacingLevel.HIGH,
                Bounds(Vec3(-4.7, 0.0, 1.3), Vec3(4.7, 3.0, 6.0)),
            ),
            Zone(
                "south_field",
                ZoneType.EXPLORATION,
                PacingLevel.CALM,
                Bounds(Vec3(-10, 0.0, -6.0), Vec3(10, 3.0, 0.7)),
            ),
            Zone(
                "west_field",
                ZoneType.EXPLORATION,
                PacingLevel.LOW,
                Bounds(Vec3(-10, 0.0, 3.4), Vec3(-5.3, 3.0, 6.0)),
            ),
        ),
        entry_points=(MarkerPoint("entry", MarkerKind.ENTRY, Vec3(-9, 0, -5)),),
        objectives=(MarkerPoint("goal", MarkerKind.OBJECTIVE, Vec3(0, 0, 4)),),
        exits=(MarkerPoint("exit", MarkerKind.EXIT, Vec3(-8, 0, 5.5)),),
    )


def paced_scene(*beats: tuple[str, ZoneType, PacingLevel]) -> SceneIR:
    """A featureless room whose zones declare the intended intensity sequence."""
    return SceneIR(
        scene="paced",
        objects=(ground(),),
        zones=tuple(Zone(name, kind, level) for name, kind, level in beats),
    )


# -- Flow: route structure ---------------------------------------------------


def test_one_doorway_leaves_a_single_route_and_one_structural_chokepoint():
    analysis = analyze_flow(context(corridor_scene(doorways=1)))
    assert analysis.route_diversity == 1
    assert len(analysis.structural_chokepoints) == 1


def test_a_second_doorway_adds_a_route_and_removes_the_structural_chokepoint():
    analysis = analyze_flow(context(corridor_scene(doorways=2)))
    assert analysis.route_diversity >= 2
    assert analysis.structural_chokepoints == ()


def test_a_cut_that_isolates_one_place_is_not_called_structural():
    analysed = context(corridor_scene(doorways=1))
    analysis = analyze_flow(analysed)
    graph = analysed.graph
    attachment = next(edge for edge in graph.edges if "entry" in edge.key)
    assert graph.degree("entry") == 1
    assert attachment.key in {edge.key for edge in graph.bridges()}
    assert attachment.key not in {point.key for point in analysis.chokepoints if point.structural}
    assert all(min(point.split) >= 2 for point in analysis.structural_chokepoints)


def test_a_single_corridor_carries_every_journey():
    analysed = context(crossing_scene(doorways=1))
    analysis = analyze_flow(analysed)
    busiest = analysis.busiest_edge
    mouths = {
        node.id
        for node in analysed.graph.nodes
        if node.zone in ("gate_south", "gate_north")
    }
    assert set(busiest.key) == mouths
    assert dict(analysis.edge_loads)[busiest.key] == pytest.approx(1.0)
    assert analysis.traversal_concentration == pytest.approx(100.0)


def test_the_busiest_route_is_the_one_every_journey_walks():
    analysed = context(crossing_scene(doorways=1))
    analysis = analyze_flow(analysed)
    graph = analysed.graph
    assert analysis.routed_journeys == len(journey_pairs(graph)) == 4
    for source, target in journey_pairs(graph):
        assert analysis.busiest_edge.key in path_routes(graph, source, target)


def test_a_second_corridor_spreads_the_traversal_out():
    funnelled = analyze_flow(context(crossing_scene(doorways=1)))
    spread = analyze_flow(context(crossing_scene(doorways=2)))
    assert spread.routed_journeys == funnelled.routed_journeys
    assert spread.traversal_concentration < funnelled.traversal_concentration
    assert dict(spread.edge_loads)[spread.busiest_edge.key] < 1.0


def test_places_are_classified_by_the_routes_that_reach_them():
    analysed = context(corridor_scene(doorways=1))
    analysis = analyze_flow(analysed)
    graph = analysed.graph
    assert analysis.dead_ends
    assert all(graph.degree(node.id) == 1 for node in analysis.dead_ends)
    assert all(graph.degree(node.id) == 3 for node in analysis.junctions)
    assert all(graph.degree(node.id) >= 4 for node in analysis.hubs)
    # A declared entry keeps its own kind even though only one route reaches it.
    assert "entry" not in {node.id for node in analysis.dead_ends}


def test_loops_are_counted_from_the_graph_not_the_geometry():
    single = context(corridor_scene(doorways=1))
    doubled = context(corridor_scene(doorways=2))
    assert analyze_flow(single).loops == single.graph.cycle_count == 0
    assert analyze_flow(doubled).loops == doubled.graph.cycle_count >= 1


def test_a_scene_without_markers_is_judged_on_an_inferred_topology():
    analysed = context(SceneIR(scene="bare", objects=(ground(),)))
    analysis = analyze_flow(analysed)
    issues = validate_flow(analysed).issues
    assert analysis.derived
    assert issues
    assert all(issue.advisory for issue in issues)


def test_a_scene_with_no_walkable_grid_degrades_to_one_finding():
    analysed = context(sealed_scene())
    report = validate_flow(analysed)
    assert report.analysis is None
    assert len(report.issues) == 1
    finding = report.issues[0]
    assert finding.code == "no_critical_path"
    assert finding.severity is Severity.INFO
    assert finding.advisory
    assert "cupboard" in finding.evidence


# -- Pacing: the intensity sequence -----------------------------------------


def test_a_curve_that_rises_and_falls_by_one_step_raises_nothing():
    scene = paced_scene(
        ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
        ("cellars", ZoneType.EXPLORATION, PacingLevel.LOW),
        ("gallery", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
        ("throne", ZoneType.COMBAT, PacingLevel.HIGH),
        ("stair", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
        ("courtyard", ZoneType.RELIEF, PacingLevel.LOW),
    )
    assert validate_pacing(context(scene)).issues == ()


def test_four_high_intensity_zones_in_a_row_are_reported():
    scene = paced_scene(
        ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
        ("cellars", ZoneType.EXPLORATION, PacingLevel.LOW),
        ("gallery", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
        ("gatehouse", ZoneType.COMBAT, PacingLevel.HIGH),
        ("bailey", ZoneType.COMBAT, PacingLevel.HIGH),
        ("keep", ZoneType.COMBAT, PacingLevel.INTENSE),
        ("throne", ZoneType.COMBAT, PacingLevel.HIGH),
        ("stair", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
        ("courtyard", ZoneType.RELIEF, PacingLevel.LOW),
    )
    issues = validate_pacing(context(scene)).issues
    assert codes(issues) == {"consecutive_high_intensity"}
    assert issues[0].metric("run_length") == 4.0
    assert "gatehouse" in issues[0].evidence


def test_a_peak_with_nothing_after_it_is_missing_relief():
    scene = paced_scene(
        ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
        ("approach", ZoneType.TENSION, PacingLevel.LOW),
        ("gallery", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
        ("climax", ZoneType.COMBAT, PacingLevel.HIGH),
    )
    issues = validate_pacing(context(scene)).issues
    assert codes(issues) == {"missing_relief"}
    assert issues[0].zone == "climax"


def test_a_calm_to_intense_jump_is_abrupt():
    scene = paced_scene(
        ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
        ("ambush", ZoneType.COMBAT, PacingLevel.INTENSE),
        ("retreat", ZoneType.COMBAT, PacingLevel.HIGH),
        ("gallery", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
        ("courtyard", ZoneType.RELIEF, PacingLevel.LOW),
    )
    issues = validate_pacing(context(scene)).issues
    assert codes(issues) == {"abrupt_transition"}
    assert issues[0].metric("delta") == 4.0
    assert issues[0].zone == "ambush"


def test_a_two_step_change_is_already_abrupt_at_the_default_limit():
    """A calm zone opening straight onto a medium one is judged abrupt.

    The default profile treats a change of two intensity steps as abrupt, so a
    calm -> medium -> intense -> calm shape is three findings, not a clean bill.
    """
    analysed = context(
        paced_scene(
            ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
            ("gallery", ZoneType.TRAVERSAL, PacingLevel.MEDIUM),
            ("cellars", ZoneType.EXPLORATION, PacingLevel.LOW),
            ("courtyard", ZoneType.RELIEF, PacingLevel.CALM),
        )
    )
    issues = validate_pacing(analysed).issues
    assert analysed.config.thresholds.abrupt_transition_delta == 2.0
    assert codes(issues) == {"abrupt_transition"}
    assert issues[0].metric("delta") == 2.0


def test_one_intensity_throughout_reads_as_a_flat_curve():
    scene = paced_scene(
        ("hall", ZoneType.EXPLORATION, PacingLevel.MEDIUM),
        ("cellars", ZoneType.EXPLORATION, PacingLevel.MEDIUM),
        ("gallery", ZoneType.EXPLORATION, PacingLevel.MEDIUM),
        ("courtyard", ZoneType.EXPLORATION, PacingLevel.MEDIUM),
    )
    issues = validate_pacing(context(scene)).issues
    assert codes(issues) == {"flat_pacing"}
    assert issues[0].metric("variety") == 0.0


def test_a_scene_without_zones_states_no_pacing_intent():
    analysed = context(SceneIR(scene="bare", objects=(ground(),)))
    report = validate_pacing(analysed)
    assert not report.analysis.declared
    assert len(report.issues) == 1
    assert report.issues[0].code == "no_zone_intent"
    assert report.issues[0].severity is Severity.INFO
    assert report.issues[0].advisory


# -- Encounter: what a fight in the room can be ------------------------------


def test_a_combat_room_with_one_door_cannot_be_entered_twice_or_flanked():
    analysed = context(citadel_scene(west_door=False))
    space = analyze_encounters(analysed).zones[0]
    issues = validate_encounter(analysed).issues
    assert space.zone == "arena"
    assert space.entrances == 1
    assert space.flank_routes == 0
    assert {"single_entrance_encounter", "no_flank_route"} <= codes(issues)
    assert not any(issue.advisory for issue in issues)


def test_a_second_independent_approach_clears_the_flank_finding():
    analysed = context(citadel_scene(west_door=True))
    space = analyze_encounters(analysed).zones[0]
    assert space.entrances >= 2
    assert space.flank_routes >= 1
    assert "no_flank_route" not in codes(validate_encounter(analysed).issues)


def test_cover_spread_across_quadrants_beats_the_same_count_in_one_corner():
    spread = context(citadel_scene(cover="spread"))
    piled = context(citadel_scene(cover="corner"))
    spread_space = analyze_encounters(spread).zones[0]
    piled_space = analyze_encounters(piled).zones[0]
    assert spread_space.cover_count == piled_space.cover_count == 4
    assert spread_space.cover_distribution == "good"
    assert piled_space.cover_distribution == "poor"
    assert "poor_cover_distribution" not in codes(validate_encounter(spread).issues)
    assert "poor_cover_distribution" in codes(validate_encounter(piled).issues)


# -- Every analyser reports the same thing twice, in JSON --------------------


def test_flow_findings_are_deterministic_and_serializable():
    scene = corridor_scene(doorways=1)
    first = validate_flow(context(scene)).to_dict()
    second = validate_flow(context(scene)).to_dict()
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_pacing_findings_are_deterministic_and_serializable():
    scene = paced_scene(
        ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
        ("ambush", ZoneType.COMBAT, PacingLevel.INTENSE),
        ("courtyard", ZoneType.RELIEF, PacingLevel.CALM),
    )
    first = validate_pacing(context(scene)).to_dict()
    second = validate_pacing(context(scene)).to_dict()
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_encounter_findings_are_deterministic_and_serializable():
    scene = citadel_scene(cover="corner")
    first = validate_encounter(context(scene)).to_dict()
    second = validate_encounter(context(scene)).to_dict()
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_pacing_measurements_survive_a_round_trip_through_json():
    scene = paced_scene(
        ("entry_hall", ZoneType.ENTRY, PacingLevel.CALM),
        ("keep", ZoneType.COMBAT, PacingLevel.HIGH),
        ("courtyard", ZoneType.RELIEF, PacingLevel.LOW),
    )
    analysis = analyze_pacing(context(scene))
    assert analysis.intensity_curve == (0, 3, 1)
    assert json.loads(json.dumps(analysis.to_dict()))["peak"] == 3
