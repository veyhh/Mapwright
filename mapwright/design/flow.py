"""Traversal structure: the routes a level offers and how traffic uses them.

Flow is a level's skeleton. This module answers four questions about it — where
the intended journey runs, how many genuinely independent ways through exist,
which routes cannot be lost without splitting the space, and how evenly the
expected journeys spread across the routes that exist.

Everything here is measurement. No number is compared against a threshold and
no finding is raised; that is :mod:`mapwright.validators.flow`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.graph import (
    NodeKind,
    RouteEdge,
    RouteGraph,
    RouteNode,
    journey_pairs,
)


@dataclass(frozen=True)
class Chokepoint:
    """One route that constrains movement, by splitting the level or by width."""

    edge: RouteEdge
    structural: bool
    narrow: bool
    load: float
    split: tuple[int, int]

    @property
    def key(self) -> tuple[str, str]:
        """Return the undirected identity of the constrained route."""
        return self.edge.key

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "route": list(self.edge.key),
            "structural": self.structural,
            "narrow": self.narrow,
            "width": round(self.edge.width, 4),
            "load": round(self.load, 4),
            "split": list(self.split),
        }


@dataclass(frozen=True)
class FlowAnalysis:
    """Measured traversal structure for one scene."""

    critical_path: tuple[str, ...]
    critical_path_length: float
    critical_path_time: float
    critical_path_basis: str
    optional_routes: int
    route_diversity: int
    loops: int
    dead_ends: tuple[RouteNode, ...]
    hubs: tuple[RouteNode, ...]
    junctions: tuple[RouteNode, ...]
    isolated: tuple[RouteNode, ...]
    chokepoints: tuple[Chokepoint, ...]
    traversal_concentration: float
    busiest_edge: RouteEdge | None
    edge_loads: tuple[tuple[tuple[str, str], float], ...]
    backtracking_ratio: float
    derived: bool
    routed_journeys: int

    @property
    def structural_chokepoints(self) -> tuple[Chokepoint, ...]:
        """Return only the chokepoints whose loss would split the level."""
        return tuple(point for point in self.chokepoints if point.structural)

    @property
    def dead_end_ratio(self) -> float:
        """Return dead ends as a percentage of the places the level offers."""
        places = len(self.dead_ends) + len(self.hubs) + len(self.junctions)
        return 0.0 if places == 0 else 100.0 * len(self.dead_ends) / places

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "critical_path": list(self.critical_path),
            "critical_path_basis": self.critical_path_basis,
            "critical_path_length": round(self.critical_path_length, 4),
            "critical_path_time": round(self.critical_path_time, 4),
            "optional_routes": self.optional_routes,
            "route_diversity": self.route_diversity,
            "loops": self.loops,
            "dead_ends": [_node_dict(node) for node in self.dead_ends],
            "hubs": [_node_dict(node) for node in self.hubs],
            "junctions": [_node_dict(node) for node in self.junctions],
            "isolated": [_node_dict(node) for node in self.isolated],
            "chokepoints": [point.to_dict() for point in self.chokepoints],
            "traversal_concentration": round(self.traversal_concentration, 2),
            "busiest_route": (
                None if self.busiest_edge is None else list(self.busiest_edge.key)
            ),
            "edge_loads": [
                {"route": list(key), "load": round(load, 4)}
                for key, load in self.edge_loads
            ],
            "backtracking_ratio": round(self.backtracking_ratio, 4),
            "routed_journeys": self.routed_journeys,
            "derived": self.derived,
        }


def analyze_flow(context: AnalysisContext) -> FlowAnalysis | None:
    """Measure one scene's traversal structure, or ``None`` when it has no graph."""
    graph = context.graph
    if graph is None:
        return None

    pairs = journey_pairs(graph)
    loads, routed = graph.edge_loads(pairs)
    ordered_loads = tuple(sorted(loads.items()))
    busiest_key = (
        min(loads.items(), key=lambda item: (-item[1], item[0]))[0] if loads else None
    )
    edges_by_key = {edge.key: edge for edge in graph.edges}
    concentration = 100.0 * loads[busiest_key] if busiest_key is not None else 0.0

    path, basis = _critical_path(graph)
    length, travel_time = _path_cost(edges_by_key, path)
    optional = 0
    if len(path) >= 2:
        optional = max(0, graph.edge_disjoint_paths(path[0], path[-1]) - 1)

    return FlowAnalysis(
        critical_path=path,
        critical_path_length=length,
        critical_path_time=travel_time,
        critical_path_basis=basis,
        optional_routes=optional,
        route_diversity=_route_diversity(graph, pairs),
        loops=graph.cycle_count,
        dead_ends=graph.nodes_of_kind(NodeKind.DEAD_END),
        hubs=graph.nodes_of_kind(NodeKind.HUB),
        junctions=graph.nodes_of_kind(NodeKind.JUNCTION),
        isolated=_isolated_nodes(graph),
        chokepoints=_chokepoints(
            graph, loads, context.config.thresholds.chokepoint_width
        ),
        traversal_concentration=concentration,
        busiest_edge=None if busiest_key is None else edges_by_key[busiest_key],
        edge_loads=ordered_loads,
        backtracking_ratio=_backtracking_ratio(graph, pairs, routed),
        derived=graph.derived,
        routed_journeys=routed,
    )


def format_flow(analysis: FlowAnalysis) -> str:
    """Render measured traversal structure as a terminal block."""
    lines = [
        "Flow measurements"
        + (" (topology inferred from sampled waypoints)" if analysis.derived else ""),
        f"Critical path ({analysis.critical_path_basis}): "
        + (
            " -> ".join(analysis.critical_path)
            if analysis.critical_path
            else "none reachable"
        ),
        f"Critical path cost: {analysis.critical_path_length:.2f} m, "
        f"{analysis.critical_path_time:.1f} s at walking pace",
        f"Route diversity: {analysis.route_diversity} independent route(s) across "
        f"{analysis.routed_journeys} routed journey(s)",
        f"Optional routes beyond the critical path: {analysis.optional_routes} | "
        f"Loops: {analysis.loops}",
        f"Places: {len(analysis.hubs)} hub(s), {len(analysis.junctions)} junction(s), "
        f"{len(analysis.dead_ends)} dead end(s), {len(analysis.isolated)} isolated",
        f"Traversal concentration: {analysis.traversal_concentration:.0f}%"
        + (
            ""
            if analysis.busiest_edge is None
            else f" on {_edge_text(analysis.busiest_edge)}"
        ),
        f"Backtracking: {analysis.backtracking_ratio * 100:.0f}% of routed journeys "
        "re-tread a route an earlier journey already used",
    ]
    if analysis.dead_ends:
        lines.append(
            "Dead ends: " + ", ".join(node.label for node in analysis.dead_ends)
        )
    if analysis.isolated:
        lines.append(
            "Isolated places: " + ", ".join(node.label for node in analysis.isolated)
        )
    if analysis.chokepoints:
        lines.append("Chokepoints:")
        for point in analysis.chokepoints:
            marks = []
            if point.structural:
                marks.append(f"structural, splits {point.split[0]}/{point.split[1]}")
            if point.narrow:
                marks.append(f"narrow at {point.edge.width:.2f} m")
            lines.append(
                f"  - {_edge_text(point.edge)}: {'; '.join(marks)}; "
                f"carries {point.load * 100:.0f}% of routed journeys"
            )
    else:
        lines.append("Chokepoints: none")
    return "\n".join(lines)


def describe_route(graph: RouteGraph, edge: RouteEdge) -> str:
    """Return a route as the two place names a designer would recognize."""
    return f"{graph.node(edge.source).label} -> {graph.node(edge.target).label}"


def _edge_text(edge: RouteEdge) -> str:
    return f"{edge.source} -> {edge.target}"


def _node_dict(node: RouteNode) -> dict[str, Any]:
    return {"id": node.id, "label": node.label, "kind": node.kind.value}


def _critical_path(graph: RouteGraph) -> tuple[tuple[str, ...], str]:
    """Return the route the level is built around, and how it was chosen.

    Preference order: an entry or spawn to the primary (first declared)
    objective and onward to the nearest exit, then entry to exit, then — when
    the scene declares no gameplay points at all — the level's diameter, the
    longest shortest path any two places impose on a player.
    """
    starts = [node.id for node in graph.nodes_of_kind(NodeKind.ENTRY, NodeKind.SPAWN)]
    objectives = [node.id for node in graph.nodes_of_kind(NodeKind.OBJECTIVE)]
    exits = [node.id for node in graph.nodes_of_kind(NodeKind.EXIT)]

    if starts and objectives:
        primary = objectives[0]
        approach = _best_path(graph, starts, [primary])
        if approach:
            if exits:
                onward = _best_path(graph, [primary], exits)
                if onward:
                    return approach + onward[1:], "entry to objective to exit"
            return approach, "entry to objective"
    if starts and exits:
        direct = _best_path(graph, starts, exits)
        if direct:
            return direct, "entry to exit"
    if starts or objectives or exits:
        return (), "declared journey unreachable"
    return _diameter(graph), "level diameter, nothing declared"


def _best_path(
    graph: RouteGraph, sources: Sequence[str], targets: Sequence[str]
) -> tuple[str, ...]:
    best: tuple[str, ...] = ()
    best_cost = math.inf
    for source in sources:
        for target in targets:
            path, distance = graph.shortest_path(source, target)
            if not path or math.isinf(distance):
                continue
            if distance < best_cost:
                best, best_cost = path, distance
    return best


def _diameter(graph: RouteGraph) -> tuple[str, ...]:
    best: tuple[str, ...] = ()
    best_cost = -1.0
    identifiers = graph.node_ids
    for index, source in enumerate(identifiers):
        for target in identifiers[index + 1 :]:
            path, distance = graph.shortest_path(source, target)
            if not path or math.isinf(distance):
                continue
            if distance > best_cost:
                best, best_cost = path, distance
    return best


def _path_cost(
    edges_by_key: dict[tuple[str, str], RouteEdge], path: Sequence[str]
) -> tuple[float, float]:
    length = 0.0
    travel_time = 0.0
    for first, second in zip(path, path[1:]):
        edge = edges_by_key.get((first, second) if first <= second else (second, first))
        if edge is None:
            continue
        length += edge.distance
        travel_time += edge.travel_time
    return length, travel_time


def _route_diversity(graph: RouteGraph, pairs: Sequence[tuple[str, str]]) -> int:
    """Return the fewest edge-disjoint routes any routable journey can call on."""
    counts = [
        graph.edge_disjoint_paths(source, target)
        for source, target in pairs
        if source != target and graph.shortest_path(source, target)[0]
    ]
    return min(counts) if counts else 0


def _isolated_nodes(graph: RouteGraph) -> tuple[RouteNode, ...]:
    """Return places outside the level's main body of connected space.

    A place with no route at all and a pocket of places cut off from the rest
    are the same design failure, so both are reported here.
    """
    components = graph.components()
    if len(components) <= 1:
        return ()
    main = components[0]
    return tuple(node for node in graph.nodes if node.id not in main)


def _chokepoints(
    graph: RouteGraph,
    loads: dict[tuple[str, str], float],
    width_limit: float,
) -> tuple[Chokepoint, ...]:
    """Return the routes that constrain movement, structurally or by width."""
    bridges = {edge.key: edge for edge in graph.bridges()}
    points: list[Chokepoint] = []
    for edge in graph.edges:
        split = (0, 0)
        structural = False
        if edge.key in bridges:
            near, far = _sides(graph, edge)
            if _side_is_substantial(near) and _side_is_substantial(far):
                structural = True
                split = (len(near), len(far))
        narrow = edge.width < width_limit
        if not structural and not narrow:
            continue
        points.append(
            Chokepoint(
                edge=edge,
                structural=structural,
                narrow=narrow,
                load=loads.get(edge.key, 0.0),
                split=split,
            )
        )
    return tuple(
        sorted(points, key=lambda point: (-point.load, not point.structural, point.key))
    )


def _sides(graph: RouteGraph, edge: RouteEdge) -> tuple[frozenset[str], frozenset[str]]:
    adjacency = graph.adjacency()

    def reachable(start: str) -> frozenset[str]:
        seen = {start}
        pending = deque([start])
        while pending:
            current = pending.popleft()
            for candidate in adjacency[current]:
                if candidate is edge:
                    continue
                neighbor = candidate.other(current)
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        return frozenset(seen)

    return reachable(edge.source), reachable(edge.target)


def _side_is_substantial(side: frozenset[str]) -> bool:
    """Return whether one side of a cut is a part of the level, not a terminus.

    A cut that isolates a single node is how every entry, exit, and objective
    attaches to the graph; calling each of those a chokepoint buries the one
    cut that really does split the level. A terminus reachable by only one
    route is still reported — as route diversity, where it belongs.
    """
    return len(side) >= 2


def _backtracking_ratio(
    graph: RouteGraph, pairs: Sequence[tuple[str, str]], routed: int
) -> float:
    """Return the share of journeys that re-tread a route an earlier one used.

    Journeys are walked in :func:`journey_pairs` order and each one's shortest
    path contributes its routes to a running set; a journey counts as
    backtracking when it shares at least one route with that set. The first
    routable journey therefore never counts, and the result is stable because
    the journey order is.
    """
    if routed == 0:
        return 0.0
    used: set[tuple[str, str]] = set()
    retread = 0
    for source, target in pairs:
        path, distance = graph.shortest_path(source, target)
        if not path or math.isinf(distance):
            continue
        keys = {
            (first, second) if first <= second else (second, first)
            for first, second in zip(path, path[1:])
        }
        if keys & used:
            retread += 1
        used |= keys
    return retread / routed
