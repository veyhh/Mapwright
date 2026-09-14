"""Competitive fairness: does every team start the match on equal terms.

A PvP layout is decided before the first shot when one team reaches the
objective sooner, arrives with more cover, holds the high ground, or funnels
through a narrower door. This validator measures each of those along the route
each team actually walks, and reports the gap as a percentage against the
tolerance the profile sets — a MOBA lane and a co-op arena disagree sharply
about how much asymmetry is acceptable.

It runs only when the scene declares spawn points for two or more teams.
Everything else is single-player space, where fairness is not a question that
can be asked, let alone scored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from mapwright.core.config import MapwrightConfig
from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.graph import NodeKind, RouteEdge, RouteGraph, RouteNode
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.core.metrics import OccupancyGrid
from mapwright.core.scene_ir import SceneObject

#: Finding raised when the scene cannot support a fairness comparison at all.
NOT_APPLICABLE = "fairness_not_applicable"

#: Smallest top surface that counts as somewhere a player can stand.
MINIMUM_STAND_AREA = 1.0

#: Most route samples taken per edge; enough to follow a corridor's shape.
MAXIMUM_EDGE_SAMPLES = 32

_TRAVEL_TIME = "travel_time"
_OBJECTIVE_ACCESS = "objective_access"
_COVER = "cover_count"
_CHOKE = "choke_width"
_FLANK = "flank_routes"
_HIGH_GROUND = "high_ground_access"

_ALL_OBJECTIVES = "*"

_EXPLANATIONS = {
    "travel_time_imbalance": (
        "The team that reaches the objective first sets the terms of every "
        "fight there, and the gap compounds on every respawn."
    ),
    "objective_access_imbalance": (
        "Access to the objective set decides who plays offence. A team that "
        "is closer to every objective controls the map by default."
    ),
    "cover_imbalance": (
        "Cover on the approach decides who can push and who must trade. A "
        "team crossing open ground loses the engagement before contact."
    ),
    "high_ground_advantage": (
        "Elevation grants sightlines, first sight, and a shooting angle no "
        "amount of skill recovers from below."
    ),
    "choke_width_imbalance": (
        "A narrower approach means a team can be held by fewer defenders and "
        "cannot rotate a full squad through it at once."
    ),
    "flank_route_imbalance": (
        "Alternative routes are what let a team answer a lost fight. One "
        "route means one plan; the other team has more."
    ),
}


@dataclass(frozen=True)
class TeamAccess:
    """What one team's best route to one objective actually offers."""

    team: str
    objective: str
    objective_label: str
    spawn: str
    distance: float
    travel_time: float
    cover_count: int
    high_ground_access: int
    choke_width: float
    flank_routes: int
    reachable: bool

    def value(self, metric: str) -> float:
        """Return one measured value by metric name."""
        return {
            _TRAVEL_TIME: self.travel_time,
            _COVER: float(self.cover_count),
            _CHOKE: self.choke_width,
            _FLANK: float(self.flank_routes),
            _HIGH_GROUND: float(self.high_ground_access),
        }[metric]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "team": self.team,
            "objective": self.objective,
            "objective_label": self.objective_label,
            "spawn": self.spawn,
            "distance": _finite(self.distance),
            "travel_time": _finite(self.travel_time),
            "cover_count": self.cover_count,
            "high_ground_access": self.high_ground_access,
            "choke_width": round(self.choke_width, 3),
            "flank_routes": self.flank_routes,
            "reachable": self.reachable,
        }


@dataclass(frozen=True)
class FairnessComparison:
    """The gap between the best and worst served team on one measurement."""

    objective: str
    metric: str
    best_team: str
    worst_team: str
    best_value: float
    worst_value: float
    asymmetry_percent: float
    objective_label: str
    unit: str
    higher_is_better: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "objective": self.objective,
            "objective_label": self.objective_label,
            "metric": self.metric,
            "best_team": self.best_team,
            "worst_team": self.worst_team,
            "best_value": round(self.best_value, 3),
            "worst_value": round(self.worst_value, 3),
            "asymmetry_percent": round(self.asymmetry_percent, 2),
        }


@dataclass(frozen=True)
class FairnessReport:
    """Every team's objective access, and where the gaps exceed tolerance."""

    issues: tuple[Issue, ...]
    teams: tuple[str, ...]
    access: tuple[TeamAccess, ...]
    comparisons: tuple[FairnessComparison, ...]
    worst_asymmetry: float

    @property
    def applicable(self) -> bool:
        """Return whether the scene supports a fairness comparison at all.

        Measured access is enough: an objective one team cannot reach at all
        produces no comparison but is the sharpest fairness failure there is,
        so the category still counts.
        """
        return len(self.teams) >= 2 and bool(self.access)

    @property
    def reason(self) -> str | None:
        """Return why fairness could not be measured, when it could not."""
        if self.applicable:
            return None
        skipped = next(
            (issue for issue in self.issues if issue.code == NOT_APPLICABLE), None
        )
        if skipped is not None:
            return skipped.evidence
        return "this scene does not support a fairness comparison"

    def for_team(self, team: str) -> tuple[TeamAccess, ...]:
        """Return one team's access records."""
        return tuple(item for item in self.access if item.team == team)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "applicable": self.applicable,
            "reason": self.reason,
            "teams": list(self.teams),
            "worst_asymmetry": round(self.worst_asymmetry, 2),
            "access": [item.to_dict() for item in self.access],
            "comparisons": [item.to_dict() for item in self.comparisons],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def validate_fairness(context: AnalysisContext) -> FairnessReport:
    """Compare every team's route to every objective and report the gaps."""
    scene = context.scene
    collector = IssueCollector(Category.FAIRNESS)
    teams = scene.teams

    if len(teams) < 2:
        return _skip(
            collector,
            context.config,
            teams,
            evidence=(
                f"The scene declares {len(scene.spawn_points)} spawn point(s) "
                f"covering {len(teams)} team(s): "
                f"{', '.join(teams) if teams else 'none named'}."
            ),
            recommendation=(
                "Tag at least two spawn points with different 'team' values to "
                "enable fairness analysis, or leave it off for single-player space."
            ),
        )

    graph = context.graph
    grid = context.grid
    if graph is None or grid is None:
        return _skip(
            collector,
            context.config,
            teams,
            evidence=(
                f"Teams {', '.join(teams)} are declared, but no route graph could "
                f"be derived: {context.graph_reason}."
            ),
            recommendation=(
                "Give the scene measurable ground and walkable space between "
                "spawns and objectives, then re-run the analysis."
            ),
        )

    objectives = tuple(
        sorted(graph.nodes_of_kind(NodeKind.OBJECTIVE), key=lambda node: node.id)
    )
    spawns = _spawns_by_team(graph, teams)
    if not objectives:
        return _skip(
            collector,
            context.config,
            teams,
            evidence=(
                f"Teams {', '.join(teams)} are declared, but the scene declares no "
                "objective to contest."
            ),
            recommendation=(
                "Declare the contested point as an objective marker so each "
                "team's route to it can be compared."
            ),
        )
    if len(spawns) < 2:
        return _skip(
            collector,
            context.config,
            teams,
            evidence=(
                f"Only {len(spawns)} of {len(teams)} team(s) have a spawn on "
                "walkable ground: "
                f"{', '.join(sorted(spawns)) if spawns else 'none'}."
            ),
            recommendation=(
                "Move team spawns onto open ground so each team's route can be "
                "traced from where players actually appear."
            ),
        )

    access = _measure_access(context, graph, grid, spawns, objectives)
    comparisons = _compare(access, objectives)
    _raise_issues(collector, context, access, comparisons, objectives)

    return FairnessReport(
        issues=collector.result(),
        teams=tuple(sorted(spawns)),
        access=access,
        comparisons=comparisons,
        worst_asymmetry=max(
            (item.asymmetry_percent for item in comparisons), default=0.0
        ),
    )


def format_report(report: FairnessReport) -> str:
    """Render a fairness report as terminal text."""
    if not report.applicable:
        lines = ["FAIRNESS  not applicable", report.reason or ""]
        for issue in report.issues:
            lines.append(f"{issue.severity.value} {issue.summary}")
            lines.append(f"Recommendation: {issue.recommendation}")
        return "\n".join(line for line in lines if line) + "\n"

    objectives = tuple(dict.fromkeys(item.objective_label for item in report.access))
    lines = [
        f"FAIRNESS  {len(report.teams)} teams, {len(objectives)} objective(s), "
        f"worst asymmetry {report.worst_asymmetry:.1f}%",
        "",
    ]
    for item in sorted(report.access, key=lambda row: (row.objective, row.team)):
        heading = f"{_team_label(item.team)} -> {item.objective_label.upper()}  "
        if not item.reachable:
            lines.append(f"{heading}UNREACHABLE: no walkable route from its spawn")
            continue
        lines.append(
            f"{heading}{item.travel_time:.1f}s over {item.distance:.1f}m, "
            f"{item.cover_count} cover, {item.high_ground_access} high ground, "
            f"{item.choke_width:.1f}m choke, {item.flank_routes} flank route(s)"
        )
    lines.append("")
    if not report.issues:
        lines.append("No fairness findings: every measured gap is within tolerance.")
    for issue in report.issues:
        lines.append(issue.evidence)
        lines.append(f"{issue.severity.value} {issue.summary}")
        if issue.explanation:
            lines.append(issue.explanation)
        lines.append(f"Recommendation: {issue.recommendation}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _skip(
    collector: IssueCollector,
    config: MapwrightConfig,
    teams: Sequence[str],
    evidence: str,
    recommendation: str,
) -> FairnessReport:
    """Return an empty report explaining why fairness was not measured."""
    collector.add(
        code=NOT_APPLICABLE,
        severity=config.severity_for(NOT_APPLICABLE, Severity.INFO),
        summary="Fairness analysis needs spawn points for two or more teams.",
        evidence=evidence,
        recommendation=recommendation,
        explanation=(
            "Competitive symmetry is only meaningful between teams. Without "
            "team spawns and a contested objective there is nothing to compare, "
            "so this category is reported as not applicable rather than scored."
        ),
        metrics=(("teams", float(len(teams))),),
    )
    return FairnessReport(
        issues=collector.result(),
        teams=tuple(teams),
        access=(),
        comparisons=(),
        worst_asymmetry=0.0,
    )


def _spawns_by_team(
    graph: RouteGraph, teams: Sequence[str]
) -> dict[str, tuple[RouteNode, ...]]:
    """Return each team's spawn nodes that made it onto the route graph."""
    known = set(teams)
    grouped: dict[str, list[RouteNode]] = {}
    for node in graph.nodes_of_kind(NodeKind.SPAWN):
        if node.team in known:
            grouped.setdefault(node.team, []).append(node)
    return {
        team: tuple(sorted(nodes, key=lambda node: node.id))
        for team, nodes in sorted(grouped.items())
    }


def _measure_access(
    context: AnalysisContext,
    graph: RouteGraph,
    grid: OccupancyGrid,
    spawns: dict[str, tuple[RouteNode, ...]],
    objectives: Sequence[RouteNode],
) -> tuple[TeamAccess, ...]:
    """Measure every team's best route to every objective."""
    config = context.config
    reach = _route_reach(config)
    cover = _cover_objects(context)
    stands = _high_ground_objects(context)
    edges = _edges_by_pair(graph)
    records: list[TeamAccess] = []

    for team, nodes in sorted(spawns.items()):
        for objective in objectives:
            best: tuple[float, RouteNode, tuple[str, ...]] | None = None
            for node in nodes:
                path, distance = graph.shortest_path(node.id, objective.id)
                if not path or not math.isfinite(distance):
                    continue
                if best is None or distance < best[0]:
                    best = (distance, node, path)
            if best is None:
                fallback = nodes[0]
                records.append(
                    TeamAccess(
                        team=team,
                        objective=objective.id,
                        objective_label=objective.label,
                        spawn=fallback.id,
                        distance=math.inf,
                        travel_time=math.inf,
                        cover_count=0,
                        high_ground_access=0,
                        choke_width=0.0,
                        flank_routes=0,
                        reachable=False,
                    )
                )
                continue
            distance, spawn, path = best
            samples = _route_samples(graph, grid, path, edges)
            records.append(
                TeamAccess(
                    team=team,
                    objective=objective.id,
                    objective_label=objective.label,
                    spawn=spawn.id,
                    distance=distance,
                    travel_time=config.player.travel_time(distance),
                    cover_count=_count_near(cover, samples, reach),
                    high_ground_access=_count_near(stands, samples, reach),
                    choke_width=_narrowest(path, edges),
                    flank_routes=graph.edge_disjoint_paths(spawn.id, objective.id),
                    reachable=True,
                )
            )
    return tuple(records)


def _compare(
    access: Sequence[TeamAccess], objectives: Sequence[RouteNode]
) -> tuple[FairnessComparison, ...]:
    """Reduce every team's measurements to best-versus-worst gaps."""
    comparisons: list[FairnessComparison] = []
    for objective in objectives:
        rows = [item for item in access if item.objective == objective.id and item.reachable]
        if len(rows) < 2:
            continue
        for metric, unit, higher_is_better in (
            (_TRAVEL_TIME, "s", False),
            (_COVER, "cover", True),
            (_CHOKE, "m choke", True),
            (_FLANK, "flank route(s)", True),
            (_HIGH_GROUND, "high-ground position(s)", True),
        ):
            comparisons.append(
                _comparison(
                    objective.id,
                    objective.label,
                    metric,
                    unit,
                    higher_is_better,
                    [(row.team, row.value(metric)) for row in rows],
                )
            )

    if len(objectives) > 1:
        means: list[tuple[str, float]] = []
        for team in sorted({item.team for item in access}):
            times = [
                item.travel_time
                for item in access
                if item.team == team and item.reachable
            ]
            if len(times) == len(objectives):
                means.append((team, sum(times) / len(times)))
        if len(means) >= 2:
            comparisons.append(
                _comparison(
                    _ALL_OBJECTIVES,
                    "all objectives",
                    _OBJECTIVE_ACCESS,
                    "s mean",
                    False,
                    means,
                )
            )
    return tuple(comparisons)


def _comparison(
    objective: str,
    label: str,
    metric: str,
    unit: str,
    higher_is_better: bool,
    values: Sequence[tuple[str, float]],
) -> FairnessComparison:
    """Build one best-versus-worst comparison from per-team values."""
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    best = ordered[-1] if higher_is_better else ordered[0]
    worst = ordered[0] if higher_is_better else ordered[-1]
    return FairnessComparison(
        objective=objective,
        metric=metric,
        best_team=best[0],
        worst_team=worst[0],
        best_value=best[1],
        worst_value=worst[1],
        asymmetry_percent=_asymmetry(best[1], worst[1]),
        objective_label=label,
        unit=unit,
        higher_is_better=higher_is_better,
    )


def _finite(value: float) -> float | None:
    """Return a measurement, or ``None`` where there is no route to measure."""
    return round(value, 3) if math.isfinite(value) else None


def _asymmetry(best: float, worst: float) -> float:
    """Return the percentage gap between the best and worst served team.

    ``100 * (worst - best) / best`` in magnitude, so the number always reads as
    "how much worse the disadvantaged team has it". A zero best value cannot be
    divided by: an identical pair is a 0% gap, anything else is total.
    """
    if not math.isfinite(best) or not math.isfinite(worst):
        return 0.0
    if best == 0.0:
        return 0.0 if worst == 0.0 else 100.0
    return abs(100.0 * (worst - best) / best)


def _raise_issues(
    collector: IssueCollector,
    context: AnalysisContext,
    access: Sequence[TeamAccess],
    comparisons: Sequence[FairnessComparison],
    objectives: Sequence[RouteNode],
) -> None:
    """Turn the gaps that exceed tolerance into explainable findings."""
    config = context.config
    thresholds = config.thresholds
    _raise_unreachable(collector, config, access, objectives)

    for comparison in comparisons:
        if comparison.metric == _TRAVEL_TIME:
            limit = thresholds.travel_time_asymmetry
            if comparison.asymmetry_percent <= limit:
                continue
            excess = comparison.worst_value - comparison.best_value * (
                1.0 + limit / 100.0
            )
            _add(
                collector,
                config,
                "travel_time_imbalance",
                Severity.WARNING,
                "Travel-time imbalance exceeds configured threshold.",
                comparison,
                limit,
                (
                    f"Move {_team_label(comparison.worst_team)}'s spawn about "
                    f"{max(excess * config.player.walk_speed, 0.1):.0f} m closer to "
                    f"{comparison.objective_label.upper()}, or open a shorter route, "
                    f"so both teams arrive within {limit:.0f}%."
                ),
            )
        elif comparison.metric == _OBJECTIVE_ACCESS:
            limit = thresholds.travel_time_asymmetry
            if comparison.asymmetry_percent <= limit:
                continue
            _add(
                collector,
                config,
                "objective_access_imbalance",
                Severity.WARNING,
                "Objective access is unequal across the objective set.",
                comparison,
                limit,
                (
                    f"Rebalance the objective set: {_team_label(comparison.best_team)} "
                    f"is closer to every objective on average. Move one objective "
                    f"toward {_team_label(comparison.worst_team)} or add a route that "
                    f"shortens its approach."
                ),
            )
        elif comparison.metric == _COVER:
            limit = thresholds.cover_asymmetry
            if comparison.asymmetry_percent <= limit:
                continue
            shortfall = max(
                1,
                math.ceil(
                    comparison.best_value * (1.0 - limit / 100.0)
                    - comparison.worst_value
                ),
            )
            _add(
                collector,
                config,
                "cover_imbalance",
                Severity.WARNING,
                "Cover along the approach is unequal between teams.",
                comparison,
                limit,
                (
                    f"Add {shortfall} piece(s) of waist-height cover within "
                    f"{_route_reach(config):.0f} m of "
                    f"{_team_label(comparison.worst_team)}'s route to "
                    f"{comparison.objective_label.upper()}."
                ),
            )
        elif comparison.metric == _CHOKE:
            limit = thresholds.choke_width_asymmetry
            if comparison.asymmetry_percent <= limit:
                continue
            target = comparison.best_value * (1.0 - limit / 100.0)
            _add(
                collector,
                config,
                "choke_width_imbalance",
                Severity.WARNING,
                "Approach chokepoints differ in width between teams.",
                comparison,
                limit,
                (
                    f"Widen the narrowest passage on "
                    f"{_team_label(comparison.worst_team)}'s route from "
                    f"{comparison.worst_value:.1f} m to at least {target:.1f} m."
                ),
            )
        elif comparison.metric == _FLANK:
            if comparison.best_value - comparison.worst_value < 1.0:
                continue
            missing = int(round(comparison.best_value - comparison.worst_value))
            _add(
                collector,
                config,
                "flank_route_imbalance",
                Severity.WARNING,
                "One team has fewer independent approaches than another.",
                comparison,
                0.0,
                (
                    f"Open {missing} additional route"
                    f"{'' if missing == 1 else 's'} from "
                    f"{_team_label(comparison.worst_team)}'s spawn to "
                    f"{comparison.objective_label.upper()} that share no segment "
                    "with its current approach."
                ),
            )
        elif comparison.metric == _HIGH_GROUND:
            if comparison.worst_value > 0 or comparison.best_value <= 0:
                continue
            _add(
                collector,
                config,
                "high_ground_advantage",
                Severity.WARNING,
                "Only one team has high ground on its approach.",
                comparison,
                0.0,
                (
                    f"Add an elevated position at least "
                    f"{thresholds.high_ground_delta:.1f} m above ground within "
                    f"{_route_reach(config):.0f} m of "
                    f"{_team_label(comparison.worst_team)}'s route, or remove the "
                    f"one on {_team_label(comparison.best_team)}'s."
                ),
            )


def _raise_unreachable(
    collector: IssueCollector,
    config: MapwrightConfig,
    access: Sequence[TeamAccess],
    objectives: Sequence[RouteNode],
) -> None:
    """Flag an objective one team can reach and another cannot."""
    for objective in objectives:
        rows = [item for item in access if item.objective == objective.id]
        blocked = sorted(item.team for item in rows if not item.reachable)
        reaching = sorted(item.team for item in rows if item.reachable)
        if not blocked or not reaching:
            continue
        collector.add(
            code="objective_access_imbalance",
            severity=config.severity_for(
                "objective_access_imbalance", Severity.ERROR
            ),
            summary="An objective is reachable for one team and not another.",
            evidence=(
                f"{_team_label(reaching[0])} -> {objective.label.upper()} reachable "
                f"/ {_team_label(blocked[0])} -> {objective.label.upper()} "
                f"UNREACHABLE / ASYMMETRY 100.0%"
            ),
            recommendation=(
                f"Connect {_team_label(blocked[0])}'s spawn to "
                f"{objective.label.upper()} along walkable ground, or move the "
                "spawn into the connected part of the level."
            ),
            explanation=_EXPLANATIONS["objective_access_imbalance"],
            subjects=(objective.id,),
            metrics=(
                ("teams_blocked", float(len(blocked))),
                ("teams_reaching", float(len(reaching))),
            ),
        )


def _add(
    collector: IssueCollector,
    config: MapwrightConfig,
    code: str,
    default: Severity,
    summary: str,
    comparison: FairnessComparison,
    limit: float,
    recommendation: str,
) -> Issue:
    """Record one imbalance finding with both teams' numbers in evidence."""
    return collector.add(
        code=code,
        severity=config.severity_for(code, default),
        summary=summary,
        evidence=_evidence(comparison, limit),
        recommendation=recommendation,
        explanation=_EXPLANATIONS[code],
        subjects=(comparison.best_team, comparison.worst_team),
        metrics=(
            ("best_value", round(comparison.best_value, 3)),
            ("worst_value", round(comparison.worst_value, 3)),
            ("asymmetry_percent", round(comparison.asymmetry_percent, 2)),
            ("threshold_percent", limit),
        ),
    )


def _evidence(comparison: FairnessComparison, limit: float) -> str:
    """Return the two-team, one-objective evidence line."""
    objective = comparison.objective_label.upper()
    line = (
        f"{_team_label(comparison.best_team)} -> {objective} "
        f"{_format_value(comparison.best_value, comparison.unit)} / "
        f"{_team_label(comparison.worst_team)} -> {objective} "
        f"{_format_value(comparison.worst_value, comparison.unit)} / "
        f"ASYMMETRY {comparison.asymmetry_percent:.1f}%"
    )
    return f"{line} (limit {limit:.0f}%)" if limit > 0 else line


def _format_value(value: float, unit: str) -> str:
    """Return a measurement with its unit, integers where counts are counted."""
    if unit == "s":
        return f"{value:.1f}s"
    if unit in ("m choke", "s mean"):
        return f"{value:.1f} {unit}"
    return f"{value:.0f} {unit}"


def _team_label(team: str) -> str:
    """Return a team's display name in report form."""
    upper = team.upper()
    return upper if upper.startswith("TEAM") else f"TEAM {upper}"


def _route_reach(config: MapwrightConfig) -> float:
    """Return how far off a route cover and high ground still count.

    A sidestep of a couple of body lengths: far enough to include the cover a
    player would actually use, close enough to exclude the next corridor.
    """
    return max(config.thresholds.chokepoint_width * 2.0, config.player.height * 2.0)


def _cover_objects(context: AnalysisContext) -> tuple[tuple[SceneObject, Bounds], ...]:
    """Return objects tall enough to hide behind and short enough to shoot over."""
    low = context.config.thresholds.cover_height_min
    high = context.config.thresholds.cover_height_max
    return tuple(
        (obj, box)
        for obj in context.scene.obstacles
        if (box := obj.world_bounds()) is not None and low <= box.height <= high
    )


def _high_ground_objects(
    context: AnalysisContext,
) -> tuple[tuple[SceneObject, Bounds], ...]:
    """Return surfaces a player can stand on above the surrounding ground."""
    delta = context.config.thresholds.high_ground_delta
    boxes = [
        box for obj in context.scene.props if (box := obj.world_bounds()) is not None
    ]
    ground = min((box.min.y for box in boxes), default=0.0)
    return tuple(
        (obj, box)
        for obj in context.scene.props
        if (box := obj.world_bounds()) is not None
        and box.max.y - ground >= delta
        and box.area_xz >= MINIMUM_STAND_AREA
    )


def _edges_by_pair(graph: RouteGraph) -> dict[tuple[str, str], RouteEdge]:
    """Return one representative route per connected pair of places.

    Where parallel routes exist, the shortest is the one a player takes, so it
    is the one whose width decides the approach.
    """
    chosen: dict[tuple[str, str], RouteEdge] = {}
    for edge in graph.edges:
        current = chosen.get(edge.key)
        if current is None or (edge.distance, -edge.width) < (
            current.distance,
            -current.width,
        ):
            chosen[edge.key] = edge
    return chosen


def _narrowest(path: Sequence[str], edges: dict[tuple[str, str], RouteEdge]) -> float:
    """Return the narrowest measured width along a route."""
    widths = [
        edge.width
        for first, second in zip(path, path[1:])
        if (edge := edges.get(_key(first, second))) is not None
    ]
    return min(widths) if widths else 0.0


def _route_samples(
    graph: RouteGraph,
    grid: OccupancyGrid,
    path: Sequence[str],
    edges: dict[tuple[str, str], RouteEdge],
) -> tuple[Vec3, ...]:
    """Return points along a route, following its walked cells where known."""
    samples: list[Vec3] = [graph.node(identifier).position for identifier in path]
    for first, second in zip(path, path[1:]):
        edge = edges.get(_key(first, second))
        if edge is None or not edge.cells:
            continue
        step = max(1, math.ceil(len(edge.cells) / MAXIMUM_EDGE_SAMPLES))
        samples.extend(
            grid.cell_center(column, row) for column, row in edge.cells[::step]
        )
    return tuple(samples)


def _key(first: str, second: str) -> tuple[str, str]:
    """Return the undirected identity of a route between two places."""
    return (first, second) if first <= second else (second, first)


def _count_near(
    candidates: Iterable[tuple[SceneObject, Bounds]],
    samples: Sequence[Vec3],
    reach: float,
) -> int:
    """Return how many objects lie within reach of any point on a route."""
    if not samples:
        return 0
    return sum(
        1
        for _, box in candidates
        if any(_distance_xz(box, point) <= reach for point in samples)
    )


def _distance_xz(box: Bounds, point: Vec3) -> float:
    """Return the horizontal distance from a point to a box."""
    dx = max(box.min.x - point.x, 0.0, point.x - box.max.x)
    dz = max(box.min.z - point.z, 0.0, point.z - box.max.z)
    return math.hypot(dx, dz)
