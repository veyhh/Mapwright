"""Encounter findings: whether each fight in a level has more than one answer.

Every number quoted here comes from
:func:`mapwright.design.encounters.analyze_encounters`. Findings that depend
on the route graph are withheld when no graph exists, rather than reported as
zeroes a designer would read as a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mapwright.core.context import AnalysisContext
from mapwright.core.graph import NodeKind
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.design.encounters import (
    EncounterAnalysis,
    EncounterSpace,
    analyze_encounters,
    format_encounters,
)


#: Approach directions closer together than this share one firing line, so the
#: encounter can only be met head-on however many doors it has.
FRONTAL_APPROACH_SPREAD = 90.0

#: Share of open floor at which a zone without flanks reads as a shooting gallery.
FRONTAL_EXPOSURE_SHARE = 0.5

#: Wording appended to findings drawn from an inferred topology.
DERIVED_NOTE = (
    "The scene declared too few places, so these routes were inferred from "
    "sampled waypoints rather than declared structure"
)


@dataclass(frozen=True)
class EncounterReport:
    """Encounter findings for one scene, with the measurements behind them."""

    issues: tuple[Issue, ...]
    analysis: EncounterAnalysis | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "analysis": None if self.analysis is None else self.analysis.to_dict(),
        }


def validate_encounter(context: AnalysisContext) -> EncounterReport:
    """Judge every declared combat space against the active profile."""
    collector = IssueCollector(Category.ENCOUNTER)
    analysis = analyze_encounters(context)
    graph = context.graph
    routed = graph is not None
    advisory = routed and graph.derived
    has_start = routed and bool(
        graph.nodes_of_kind(NodeKind.ENTRY, NodeKind.SPAWN)
    )
    for space in analysis.zones:
        if routed:
            _check_entrances(collector, context, space, advisory)
            _check_flanking(collector, context, space, advisory)
            _check_frontal(collector, context, space, advisory)
            if has_start:
                _check_retreat(collector, context, space, advisory)
        _check_high_ground(collector, context, space)
        _check_cover(collector, context, space)
    return EncounterReport(issues=collector.result(), analysis=analysis)


def format_report(report: EncounterReport) -> str:
    """Render encounter measurements and findings as a terminal report."""
    lines = ["Encounter", "========="]
    if report.analysis is None:
        lines.append("Encounter space could not be measured for this scene.")
    else:
        lines.append(format_encounters(report.analysis))
    lines.append("")
    if not report.issues:
        lines.append("No encounter findings.")
        return "\n".join(lines)
    lines.append(f"Findings ({len(report.issues)}):")
    for issue in report.issues:
        lines.append("")
        lines.append(issue.to_text())
        if issue.advisory:
            lines.append("(advisory: measured from an inferred topology)")
    return "\n".join(lines)


def _evidence(text: str, advisory: bool) -> str:
    return f"{text} {DERIVED_NOTE}." if advisory else text


def _approaches(space: EncounterSpace) -> str:
    if not space.spawn_candidates:
        return "no approach point"
    return ", ".join(space.spawn_candidates)


def _check_entrances(
    collector: IssueCollector,
    context: AnalysisContext,
    space: EncounterSpace,
    advisory: bool,
) -> None:
    minimum = context.config.thresholds.minimum_encounter_entrances
    if space.entrances >= minimum:
        return
    collector.add(
        "single_entrance_encounter",
        context.config.severity_for("single_entrance_encounter", Severity.WARNING),
        f"{space.zone} can only be entered one way",
        _evidence(
            f"{space.zone} has {space.entrances} route(s) crossing its "
            f"boundary, below the {minimum:.0f} this profile expects for a "
            f"combat space. The only approach is via {_approaches(space)}, "
            f"across {space.engagement_distance:.1f} m of engagement space.",
            advisory,
        ),
        f"Cut a second way into {space.zone} — a side door, a balcony, a "
        "breakable wall, or a drop from above — so the player can choose how "
        "to open the fight and is not trapped once it starts.",
        explanation=(
            "One entrance means one plan. The defender knows where the fight "
            "starts, the attacker has no approach to choose, and a player who "
            "is losing has the same door to leave by that reinforcements use."
        ),
        zone=space.zone,
        metrics=(
            ("entrances", float(space.entrances)),
            ("minimum", minimum),
            ("exits", float(space.exits)),
        ),
        advisory=advisory,
    )


def _check_flanking(
    collector: IssueCollector,
    context: AnalysisContext,
    space: EncounterSpace,
    advisory: bool,
) -> None:
    minimum = context.config.thresholds.minimum_flank_routes
    if space.flank_routes >= minimum:
        return
    collector.add(
        "no_flank_route",
        context.config.severity_for("no_flank_route", Severity.WARNING),
        f"{space.zone} cannot be flanked",
        _evidence(
            f"{space.zone} offers {space.flank_routes} independent approach "
            f"beyond the direct one, below the {minimum:.0f} this profile "
            f"expects. Every route from the nearest entry or spawn shares a "
            f"segment with the shortest one; the zone has {space.entrances} "
            "boundary crossing(s) in total.",
            advisory,
        ),
        f"Add a route to {space.zone} that shares no segment with the main "
        "approach — a parallel corridor, a rooftop, a service tunnel — so the "
        "fight can be opened from a second direction.",
        explanation=(
            "Without an independent approach the encounter has one solution. "
            "Whoever holds the direct route decides the fight, and a player "
            "who fails there can only repeat the same attempt."
        ),
        zone=space.zone,
        metrics=(
            ("flank_routes", float(space.flank_routes)),
            ("minimum", minimum),
            ("entrances", float(space.entrances)),
        ),
        advisory=advisory,
    )


def _check_frontal(
    collector: IssueCollector,
    context: AnalysisContext,
    space: EncounterSpace,
    advisory: bool,
) -> None:
    if space.entrances < 2:
        return
    crowded = space.approach_spread < FRONTAL_APPROACH_SPREAD
    exposed = space.flank_routes == 0 and space.exposure >= FRONTAL_EXPOSURE_SHARE
    if not crowded and not exposed:
        return
    if crowded:
        measurement = (
            f"{space.zone} has {space.entrances} ways in, but they arrive "
            f"within {space.approach_spread:.0f} degrees of each other "
            f"(under {FRONTAL_APPROACH_SPREAD:.0f}), all from the same side: "
            f"{_approaches(space)}."
        )
    else:
        measurement = (
            f"{space.zone} has {space.entrances} ways in and no independent "
            f"flank, and {space.exposure * 100:.0f}% of its floor has open "
            f"sightlines past half its {space.engagement_distance:.1f} m "
            "engagement distance."
        )
    collector.add(
        "frontal_only_engagement",
        context.config.severity_for("frontal_only_engagement", Severity.WARNING),
        f"{space.zone} can only be fought head-on",
        _evidence(measurement, advisory),
        f"Move one approach to the far side of {space.zone}, or open a route "
        "that arrives behind the defending position, so attacking is a "
        "question of direction rather than of timing alone.",
        explanation=(
            "Several doors on the same wall are one door. The defender faces "
            "a single arc, the attacker crosses the same open ground whichever "
            "entrance is used, and the fight has one shape every time."
        ),
        zone=space.zone,
        metrics=(
            ("approach_spread", round(space.approach_spread, 2)),
            ("entrances", float(space.entrances)),
            ("exposure", round(space.exposure, 4)),
        ),
        advisory=advisory,
    )


def _check_retreat(
    collector: IssueCollector,
    context: AnalysisContext,
    space: EncounterSpace,
    advisory: bool,
) -> None:
    if space.retreat_routes > 0:
        return
    collector.add(
        "no_retreat_route",
        context.config.severity_for("no_retreat_route", Severity.WARNING),
        f"{space.zone} has no way back",
        _evidence(
            f"None of {space.zone}'s {space.entrances} boundary crossing(s) "
            "leads back to an entry or spawn without passing through the zone "
            f"again; {space.exits} lead onward.",
            advisory,
        ),
        f"Connect {space.zone} back toward the entry side — reopen the door "
        "the player came in by, or add a shortcut back — so withdrawing is a "
        "tactical choice rather than a reload.",
        explanation=(
            "A fight with no exit backwards can only be won or restarted. "
            "Retreat is what lets a player recover, re-plan, and come back at "
            "the encounter differently."
        ),
        zone=space.zone,
        metrics=(
            ("retreat_routes", float(space.retreat_routes)),
            ("entrances", float(space.entrances)),
            ("exits", float(space.exits)),
        ),
        advisory=advisory,
    )


def _check_high_ground(
    collector: IssueCollector, context: AnalysisContext, space: EncounterSpace
) -> None:
    if space.high_ground:
        return
    required = context.config.thresholds.high_ground_delta
    collector.add(
        "no_high_ground",
        context.config.severity_for("no_high_ground", Severity.INFO),
        f"{space.zone} is flat",
        f"{space.zone} offers no standable surface at least {required:.2f} m "
        f"above its floor across {space.engagement_distance:.1f} m of "
        f"engagement space, with {space.cover_count} cover object(s) on one "
        "level.",
        f"Add a raised position to {space.zone} — a walkway, a ledge, a stack "
        "of crates wide enough to stand on — reachable by at least one "
        "approach, so position is worth contesting.",
        explanation=(
            "Height is the cheapest way to make a room readable and worth "
            "fighting over: it gives sightlines to earn, a reason to move, "
            "and a way to see the encounter before entering it."
        ),
        zone=space.zone,
        metrics=(
            ("high_ground_delta", round(space.high_ground_delta, 3)),
            ("required_delta", required),
            ("engagement_distance", round(space.engagement_distance, 3)),
        ),
    )


def _check_cover(
    collector: IssueCollector, context: AnalysisContext, space: EncounterSpace
) -> None:
    if space.cover_distribution == "good":
        return
    default = (
        Severity.INFO if space.cover_distribution == "uneven" else Severity.WARNING
    )
    if space.cover_distribution == "none":
        measurement = (
            f"{space.zone} contains no cover between "
            f"{context.config.thresholds.cover_height_min:.2f} m and "
            f"{context.config.thresholds.cover_height_max:.2f} m tall, across "
            f"{space.engagement_distance:.1f} m of engagement space with "
            f"{space.exposure * 100:.0f}% of its floor in the open."
        )
        fix = (
            f"Place cover across {space.zone} in all four quadrants — chest-"
            "high blocks, low walls, or wreckage — so there is somewhere to "
            "break line of sight wherever the fight moves"
        )
    elif space.cover_distribution == "poor":
        measurement = (
            f"{space.zone}'s {space.cover_count} cover object(s) sit in a "
            "single quadrant of the zone, leaving the rest of the floor open "
            f"across {space.engagement_distance:.1f} m of engagement space."
        )
        fix = (
            f"Spread {space.zone}'s cover into the other quadrants, or add "
            "cover there, so advancing does not mean crossing open ground"
        )
    else:
        measurement = (
            f"{space.zone}'s {space.cover_count} cover object(s) are spread "
            "unevenly: they do not reach every quadrant, or more than half "
            "of them sit in one."
        )
        fix = (
            f"Redistribute {space.zone}'s cover so each quadrant holds some "
            "and no quadrant holds more than half"
        )
    collector.add(
        "poor_cover_distribution",
        context.config.severity_for("poor_cover_distribution", default),
        f"Cover in {space.zone} is {space.cover_distribution}",
        measurement,
        f"{fix}.",
        explanation=(
            "Cover is what turns a room into a fight with moves in it. Where "
            "cover is missing or bunched, players either stand still behind "
            "the one safe object or cross the open ground and die to it."
        ),
        zone=space.zone,
        metrics=(
            ("cover_count", float(space.cover_count)),
            ("exposure", round(space.exposure, 4)),
            ("engagement_distance", round(space.engagement_distance, 3)),
        ),
    )
