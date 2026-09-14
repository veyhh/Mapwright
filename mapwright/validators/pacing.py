"""Pacing findings: what the measured intensity sequence means for play.

Every number quoted here comes from
:func:`mapwright.design.pacing.analyze_pacing`. Pacing is judged against
declared intent only: a scene that states no zone intent is reported as
unjudged, never as well paced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mapwright.core.context import AnalysisContext
from mapwright.core.issues import Category, Issue, IssueCollector, Severity
from mapwright.design.pacing import (
    HIGH_INTENSITY,
    PacingAnalysis,
    analyze_pacing,
    format_pacing,
)


#: Mean intensity step below which a curve is flat rather than shaped. Half a
#: step per transition is the point at which the level never changes register;
#: no profile threshold covers curve movement yet.
FLAT_VARIETY_LIMIT = 0.5

#: Fewest beats needed before flatness is a judgement rather than an accident.
FLAT_MINIMUM_BEATS = 3


@dataclass(frozen=True)
class PacingReport:
    """Pacing findings for one scene, with the measurements behind them."""

    issues: tuple[Issue, ...]
    analysis: PacingAnalysis | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "analysis": None if self.analysis is None else self.analysis.to_dict(),
        }


def validate_pacing(context: AnalysisContext) -> PacingReport:
    """Judge one scene's intensity sequence against the active profile."""
    collector = IssueCollector(Category.PACING)
    analysis = analyze_pacing(context)
    if not analysis.declared:
        collector.add(
            "no_zone_intent",
            context.config.severity_for("no_zone_intent", Severity.INFO),
            "The scene states no pacing intent",
            f"'{context.scene.scene}' declares no zones, so it states no "
            "purpose or intensity for any part of the space. Pacing was not "
            "judged.",
            "Declare zones with a type and a pacing level — for example an "
            "entry marked calm, a combat room marked high, a relief room "
            "marked low — so the intended rhythm can be checked against the "
            "built space.",
            explanation=(
                "Pacing is intent, not geometry: no measurement of a room can "
                "tell whether it was meant to be tense or restful. Without "
                "declared zones there is nothing to compare the layout "
                "against."
            ),
            advisory=True,
        )
        return PacingReport(issues=collector.result(), analysis=analysis)

    _check_runs(collector, context, analysis)
    _check_relief(collector, context, analysis)
    _check_transitions(collector, context, analysis)
    _check_flatness(collector, context, analysis)
    return PacingReport(issues=collector.result(), analysis=analysis)


def format_report(report: PacingReport) -> str:
    """Render pacing measurements and findings as a terminal report."""
    lines = ["Pacing", "======"]
    if report.analysis is None:
        lines.append("Pacing could not be measured for this scene.")
    else:
        lines.append(format_pacing(report.analysis))
    lines.append("")
    if not report.issues:
        lines.append("No pacing findings.")
        return "\n".join(lines)
    lines.append(f"Findings ({len(report.issues)}):")
    for issue in report.issues:
        lines.append("")
        lines.append(issue.to_text())
    return "\n".join(lines)


def _check_runs(
    collector: IssueCollector, context: AnalysisContext, analysis: PacingAnalysis
) -> None:
    limit = context.config.thresholds.max_consecutive_high
    for run in analysis.runs:
        if run.length <= limit:
            continue
        names = " -> ".join(run.zones)
        collector.add(
            "consecutive_high_intensity",
            context.config.severity_for(
                "consecutive_high_intensity", Severity.WARNING
            ),
            f"{run.length} high-intensity zones run back to back",
            f"{names} run consecutively at intensity {run.peak} or above "
            f"(positions {run.start_order + 1}-{run.end_order + 1} of "
            f"{len(analysis.sequence)}), past the {limit:.0f} consecutive "
            "high zones this profile allows.",
            f"Insert a low or calm zone between {run.zones[0]} and "
            f"{run.zones[-1]} — a corridor, a looted room, a view over what "
            "comes next — so the player can reload, re-orient, and feel the "
            "next peak.",
            explanation=(
                "Sustained pressure stops reading as pressure. Without a "
                "trough between peaks the player adapts to the intensity and "
                "the climax lands no harder than the approach."
            ),
            zone=run.zones[0],
            subjects=run.zones,
            metrics=(
                ("run_length", float(run.length)),
                ("limit", limit),
                ("peak", float(run.peak)),
            ),
        )


def _check_relief(
    collector: IssueCollector, context: AnalysisContext, analysis: PacingAnalysis
) -> None:
    if analysis.peak < HIGH_INTENSITY or analysis.relief_after_peak:
        return
    last_peak = [
        beat for beat in analysis.sequence if beat.intensity == analysis.peak
    ][-1]
    trailing = [beat for beat in analysis.sequence if beat.order > last_peak.order]
    if trailing:
        lowest = min(beat.intensity for beat in trailing)
        tail = (
            f"The zones after it ({', '.join(beat.zone for beat in trailing)}) "
            f"never drop below intensity {lowest}."
        )
    else:
        tail = "It is the last zone in the sequence."
    collector.add(
        "missing_relief",
        context.config.severity_for("missing_relief", Severity.WARNING),
        "The level never lets go after its peak",
        f"{last_peak.zone} peaks at intensity {analysis.peak} "
        f"({last_peak.pacing.value}) at position {last_peak.order + 1} of "
        f"{len(analysis.sequence)}. {tail}",
        f"Add a relief beat after {last_peak.zone} — a calm or low zone, or a "
        "relief-typed space such as a safe room or an exit approach — so the "
        "peak has an aftermath.",
        explanation=(
            "A peak is only felt against what follows it. Ending on maximum "
            "intensity denies the player the moment where the fight is over "
            "and the level lands."
        ),
        zone=last_peak.zone,
        subjects=(last_peak.zone,),
        metrics=(
            ("peak", float(analysis.peak)),
            ("peak_position", float(last_peak.order + 1)),
            ("beats", float(len(analysis.sequence))),
        ),
    )


def _check_transitions(
    collector: IssueCollector, context: AnalysisContext, analysis: PacingAnalysis
) -> None:
    limit = context.config.thresholds.abrupt_transition_delta
    for step in analysis.transitions:
        if abs(step.delta) < limit:
            continue
        if step.escalates:
            reason = (
                "A jump this size skips the build-up. The player walks out of "
                "quiet directly into the loudest part of the level with no "
                "warning to read and no chance to prepare."
            )
            fix = (
                f"Ramp the step: raise {step.from_zone} a level, lower "
                f"{step.to_zone} a level, or place a tension zone between them "
                "that telegraphs what is coming"
            )
        else:
            reason = (
                "Dropping this far in one step dumps the tension rather than "
                "releasing it. The fight ends and the level goes quiet before "
                "the player has registered that it is over."
            )
            fix = (
                f"Let the pressure down in stages: give {step.to_zone} a "
                "medium or low beat, or place a short cool-down zone between "
                f"{step.from_zone} and {step.to_zone}"
            )
        collector.add(
            "abrupt_transition",
            # A skipped intensity step is worth noticing, not correcting: a
            # calm arrival opening onto a medium space clears this bar too.
            context.config.severity_for("abrupt_transition", Severity.INFO),
            f"Abrupt pacing step from {step.from_zone} to {step.to_zone}",
            f"{step.from_zone} ({step.from_intensity}) to {step.to_zone} "
            f"({step.to_intensity}) changes intensity by {step.delta:+d} in one "
            f"beat, at or past the {limit:.0f}-step change this profile treats "
            "as abrupt.",
            f"{fix}.",
            explanation=reason,
            zone=step.to_zone,
            subjects=(step.from_zone, step.to_zone),
            metrics=(
                ("delta", float(step.delta)),
                ("limit", limit),
                ("from_intensity", float(step.from_intensity)),
                ("to_intensity", float(step.to_intensity)),
            ),
        )


def _check_flatness(
    collector: IssueCollector, context: AnalysisContext, analysis: PacingAnalysis
) -> None:
    if len(analysis.sequence) < FLAT_MINIMUM_BEATS:
        return
    if analysis.variety >= FLAT_VARIETY_LIMIT:
        return
    levels = ", ".join(
        f"{beat.zone} ({beat.pacing.value})" for beat in analysis.sequence
    )
    collector.add(
        "flat_pacing",
        context.config.severity_for("flat_pacing", Severity.WARNING),
        "The intensity curve never moves",
        f"Across {len(analysis.sequence)} zones the curve moves "
        f"{analysis.variety:.2f} intensity steps per transition on average "
        f"(below {FLAT_VARIETY_LIMIT:.2f}), peaking at {analysis.peak} with a "
        f"mean of {analysis.mean_intensity:.1f}: {levels}.",
        "Shape the sequence: raise one zone to a clear peak and drop the zone "
        "before or after it, so the level has somewhere to build to and "
        "somewhere to recover.",
        explanation=(
            "A level held at one intensity has no shape. Whether it is flat "
            "calm or flat loud, the player stops registering the level as "
            "events and starts reading it as one long corridor."
        ),
        subjects=tuple(beat.zone for beat in analysis.sequence),
        metrics=(
            ("variety", round(analysis.variety, 3)),
            ("limit", FLAT_VARIETY_LIMIT),
            ("peak", float(analysis.peak)),
            ("mean_intensity", round(analysis.mean_intensity, 3)),
        ),
    )
