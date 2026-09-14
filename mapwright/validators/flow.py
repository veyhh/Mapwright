"""Flow findings: what the measured traversal structure means for play.

This module owns the opinions. Every number it quotes comes from
:func:`mapwright.design.flow.analyze_flow`; nothing here re-measures the
level, and every threshold comes from the active profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mapwright.core.context import AnalysisContext
from mapwright.core.graph import RouteGraph, RouteNode
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.design.flow import Chokepoint, FlowAnalysis, analyze_flow, format_flow


#: Share of journeys that may re-tread earlier ground before it reads as a loop
#: problem. Half the journey set is the point at which a return trip is the
#: rule rather than the exception; no profile threshold covers this yet.
BACKTRACKING_LIMIT = 0.5

#: Wording appended to findings drawn from an inferred topology.
DERIVED_NOTE = (
    "The scene declared too few places, so this topology was inferred from "
    "sampled waypoints rather than declared structure"
)


@dataclass(frozen=True)
class FlowReport:
    """Flow findings for one scene, with the measurements behind them."""

    issues: tuple[Issue, ...]
    analysis: FlowAnalysis | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "analysis": None if self.analysis is None else self.analysis.to_dict(),
        }


def validate_flow(context: AnalysisContext) -> FlowReport:
    """Judge one scene's traversal structure against the active profile."""
    collector = IssueCollector(Category.FLOW)
    analysis = analyze_flow(context)
    graph = context.graph
    if analysis is None or graph is None:
        collector.add(
            "no_critical_path",
            context.config.severity_for("no_critical_path", Severity.INFO),
            "Traversal structure could not be measured",
            f"No route graph could be derived for '{context.scene.scene}': "
            f"{context.graph_reason.rstrip('.')}.",
            "Declare ground geometry and at least one entry and one objective "
            "marker, then re-run the analysis so flow can be judged instead of "
            "skipped.",
            explanation=(
                "Flow describes where a player can go. With no walkable "
                "topology there is nothing to measure, so this scene is "
                "unjudged rather than passing."
            ),
            advisory=True,
        )
        return FlowReport(issues=collector.result(), analysis=None)

    advisory = analysis.derived
    _check_critical_path(collector, context, analysis, graph, advisory)
    _check_concentration(collector, context, analysis, graph, advisory)
    _check_route_diversity(collector, context, analysis, graph, advisory)
    _check_chokepoints(collector, context, analysis, graph, advisory)
    _check_dead_ends(collector, context, analysis, advisory)
    _check_isolated(collector, context, analysis, graph, advisory)
    _check_backtracking(collector, context, analysis, graph, advisory)
    return FlowReport(issues=collector.result(), analysis=analysis)


def format_report(report: FlowReport) -> str:
    """Render flow measurements and findings as a terminal report."""
    lines = ["Flow", "===="]
    if report.analysis is None:
        lines.append("Flow could not be measured for this scene.")
    else:
        lines.append(format_flow(report.analysis))
    lines.append("")
    if not report.issues:
        lines.append("No flow findings.")
        return "\n".join(lines)
    lines.append(f"Findings ({len(report.issues)}):")
    for issue in report.issues:
        lines.append("")
        lines.append(issue.to_text())
        if issue.advisory:
            lines.append("(advisory finding)")
    return "\n".join(lines)


def _place_name(node: RouteNode) -> str:
    """Return a place name a designer can find, locating sampled waypoints."""
    if not node.derived:
        return node.label
    return f"{node.label} (x {node.position.x:.1f}, z {node.position.z:.1f})"


def _label(graph: RouteGraph, node_id: str) -> str:
    return _place_name(graph.node(node_id))


def _evidence(text: str, advisory: bool) -> str:
    return f"{text} {DERIVED_NOTE}." if advisory else text


def _check_critical_path(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    advisory: bool,
) -> None:
    if analysis.critical_path:
        return
    places = ", ".join(
        node.label for node in graph.nodes if node.is_semantic
    ) or "none"
    collector.add(
        "no_critical_path",
        context.config.severity_for("no_critical_path", Severity.ERROR),
        "No walkable route joins the declared journey",
        _evidence(
            f"'{context.scene.scene}' declares the gameplay places {places}, but "
            f"no walkable route connects them ({analysis.critical_path_basis}). "
            f"The level graph holds {len(graph.nodes)} place(s) in "
            f"{len(graph.components())} disconnected group(s).",
            advisory,
        ),
        "Connect the declared places with walkable ground — remove the "
        "blocking geometry between them or add a corridor — so the intended "
        "journey can be walked end to end.",
        explanation=(
            "The critical path is the route the level exists to deliver. If it "
            "cannot be walked, every other flow measurement describes a space "
            "the player cannot complete."
        ),
        subjects=tuple(node.id for node in graph.nodes if node.is_semantic),
        metrics=(
            ("places", float(len(graph.nodes))),
            ("components", float(len(graph.components()))),
        ),
        advisory=advisory,
    )


def _check_concentration(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    advisory: bool,
) -> None:
    limit = context.config.thresholds.traversal_concentration
    edge = analysis.busiest_edge
    if edge is None or analysis.traversal_concentration <= limit:
        return
    source, target = _label(graph, edge.source), _label(graph, edge.target)
    collector.add(
        "traversal_concentration",
        context.config.severity_for("traversal_concentration", Severity.WARNING),
        f"Traversal concentrates on {source} -> {target}",
        _evidence(
            f"{analysis.traversal_concentration:.0f}% of critical traversal "
            f"passes through {source} -> {target}, measured across "
            f"{analysis.routed_journeys} declared journey(s); this profile "
            f"allows {limit:.0f}%.",
            advisory,
        ),
        f"Create an alternate path between {source} and {target} — a flank "
        "corridor, an upper walkway, or a second door — so the traffic can "
        "split instead of funnelling.",
        explanation=(
            "This creates excessive route concentration. Players meet the same "
            "ground on every journey, encounters staged elsewhere are never "
            "seen, and one blocked or camped route stops the whole level."
        ),
        subjects=(edge.source, edge.target),
        metrics=(
            ("traversal_concentration", round(analysis.traversal_concentration, 2)),
            ("limit", limit),
            ("routed_journeys", float(analysis.routed_journeys)),
        ),
        advisory=advisory,
    )


def _check_route_diversity(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    advisory: bool,
) -> None:
    minimum = context.config.thresholds.minimum_route_diversity
    if analysis.routed_journeys == 0 or analysis.route_diversity >= minimum:
        return
    if len(analysis.critical_path) >= 2:
        first = _label(graph, analysis.critical_path[0])
        last = _label(graph, analysis.critical_path[-1])
    else:
        first, last = "the entry", "the objective"
    collector.add(
        "low_route_diversity",
        context.config.severity_for("low_route_diversity", Severity.WARNING),
        "The level offers only one way through",
        _evidence(
            f"The least-served of {analysis.routed_journeys} declared journey(s) "
            f"has {analysis.route_diversity} independent route(s); this profile "
            f"expects {minimum:.0f}. The level contains "
            f"{analysis.loops} loop(s) and "
            f"{analysis.optional_routes} optional route(s) beside the critical "
            f"path {first} -> {last}.",
            advisory,
        ),
        f"Add a second route between {first} and {last} that shares no segment "
        "with the first — a parallel corridor, a drop-down shortcut, or a "
        "vertical bypass — so the player has a choice and a fallback.",
        explanation=(
            "With a single independent route every player moves identically, "
            "there is no approach to choose and no recovery when the route is "
            "blocked, contested, or already explored."
        ),
        metrics=(
            ("route_diversity", float(analysis.route_diversity)),
            ("minimum", minimum),
            ("loops", float(analysis.loops)),
        ),
        advisory=advisory,
    )


def _check_chokepoints(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    advisory: bool,
) -> None:
    width_limit = context.config.thresholds.chokepoint_width
    reportable = analysis.chokepoints
    if advisory:
        reportable = analysis.structural_chokepoints
        _report_inferred_widths(collector, context, analysis, graph, width_limit)
    for point in reportable:
        edge = point.edge
        source, target = _label(graph, edge.source), _label(graph, edge.target)
        default = (
            Severity.ERROR if point.structural and point.narrow else Severity.WARNING
        )
        if point.structural:
            measurement = (
                f"{source} -> {target} is the only route between two halves of "
                f"the level ({point.split[0]} place(s) on one side, "
                f"{point.split[1]} on the other). It is {edge.width:.2f} m wide "
                f"and carries {point.load * 100:.0f}% of routed journeys."
            )
            reason = (
                "Every crossing between those two sides uses this one route. "
                "Blocking, camping, or mis-reading it stops the level rather "
                "than slowing it."
            )
            fix = (
                f"Add a second connection between the {source} side and the "
                f"{target} side so the split is a choice rather than a "
                "single point of failure"
            )
        else:
            measurement = (
                f"{source} -> {target} narrows to {edge.width:.2f} m, below the "
                f"{width_limit:.2f} m this profile treats as passable flow, and "
                f"carries {point.load * 100:.0f}% of routed journeys."
            )
            reason = (
                "A passage this narrow forces players into single file, snags "
                "movement against the geometry, and turns any traffic into a "
                "queue."
            )
            fix = (
                f"Widen {source} -> {target} past {width_limit:.2f} m, or move "
                "the geometry pinching it aside"
            )
        if point.narrow and point.structural:
            fix += f", and widen it past {width_limit:.2f} m"
        collector.add(
            "chokepoint",
            context.config.severity_for("chokepoint", default),
            f"Chokepoint on {source} -> {target}",
            _evidence(measurement, advisory),
            f"{fix}.",
            explanation=reason,
            subjects=(edge.source, edge.target),
            metrics=(
                ("width", round(edge.width, 3)),
                ("load", round(point.load, 4)),
                ("side_a", float(point.split[0])),
                ("side_b", float(point.split[1])),
            ),
            advisory=advisory,
        )


def _report_inferred_widths(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    width_limit: float,
) -> None:
    """Report narrow inferred routes once, rather than as a finding per waypoint.

    Between sampled waypoints a width measures wherever the sampler happened
    to land, so naming each one separately would invite a designer to widen
    passages that were never designed.
    """
    narrow: list[Chokepoint] = [
        point
        for point in analysis.chokepoints
        if point.narrow and not point.structural
    ]
    if not narrow:
        return
    tightest = min(narrow, key=lambda point: (point.edge.width, point.key))
    source = _label(graph, tightest.edge.source)
    target = _label(graph, tightest.edge.target)
    collector.add(
        "chokepoint",
        context.config.severity_for("chokepoint", Severity.INFO),
        f"{len(narrow)} inferred routes are narrower than {width_limit:.2f} m",
        _evidence(
            f"{len(narrow)} of the {len(analysis.edge_loads)} inferred route(s) "
            f"measure under {width_limit:.2f} m; the tightest is {source} -> "
            f"{target} at {tightest.edge.width:.2f} m, carrying "
            f"{tightest.load * 100:.0f}% of routed journeys.",
            True,
        ),
        "Declare this level's entries, objectives, exits and zones, then "
        "re-run so widths are measured along the routes players take; or "
        f"inspect the tight space around {source} directly and widen it if it "
        "is meant to be a passage.",
        explanation=(
            "Narrow space between sampled waypoints may be a corridor, the gap "
            "behind a prop, or the edge of the playable area. Until the level "
            "names its places, these widths cannot be attributed to routes."
        ),
        metrics=(
            ("narrow_routes", float(len(narrow))),
            ("narrowest_width", round(tightest.edge.width, 3)),
            ("limit", width_limit),
        ),
        advisory=True,
    )


def _check_dead_ends(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    advisory: bool,
) -> None:
    limit = context.config.thresholds.dead_end_ratio
    ratio = analysis.dead_end_ratio
    if not analysis.dead_ends or ratio <= limit:
        return
    names = ", ".join(_place_name(node) for node in analysis.dead_ends)
    first = _place_name(analysis.dead_ends[0])
    collector.add(
        "dead_end",
        context.config.severity_for("dead_end", Severity.WARNING),
        "Too much of the level ends in dead ends",
        _evidence(
            f"{len(analysis.dead_ends)} of the level's structural places are "
            f"dead ends ({ratio:.0f}%, above the {limit:.0f}% this profile "
            f"allows): {names}.",
            advisory,
        ),
        f"Connect {first} back into the level to close a loop, or give it a "
        "reward — a vantage point, a resource, a story beat — that pays for "
        "the walk back.",
        explanation=(
            "A dead end spends the player's time and returns them to where "
            "they started with nothing learned. A few are deliberate rewards; "
            "this many read as unfinished layout."
        ),
        subjects=tuple(node.id for node in analysis.dead_ends),
        metrics=(
            ("dead_ends", float(len(analysis.dead_ends))),
            ("dead_end_ratio", round(ratio, 2)),
            ("limit", limit),
        ),
        advisory=advisory,
    )


def _check_isolated(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    advisory: bool,
) -> None:
    if not analysis.isolated:
        return
    main = graph.components()[0]
    for node in analysis.isolated:
        name = _place_name(node)
        collector.add(
            "isolated_route_node",
            context.config.severity_for("isolated_route_node", Severity.ERROR),
            f"{name} is cut off from the level",
            _evidence(
                f"{name} has no walkable route to the main body of the "
                f"level, which holds {len(main)} of {len(graph.nodes)} place(s). "
                f"It has {graph.degree(node.id)} route(s) of its own.",
                advisory,
            ),
            f"Connect {name} to the nearest reachable place, or remove it "
            "if the space behind it is not meant to be played.",
            explanation=(
                "A place with no route to the rest of the level cannot be "
                "reached in play. Whatever it holds — an objective, cover, a "
                "landmark — is invisible to the player."
            ),
            subjects=(node.id,),
            zone=node.zone,
            metrics=(
                ("degree", float(graph.degree(node.id))),
                ("main_component", float(len(main))),
            ),
            advisory=advisory,
        )


def _check_backtracking(
    collector: IssueCollector,
    context: AnalysisContext,
    analysis: FlowAnalysis,
    graph: RouteGraph,
    advisory: bool,
) -> None:
    if analysis.routed_journeys == 0:
        return
    if analysis.backtracking_ratio <= BACKTRACKING_LIMIT:
        return
    if len(analysis.critical_path) >= 2:
        first = _label(graph, analysis.critical_path[0])
        last = _label(graph, analysis.critical_path[-1])
    else:
        first, last = "the start", "the far end"
    fix = (
        f"Close a loop between {first} and {last} — a return corridor or a "
        "one-way drop back to the start — so the journey out and the journey "
        "back cover different ground."
    )
    if advisory:
        fix = (
            "Declare the level's entry, objective and exit so re-treading is "
            "measured against the intended journey rather than every pair of "
            f"sampled places; the repeated ground lies between {first} and "
            f"{last}."
        )
    collector.add(
        "backtracking_risk",
        context.config.severity_for("backtracking_risk", Severity.INFO),
        "Journeys re-tread ground they have already covered",
        _evidence(
            f"{analysis.backtracking_ratio * 100:.0f}% of the "
            f"{analysis.routed_journeys} routed journey(s) re-use a route an "
            f"earlier journey already walked, above the "
            f"{BACKTRACKING_LIMIT * 100:.0f}% mark. The level offers "
            f"{analysis.loops} loop(s).",
            advisory,
        ),
        fix,
        explanation=(
            "Re-treading is cheap to build and expensive to play: the second "
            "pass through a space has no surprises left, and the level feels "
            "smaller than it is."
        ),
        metrics=(
            ("backtracking_ratio", round(analysis.backtracking_ratio, 4)),
            ("limit", BACKTRACKING_LIMIT),
            ("loops", float(analysis.loops)),
        ),
        advisory=advisory,
    )
