"""The route graph: a level's traversable topology, derived from walkable space.

Nodes are the places a player aims for — entries, objectives, exits, zone
centres — and edges are the routes that actually connect them along walkable
ground, not straight lines through walls. Every flow, pacing, encounter, and
fairness question is answered against this graph.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping, Sequence

from mapwright.core.geometry import Vec3
from mapwright.core.metrics import OccupancyGrid, measure_passage_width
from mapwright.core.scene_ir import MarkerKind, SceneIR


class GraphError(ValueError):
    """Raised when a route graph cannot be derived from a scene."""


class NodeKind(str, Enum):
    """What role a place plays in the level's topology."""

    ENTRY = "entry"
    EXIT = "exit"
    OBJECTIVE = "objective"
    SPAWN = "spawn"
    HUB = "hub"
    JUNCTION = "junction"
    CORRIDOR = "corridor"
    DEAD_END = "dead_end"
    ISOLATED = "isolated"


#: Nodes whose kind comes from the scene rather than from graph structure.
SEMANTIC_KINDS = frozenset(
    {NodeKind.ENTRY, NodeKind.EXIT, NodeKind.OBJECTIVE, NodeKind.SPAWN}
)


@dataclass(frozen=True)
class RouteNode:
    """One place in the level that routes connect."""

    id: str
    kind: NodeKind
    position: Vec3
    cell: tuple[int, int]
    label: str
    zone: str | None = None
    team: str | None = None
    derived: bool = False

    @property
    def is_semantic(self) -> bool:
        """Return whether the scene declared this place, rather than sampling it."""
        return not self.derived


@dataclass(frozen=True)
class RouteEdge:
    """One walkable route between two places.

    ``width`` is the narrowest passable width along the route, measured from
    the clearance field, so it reports what a body must fit through rather
    than the nominal corridor size.
    """

    source: str
    target: str
    distance: float
    width: float
    travel_time: float
    elevation_change: float
    directness: float
    cells: tuple[tuple[int, int], ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        """Return the undirected identity of this route."""
        return (self.source, self.target) if self.source <= self.target else (self.target, self.source)

    def other(self, node_id: str) -> str:
        """Return the place at the far end of this route."""
        if node_id == self.source:
            return self.target
        if node_id == self.target:
            return self.source
        raise GraphError(f"Route {self.key} does not touch node '{node_id}'.")


@dataclass(frozen=True)
class RouteGraph:
    """The traversable topology of one level."""

    nodes: tuple[RouteNode, ...]
    edges: tuple[RouteEdge, ...]
    derived: bool = False

    def __post_init__(self) -> None:
        known = {node.id for node in self.nodes}
        for edge in self.edges:
            missing = {edge.source, edge.target} - known
            if missing:
                raise GraphError(
                    f"Route references unknown node(s): {', '.join(sorted(missing))}."
                )

    @property
    def node_ids(self) -> tuple[str, ...]:
        """Return every node identifier in declaration order."""
        return tuple(node.id for node in self.nodes)

    def node(self, node_id: str) -> RouteNode:
        """Return one node by identifier."""
        found = next((node for node in self.nodes if node.id == node_id), None)
        if found is None:
            raise GraphError(f"Unknown route node '{node_id}'.")
        return found

    def nodes_of_kind(self, *kinds: NodeKind) -> tuple[RouteNode, ...]:
        """Return every node matching any of the given kinds."""
        wanted = set(kinds)
        return tuple(node for node in self.nodes if node.kind in wanted)

    def adjacency(self) -> dict[str, tuple[RouteEdge, ...]]:
        """Return each node's incident routes."""
        result: dict[str, list[RouteEdge]] = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            result[edge.source].append(edge)
            result[edge.target].append(edge)
        return {key: tuple(value) for key, value in result.items()}

    def degree(self, node_id: str) -> int:
        """Return how many routes touch a place."""
        return sum(
            1 for edge in self.edges if node_id in (edge.source, edge.target)
        )

    def components(self) -> tuple[frozenset[str], ...]:
        """Return the connected groups of places, largest first."""
        adjacency = self.adjacency()
        seen: set[str] = set()
        groups: list[frozenset[str]] = []
        for node in self.nodes:
            if node.id in seen:
                continue
            pending = deque([node.id])
            seen.add(node.id)
            group = {node.id}
            while pending:
                current = pending.popleft()
                for edge in adjacency[current]:
                    neighbor = edge.other(current)
                    if neighbor not in seen:
                        seen.add(neighbor)
                        group.add(neighbor)
                        pending.append(neighbor)
            groups.append(frozenset(group))
        return tuple(sorted(groups, key=lambda group: (-len(group), sorted(group)[0])))

    @property
    def cycle_count(self) -> int:
        """Return the number of independent loops in the topology.

        This is the cyclomatic number ``E - V + components``: zero means a pure
        tree with no way to come back by a different route.
        """
        return len(self.edges) - len(self.nodes) + len(self.components())

    def shortest_path(self, source: str, target: str) -> tuple[tuple[str, ...], float]:
        """Return the fewest-metres route between two places."""
        self.node(source)
        self.node(target)
        if source == target:
            return (source,), 0.0
        adjacency = self.adjacency()
        distances = {source: 0.0}
        previous: dict[str, str] = {}
        queue: list[tuple[float, str]] = [(0.0, source)]
        visited: set[str] = set()
        while queue:
            distance, current = heapq.heappop(queue)
            if current in visited:
                continue
            visited.add(current)
            if current == target:
                break
            for edge in adjacency[current]:
                neighbor = edge.other(current)
                candidate = distance + edge.distance
                if candidate < distances.get(neighbor, math.inf):
                    distances[neighbor] = candidate
                    previous[neighbor] = current
                    heapq.heappush(queue, (candidate, neighbor))
        if target not in distances:
            return (), math.inf
        path = [target]
        while path[-1] != source:
            path.append(previous[path[-1]])
        path.reverse()
        return tuple(path), distances[target]

    def bridges(self) -> tuple[RouteEdge, ...]:
        """Return routes whose loss would split the level in two.

        A bridge is a structural chokepoint: every player crossing between the
        two sides must use it, whatever its width. Parallel routes between the
        same pair are handled by identity, so a doubled connection is correctly
        not a bridge.
        """
        incident: dict[str, list[tuple[int, str]]] = {node.id: [] for node in self.nodes}
        for index, edge in enumerate(self.edges):
            incident[edge.source].append((index, edge.target))
            incident[edge.target].append((index, edge.source))

        discovery: dict[str, int] = {}
        low: dict[str, int] = {}
        bridge_indices: set[int] = set()
        counter = 0

        # Iterative depth-first search; recursion would cap level size.
        for start in self.node_ids:
            if start in discovery:
                continue
            discovery[start] = low[start] = counter
            counter += 1
            stack: list[tuple[str, int, int]] = [(start, -1, 0)]
            while stack:
                current, arrival, cursor = stack[-1]
                if cursor < len(incident[current]):
                    stack[-1] = (current, arrival, cursor + 1)
                    edge_index, neighbor = incident[current][cursor]
                    if edge_index == arrival:
                        continue
                    if neighbor in discovery:
                        low[current] = min(low[current], discovery[neighbor])
                    else:
                        discovery[neighbor] = low[neighbor] = counter
                        counter += 1
                        stack.append((neighbor, edge_index, 0))
                    continue
                stack.pop()
                if stack:
                    ancestor = stack[-1][0]
                    low[ancestor] = min(low[ancestor], low[current])
                    if low[current] > discovery[ancestor]:
                        bridge_indices.add(arrival)
        return tuple(
            sorted(
                (self.edges[index] for index in bridge_indices),
                key=lambda edge: edge.key,
            )
        )

    def edge_disjoint_paths(self, source: str, target: str) -> int:
        """Return how many routes between two places share no segment.

        One means a single point of failure; two or more means the player has
        a genuine alternative rather than a cosmetic detour.
        """
        self.node(source)
        self.node(target)
        if source == target:
            return 0
        capacity: dict[str, dict[str, int]] = {node.id: {} for node in self.nodes}
        for edge in self.edges:
            capacity[edge.source][edge.target] = (
                capacity[edge.source].get(edge.target, 0) + 1
            )
            capacity[edge.target][edge.source] = (
                capacity[edge.target].get(edge.source, 0) + 1
            )
        flow = 0
        while True:
            previous: dict[str, str] = {source: source}
            pending = deque([source])
            while pending and target not in previous:
                current = pending.popleft()
                for neighbor, remaining in capacity[current].items():
                    if remaining > 0 and neighbor not in previous:
                        previous[neighbor] = current
                        pending.append(neighbor)
            if target not in previous:
                return flow
            node = target
            while node != source:
                parent = previous[node]
                capacity[parent][node] -= 1
                capacity[node][parent] += 1
                node = parent
            flow += 1

    def edge_loads(
        self, pairs: Sequence[tuple[str, str]]
    ) -> tuple[dict[tuple[str, str], float], int]:
        """Return each route's share of the traffic across the given journeys.

        Routing every meaningful journey along its shortest path shows where
        traversal concentrates: the busiest edge's share is the number quoted
        in flow findings.
        """
        loads: dict[tuple[str, str], float] = {edge.key: 0.0 for edge in self.edges}
        routed = 0
        for source, target in pairs:
            path, distance = self.shortest_path(source, target)
            if not path or math.isinf(distance):
                continue
            routed += 1
            for first, second in zip(path, path[1:]):
                key = (first, second) if first <= second else (second, first)
                if key in loads:
                    loads[key] += 1.0
        if routed:
            loads = {key: value / routed for key, value in loads.items()}
        return loads, routed

    def to_dict(self) -> dict:
        """Return a JSON-serializable summary of the topology."""
        return {
            "derived": self.derived,
            "nodes": [
                {
                    "id": node.id,
                    "kind": node.kind.value,
                    "position": node.position.to_list(),
                    "label": node.label,
                    "zone": node.zone,
                    "team": node.team,
                    "degree": self.degree(node.id),
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "distance": round(edge.distance, 4),
                    "width": round(edge.width, 4),
                    "travel_time": round(edge.travel_time, 4),
                    "elevation_change": round(edge.elevation_change, 4),
                    "directness": round(edge.directness, 4),
                }
                for edge in self.edges
            ],
        }


def build_route_graph(
    scene: SceneIR,
    grid: OccupancyGrid,
    walk_speed: float,
    clearance: Sequence[Sequence[float]] | None = None,
    minimum_nodes: int = 6,
) -> RouteGraph:
    """Derive the route graph from declared places and walkable space.

    Declared entries, spawns, objectives, exits, and zone centres become nodes.
    When a scene declares too few, walkable space is sampled by farthest-point
    selection so the topology of an unannotated level can still be measured —
    such a graph is marked ``derived`` and reported as an approximation.
    """
    if walk_speed <= 0:
        raise GraphError("Walk speed must be positive to estimate travel time.")
    field = clearance if clearance is not None else grid.clearance_field()
    anchors = _collect_anchors(scene)
    declared = len(anchors)
    if declared < minimum_nodes:
        anchors.extend(
            _sample_waypoints(grid, field, anchors, minimum_nodes - declared)
        )
    nodes = _snap_nodes(grid, anchors)
    if len(nodes) < 2:
        raise GraphError(
            "Route analysis needs at least two reachable places; the scene "
            "declares none and walkable space is too small to sample."
        )

    cells = [node.cell for node in nodes]
    owner, _ = grid.voronoi_labels(cells)
    adjacent = _adjacent_owners(grid, owner)
    edges: list[RouteEdge] = []
    for first, second in sorted(adjacent):
        source, target = nodes[first], nodes[second]
        path = grid.path(source.cell, target.cell, field)
        if not path.reachable:
            continue
        straight = source.position.distance_xz(target.position)
        widest, _ = grid.widest_path(source.cell, target.cell, field)
        edges.append(
            RouteEdge(
                source=source.id,
                target=target.id,
                distance=path.length,
                width=measure_passage_width(grid, widest),
                travel_time=path.length / walk_speed,
                elevation_change=abs(source.position.y - target.position.y),
                directness=straight / path.length if path.length > 0 else 1.0,
                cells=path.cells,
            )
        )
    return RouteGraph(
        nodes=_classify(nodes, edges),
        edges=tuple(edges),
        derived=declared < minimum_nodes,
    )


@dataclass(frozen=True)
class _Anchor:
    """A candidate place, before it is snapped onto walkable ground."""

    id: str
    kind: NodeKind
    position: Vec3
    label: str
    zone: str | None = None
    team: str | None = None
    derived: bool = False


def _collect_anchors(scene: SceneIR) -> list[_Anchor]:
    """Return every place the scene declares, markers first then zone centres."""
    kinds = {
        MarkerKind.ENTRY: NodeKind.ENTRY,
        MarkerKind.SPAWN: NodeKind.SPAWN,
        MarkerKind.OBJECTIVE: NodeKind.OBJECTIVE,
        MarkerKind.EXIT: NodeKind.EXIT,
    }
    anchors = [
        _Anchor(
            id=point.id,
            kind=kinds[point.kind],
            position=point.position,
            label=point.label,
            zone=point.zone,
            team=point.team,
        )
        for point in scene.entry_points
        + scene.spawn_points
        + scene.objectives
        + scene.exits
    ]
    taken = {(round(anchor.position.x, 3), round(anchor.position.z, 3)) for anchor in anchors}
    for zone in scene.zones:
        if zone.bounds is None:
            continue
        center = zone.bounds.center
        if (round(center.x, 3), round(center.z, 3)) in taken:
            continue
        anchors.append(
            _Anchor(
                id=f"zone:{zone.name}",
                kind=NodeKind.CORRIDOR,
                position=center,
                label=zone.name,
                zone=zone.name,
            )
        )
    return anchors


def _sample_waypoints(
    grid: OccupancyGrid,
    clearance: Sequence[Sequence[float]],
    existing: Sequence[_Anchor],
    count: int,
) -> list[_Anchor]:
    """Pick well-separated open places to stand in for undeclared structure.

    Farthest-point sampling, tie-broken by openness then cell order, so the
    same scene always yields the same waypoints.
    """
    free = [
        (column, row)
        for row in range(grid.rows)
        for column in range(grid.columns)
        if not grid.blocked[row][column]
    ]
    if not free:
        return []
    chosen = [
        cell
        for anchor in existing
        if (cell := grid.nearest_free_cell(anchor.position)) is not None
    ]
    sampled: list[_Anchor] = []
    for _ in range(count):
        if chosen:
            cell = max(
                free,
                key=lambda candidate: (
                    min(
                        (candidate[0] - other[0]) ** 2 + (candidate[1] - other[1]) ** 2
                        for other in chosen
                    ),
                    round(clearance[candidate[1]][candidate[0]], 6),
                    -candidate[1],
                    -candidate[0],
                ),
            )
            if cell in chosen:
                break
        else:
            # Start from the most open point: the middle of the largest space.
            cell = max(
                free,
                key=lambda candidate: (
                    round(clearance[candidate[1]][candidate[0]], 6),
                    -candidate[1],
                    -candidate[0],
                ),
            )
        chosen.append(cell)
        index = len(sampled) + 1
        sampled.append(
            _Anchor(
                id=f"waypoint_{index}",
                kind=NodeKind.CORRIDOR,
                position=grid.cell_center(*cell),
                label=f"waypoint {index}",
                derived=True,
            )
        )
    return sampled


def _snap_nodes(grid: OccupancyGrid, anchors: Sequence[_Anchor]) -> list[RouteNode]:
    """Snap every anchor onto walkable ground, dropping duplicates."""
    nodes: list[RouteNode] = []
    used: set[tuple[int, int]] = set()
    for anchor in anchors:
        cell = grid.nearest_free_cell(anchor.position)
        if cell is None or cell in used:
            continue
        used.add(cell)
        nodes.append(
            RouteNode(
                id=anchor.id,
                kind=anchor.kind,
                position=grid.cell_center(*cell),
                cell=cell,
                label=anchor.label,
                zone=anchor.zone,
                team=anchor.team,
                derived=anchor.derived,
            )
        )
    return nodes


def _adjacent_owners(
    grid: OccupancyGrid, owner: Sequence[Sequence[int]]
) -> set[tuple[int, int]]:
    """Return index pairs whose walkable territories touch."""
    pairs: set[tuple[int, int]] = set()
    for row in range(grid.rows):
        for column in range(grid.columns):
            current = owner[row][column]
            if current < 0:
                continue
            for next_column, next_row in ((column + 1, row), (column, row + 1)):
                if next_column >= grid.columns or next_row >= grid.rows:
                    continue
                neighbor = owner[next_row][next_column]
                if neighbor < 0 or neighbor == current:
                    continue
                if not grid.is_free(next_column, next_row):
                    continue
                pairs.add((min(current, neighbor), max(current, neighbor)))
    return pairs


def _classify(nodes: Sequence[RouteNode], edges: Sequence[RouteEdge]) -> tuple[RouteNode, ...]:
    """Assign structural kinds to places the scene did not name."""
    degrees: dict[str, int] = {node.id: 0 for node in nodes}
    for edge in edges:
        degrees[edge.source] += 1
        degrees[edge.target] += 1
    classified = []
    for node in nodes:
        if node.kind in SEMANTIC_KINDS:
            classified.append(node)
            continue
        degree = degrees[node.id]
        if degree == 0:
            kind = NodeKind.ISOLATED
        elif degree == 1:
            kind = NodeKind.DEAD_END
        elif degree == 2:
            kind = NodeKind.CORRIDOR
        elif degree == 3:
            kind = NodeKind.JUNCTION
        else:
            kind = NodeKind.HUB
        classified.append(replace(node, kind=kind))
    return tuple(classified)


def journey_pairs(graph: RouteGraph) -> tuple[tuple[str, str], ...]:
    """Return the journeys a player is expected to make.

    Entries and spawns to objectives, objectives onward to exits, and entries
    straight to exits when a level declares no objective.
    """
    starts = [node.id for node in graph.nodes_of_kind(NodeKind.ENTRY, NodeKind.SPAWN)]
    objectives = [node.id for node in graph.nodes_of_kind(NodeKind.OBJECTIVE)]
    exits = [node.id for node in graph.nodes_of_kind(NodeKind.EXIT)]
    pairs: list[tuple[str, str]] = []
    for start in starts:
        pairs.extend((start, objective) for objective in objectives)
        if not objectives:
            pairs.extend((start, exit_id) for exit_id in exits)
    for objective in objectives:
        pairs.extend((objective, exit_id) for exit_id in exits)
    if pairs:
        return tuple(dict.fromkeys(pairs))
    # Nothing is declared: measure the longest journeys the space supports.
    identifiers = list(graph.node_ids)
    return tuple(
        (first, second)
        for index, first in enumerate(identifiers)
        for second in identifiers[index + 1 :]
    )
