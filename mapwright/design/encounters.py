"""Combat-space affordances: what a fight in this room can actually be.

A fight is interesting when it has more than one answer. This module measures
the answers a combat zone offers — how many ways in, whether any of them
flanks, whether there is anywhere to stand above or behind, and how much of
the floor is open ground with nothing to break a sightline.

Everything here is measurement. Thresholds and findings live in
:mod:`mapwright.validators.encounter`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.graph import NodeKind, RouteEdge, RouteGraph, RouteNode
from mapwright.core.metrics import OccupancyGrid
from mapwright.core.scene_ir import MarkerKind, SceneObject
from mapwright.core.zones import ZoneSummary


#: Upper bound on cells sampled per zone when measuring exposure.
EXPOSURE_SAMPLE_LIMIT = 4096

#: Cover spread is judged across the zone's four quadrants.
QUADRANT_COUNT = 4


@dataclass(frozen=True)
class EncounterSpace:
    """What one combat zone offers a player who has to fight in it."""

    zone: str
    entrances: int
    exits: int
    flank_routes: int
    high_ground: bool
    high_ground_delta: float
    cover_count: int
    cover_distribution: str
    retreat_routes: int
    engagement_distance: float
    exposure: float
    approach_spread: float
    spawn_candidates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "zone": self.zone,
            "entrances": self.entrances,
            "exits": self.exits,
            "flank_routes": self.flank_routes,
            "high_ground": self.high_ground,
            "high_ground_delta": round(self.high_ground_delta, 4),
            "cover_count": self.cover_count,
            "cover_distribution": self.cover_distribution,
            "retreat_routes": self.retreat_routes,
            "engagement_distance": round(self.engagement_distance, 4),
            "exposure": round(self.exposure, 4),
            "approach_spread": round(self.approach_spread, 2),
            "spawn_candidates": list(self.spawn_candidates),
        }


@dataclass(frozen=True)
class EncounterAnalysis:
    """Measured combat affordances for every combat zone in a scene."""

    zones: tuple[EncounterSpace, ...]

    @property
    def measured(self) -> bool:
        """Return whether the scene declares any combat space to analyse."""
        return bool(self.zones)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {"zones": [space.to_dict() for space in self.zones]}


def analyze_encounters(context: AnalysisContext) -> EncounterAnalysis:
    """Measure every combat zone's affordances, in zone declaration order."""
    graph = context.graph
    grid = context.grid
    clearance = context.clearance
    spaces = [
        _measure_zone(summary, graph, grid, clearance)
        for summary in context.zone_summaries
        if summary.zone.is_combat
    ]
    return EncounterAnalysis(zones=tuple(spaces))


def format_encounters(analysis: EncounterAnalysis) -> str:
    """Render measured combat affordances as a terminal block."""
    if not analysis.measured:
        return (
            "Encounter measurements\n"
            "No combat or objective zones are declared, so there is no "
            "encounter space to measure."
        )
    lines = ["Encounter measurements"]
    for space in analysis.zones:
        lines.extend(
            [
                f"  {space.zone}:",
                f"    Ways in: {space.entrances} "
                f"(onward exits {space.exits}, retreats {space.retreat_routes}, "
                f"flanks {space.flank_routes})",
                f"    Approach spread: {space.approach_spread:.0f} degrees | "
                f"Engagement distance: {space.engagement_distance:.1f} m",
                f"    Cover: {space.cover_count} object(s), "
                f"{space.cover_distribution} distribution | "
                f"High ground: "
                + (
                    f"yes, +{space.high_ground_delta:.2f} m"
                    if space.high_ground
                    else "none"
                ),
                f"    Exposure: {space.exposure * 100:.0f}% of the floor has open "
                "sightlines past half the engagement distance",
            ]
        )
        if space.spawn_candidates:
            lines.append(
                "    Approach points: " + ", ".join(space.spawn_candidates)
            )
    return "\n".join(lines)


def _measure_zone(
    summary: ZoneSummary,
    graph: RouteGraph | None,
    grid: OccupancyGrid | None,
    clearance: Sequence[Sequence[float]] | None,
) -> EncounterSpace:
    bounds = summary.bounds
    crossings = _boundary_crossings(graph, bounds)
    outside = _outside_endpoints(graph, bounds, crossings)
    engagement = summary.engagement_distance
    return EncounterSpace(
        zone=summary.name,
        entrances=len(crossings),
        exits=_onward_exits(graph, bounds, crossings),
        flank_routes=_flank_routes(graph, summary, bounds),
        high_ground=summary.has_high_ground,
        high_ground_delta=max(0.0, summary.highest_stand - summary.ground_height),
        cover_count=len(summary.cover_objects),
        cover_distribution=_cover_distribution(summary.cover_objects, bounds),
        retreat_routes=_retreat_routes(graph, bounds, crossings),
        engagement_distance=engagement,
        exposure=_exposure(grid, clearance, bounds, engagement),
        approach_spread=_approach_spread(graph, bounds, outside),
        spawn_candidates=_spawn_candidates(summary, outside),
    )


def _inside(bounds: Bounds | None, node: RouteNode) -> bool:
    return bounds is not None and bounds.contains_xz(node.position)


def _boundary_crossings(
    graph: RouteGraph | None, bounds: Bounds | None
) -> tuple[RouteEdge, ...]:
    """Return the routes with exactly one end inside the zone: the ways in."""
    if graph is None or bounds is None:
        return ()
    return tuple(
        edge
        for edge in graph.edges
        if _inside(bounds, graph.node(edge.source))
        != _inside(bounds, graph.node(edge.target))
    )


def _outside_endpoints(
    graph: RouteGraph | None, bounds: Bounds | None, crossings: Sequence[RouteEdge]
) -> tuple[RouteNode, ...]:
    if graph is None:
        return ()
    nodes = {
        (
            edge.target
            if _inside(bounds, graph.node(edge.source))
            else edge.source
        )
        for edge in crossings
    }
    return tuple(graph.node(identifier) for identifier in sorted(nodes))


def _reaches(
    graph: RouteGraph, bounds: Bounds | None, start: str, targets: set[str]
) -> bool:
    """Return whether a place reaches any target without re-entering the zone."""
    if not targets:
        return False
    adjacency = graph.adjacency()
    seen = {start}
    pending = deque([start])
    while pending:
        current = pending.popleft()
        if current in targets:
            return True
        for edge in adjacency[current]:
            neighbor = edge.other(current)
            if neighbor in seen or _inside(bounds, graph.node(neighbor)):
                continue
            seen.add(neighbor)
            pending.append(neighbor)
    return False


def _onward_exits(
    graph: RouteGraph | None, bounds: Bounds | None, crossings: Sequence[RouteEdge]
) -> int:
    """Return how many ways out lead onward rather than into a pocket.

    A way out counts when the far side reaches a declared exit or an objective
    outside the zone without coming back through it. When the scene declares
    neither, the test falls back to structure: the far side must continue
    somewhere rather than terminate.
    """
    if graph is None or bounds is None:
        return 0
    targets = {
        node.id
        for node in graph.nodes_of_kind(NodeKind.EXIT, NodeKind.OBJECTIVE)
        if not _inside(bounds, node)
    }
    count = 0
    for edge in crossings:
        outside = (
            edge.target if _inside(bounds, graph.node(edge.source)) else edge.source
        )
        if targets:
            if _reaches(graph, bounds, outside, targets):
                count += 1
        elif graph.degree(outside) > 1:
            count += 1
    return count


def _retreat_routes(
    graph: RouteGraph | None, bounds: Bounds | None, crossings: Sequence[RouteEdge]
) -> int:
    """Return how many ways out lead back toward an entry or spawn."""
    if graph is None or bounds is None:
        return 0
    targets = {
        node.id
        for node in graph.nodes_of_kind(NodeKind.ENTRY, NodeKind.SPAWN)
        if not _inside(bounds, node)
    }
    return sum(
        1
        for edge in crossings
        if _reaches(
            graph,
            bounds,
            edge.target if _inside(bounds, graph.node(edge.source)) else edge.source,
            targets,
        )
    )


def _zone_node(
    graph: RouteGraph, summary: ZoneSummary, bounds: Bounds | None
) -> RouteNode | None:
    """Return the graph node that stands for the zone itself."""
    named = next(
        (node for node in graph.nodes if node.id == f"zone:{summary.name}"), None
    )
    if named is not None:
        return named
    tagged = [node for node in graph.nodes if node.zone == summary.name]
    inside = tagged or [node for node in graph.nodes if _inside(bounds, node)]
    if not inside or bounds is None:
        return None
    center = bounds.center
    return min(inside, key=lambda node: (node.position.distance_xz(center), node.id))


def _flank_routes(
    graph: RouteGraph | None, summary: ZoneSummary, bounds: Bounds | None
) -> int:
    """Return how many independent approaches exist beyond the shortest one."""
    if graph is None:
        return 0
    node = _zone_node(graph, summary, bounds)
    if node is None:
        return 0
    starts = [
        candidate
        for candidate in graph.nodes_of_kind(NodeKind.ENTRY, NodeKind.SPAWN)
        if candidate.id != node.id
    ]
    if not starts:
        return 0
    reachable = [
        (distance, candidate.id)
        for candidate in starts
        if (distance := graph.shortest_path(candidate.id, node.id)[1]) < math.inf
    ]
    if not reachable:
        return 0
    nearest = min(reachable)[1]
    return max(0, graph.edge_disjoint_paths(nearest, node.id) - 1)


def _approach_spread(
    graph: RouteGraph | None, bounds: Bounds | None, outside: Sequence[RouteNode]
) -> float:
    """Return the compass spread, in degrees, of the directions attacks arrive from.

    A wide spread means the zone can be pressured from several sides; a narrow
    one means every approach shares a firing line however many doors there are.
    """
    if graph is None or bounds is None or len(outside) < 2:
        return 0.0
    center = bounds.center
    bearings = sorted(
        math.degrees(
            math.atan2(node.position.x - center.x, node.position.z - center.z)
        )
        % 360.0
        for node in outside
    )
    gaps = [
        second - first for first, second in zip(bearings, bearings[1:])
    ] + [bearings[0] + 360.0 - bearings[-1]]
    return 360.0 - max(gaps)


def _spawn_candidates(
    summary: ZoneSummary, outside: Sequence[RouteNode]
) -> tuple[str, ...]:
    """Return the places a fight can be fed from: declared spawns and approaches."""
    declared = [
        point.id for point in summary.markers if point.kind is MarkerKind.SPAWN
    ]
    return tuple(sorted({*declared, *(node.id for node in outside)}))


def _cover_distribution(
    cover: Sequence[SceneObject], bounds: Bounds | None
) -> str:
    """Return how evenly cover is spread across the zone's four quadrants.

    Counting cover is not enough: eight crates stacked in one corner leave
    three quarters of the fight with nothing to hide behind. ``good`` requires
    every quadrant to hold cover and no single quadrant to hold more than half
    of it, which needs no tuned number to decide.
    """
    if not cover:
        return "none"
    if bounds is None:
        return "poor"
    center = bounds.center
    counts = [0] * QUADRANT_COUNT
    for obj in cover:
        index = (2 if obj.position.x >= center.x else 0) + (
            1 if obj.position.z >= center.z else 0
        )
        counts[index] += 1
    occupied = sum(1 for count in counts if count)
    if occupied <= 1:
        return "poor"
    if occupied < QUADRANT_COUNT or max(counts) * 2 > len(cover):
        return "uneven"
    return "good"


def _exposure(
    grid: OccupancyGrid | None,
    clearance: Sequence[Sequence[float]] | None,
    bounds: Bounds | None,
    engagement_distance: float,
) -> float:
    """Return the share of walkable floor with nothing near enough to break a line.

    A cell counts as exposed when its clearance exceeds half the zone's
    engagement distance: at that point a player standing there can be shot
    from across the space with no obstacle in reach to duck behind.
    """
    if grid is None or clearance is None or bounds is None:
        return 0.0
    if engagement_distance <= 0.0:
        return 0.0
    first_column, first_row = grid.cell_of(Vec3(bounds.min.x, 0.0, bounds.min.z))
    last_column, last_row = grid.cell_of(Vec3(bounds.max.x, 0.0, bounds.max.z))
    columns = max(1, last_column - first_column + 1)
    rows = max(1, last_row - first_row + 1)
    stride = max(1, math.ceil(math.sqrt(columns * rows / EXPOSURE_SAMPLE_LIMIT)))
    threshold = engagement_distance * 0.5
    sampled = 0
    exposed = 0
    for row in range(first_row, last_row + 1, stride):
        for column in range(first_column, last_column + 1, stride):
            if not grid.is_free(column, row):
                continue
            if not bounds.contains_xz(grid.cell_center(column, row)):
                continue
            sampled += 1
            if clearance[row][column] > threshold:
                exposed += 1
    return exposed / sampled if sampled else 0.0
