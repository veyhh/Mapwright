"""Intensity sequence: how a level's declared pressure rises and falls.

Pacing is the one design property geometry cannot state on its own. A zone
declares what it is for and how intense it should feel; this module reads
those declarations in order and measures the shape of the resulting curve —
its peaks, its plateaus, its jumps, and whether it ever lets go.

Everything here is measurement. Thresholds and findings live in
:mod:`mapwright.validators.pacing`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from mapwright.core.context import AnalysisContext
from mapwright.core.scene_ir import RELIEF_ZONE_TYPES, PacingLevel, ZoneType


#: Intensity at or above which a beat counts as sustained pressure.
HIGH_INTENSITY = PacingLevel.HIGH.intensity

#: Intensity at or below which a beat gives the player room to recover.
RELIEF_INTENSITY = PacingLevel.LOW.intensity


@dataclass(frozen=True)
class ZoneBeat:
    """One zone's place in the intended play sequence."""

    zone: str
    type: ZoneType
    pacing: PacingLevel
    intensity: int
    order: int

    @property
    def is_relief(self) -> bool:
        """Return whether this beat lets the player recover."""
        return self.type in RELIEF_ZONE_TYPES or self.intensity <= RELIEF_INTENSITY

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "zone": self.zone,
            "type": self.type.value,
            "pacing": self.pacing.value,
            "intensity": self.intensity,
            "order": self.order,
        }


@dataclass(frozen=True)
class IntensityRun:
    """An unbroken stretch of high or intense beats."""

    zones: tuple[str, ...]
    start_order: int
    end_order: int
    peak: int

    @property
    def length(self) -> int:
        """Return how many consecutive beats the stretch covers."""
        return len(self.zones)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "zones": list(self.zones),
            "start_order": self.start_order,
            "end_order": self.end_order,
            "length": self.length,
            "peak": self.peak,
        }


@dataclass(frozen=True)
class Transition:
    """The intensity step between two consecutive beats."""

    from_zone: str
    to_zone: str
    from_intensity: int
    to_intensity: int
    delta: int

    @property
    def escalates(self) -> bool:
        """Return whether the step raises pressure rather than releasing it."""
        return self.delta > 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "from_zone": self.from_zone,
            "to_zone": self.to_zone,
            "from_intensity": self.from_intensity,
            "to_intensity": self.to_intensity,
            "delta": self.delta,
        }


@dataclass(frozen=True)
class PacingAnalysis:
    """The measured shape of one level's intensity curve."""

    sequence: tuple[ZoneBeat, ...]
    intensity_curve: tuple[int, ...]
    runs: tuple[IntensityRun, ...]
    transitions: tuple[Transition, ...]
    peak: int
    mean_intensity: float
    variety: float
    relief_after_peak: bool
    longest_high_run: int

    @property
    def declared(self) -> bool:
        """Return whether the scene states any pacing intent at all."""
        return bool(self.sequence)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "sequence": [beat.to_dict() for beat in self.sequence],
            "intensity_curve": list(self.intensity_curve),
            "runs": [run.to_dict() for run in self.runs],
            "transitions": [step.to_dict() for step in self.transitions],
            "peak": self.peak,
            "mean_intensity": round(self.mean_intensity, 4),
            "variety": round(self.variety, 4),
            "relief_after_peak": self.relief_after_peak,
            "longest_high_run": self.longest_high_run,
        }


def analyze_pacing(context: AnalysisContext) -> PacingAnalysis:
    """Measure the intensity sequence a scene's zones declare.

    Zones are read in Scene IR declaration order, which Mapwright treats as the
    intended play order: an adapter writes zones in the order the designer
    means them to be experienced, because no geometry can imply that sequence.
    """
    sequence = tuple(
        ZoneBeat(
            zone=zone.name,
            type=zone.type,
            pacing=zone.pacing,
            intensity=zone.pacing.intensity,
            order=order,
        )
        for order, zone in enumerate(context.scene.zones)
    )
    curve = tuple(beat.intensity for beat in sequence)
    transitions = tuple(
        Transition(
            from_zone=first.zone,
            to_zone=second.zone,
            from_intensity=first.intensity,
            to_intensity=second.intensity,
            delta=second.intensity - first.intensity,
        )
        for first, second in zip(sequence, sequence[1:])
    )
    runs = _high_runs(sequence)
    peak = max(curve) if curve else 0
    return PacingAnalysis(
        sequence=sequence,
        intensity_curve=curve,
        runs=runs,
        transitions=transitions,
        peak=peak,
        mean_intensity=statistics.fmean(curve) if curve else 0.0,
        variety=_variety(transitions),
        relief_after_peak=_relief_after_peak(sequence, peak),
        longest_high_run=max((run.length for run in runs), default=0),
    )


def format_pacing(analysis: PacingAnalysis) -> str:
    """Render the intensity sequence as a terminal block with a curve."""
    if not analysis.declared:
        return (
            "Pacing measurements\n"
            "No zones are declared, so the scene states no pacing intent."
        )
    width = max(len(beat.zone) for beat in analysis.sequence)
    lines = ["Pacing measurements (zone declaration order = intended play order)"]
    for beat in analysis.sequence:
        bar = "#" * (beat.intensity + 1) + "." * (4 - beat.intensity)
        lines.append(
            f"  {beat.order + 1:>2}. {beat.zone:<{width}}  "
            f"{beat.type.value:<11} {beat.pacing.value:<8} [{bar}] {beat.intensity}"
        )
    lines.extend(
        [
            f"Peak intensity: {analysis.peak} | Mean: {analysis.mean_intensity:.2f} | "
            f"Variety: {analysis.variety:.2f} steps per transition",
            f"Longest high-intensity run: {analysis.longest_high_run} zone(s) | "
            f"Relief after the final peak: "
            f"{'yes' if analysis.relief_after_peak else 'no'}",
        ]
    )
    if analysis.runs:
        lines.append("High-intensity runs:")
        lines.extend(
            f"  - {' -> '.join(run.zones)} ({run.length} zone(s), peak {run.peak})"
            for run in analysis.runs
        )
    steps = [step for step in analysis.transitions if step.delta]
    if steps:
        lines.append("Transitions:")
        lines.extend(
            f"  - {step.from_zone} -> {step.to_zone}: "
            f"{step.from_intensity} to {step.to_intensity} ({step.delta:+d})"
            for step in steps
        )
    return "\n".join(lines)


def _high_runs(sequence: Sequence[ZoneBeat]) -> tuple[IntensityRun, ...]:
    runs: list[IntensityRun] = []
    current: list[ZoneBeat] = []

    def flush() -> None:
        if not current:
            return
        runs.append(
            IntensityRun(
                zones=tuple(item.zone for item in current),
                start_order=current[0].order,
                end_order=current[-1].order,
                peak=max(item.intensity for item in current),
            )
        )
        current.clear()

    for beat in sequence:
        if beat.intensity >= HIGH_INTENSITY:
            current.append(beat)
        else:
            flush()
    flush()
    return tuple(runs)


def _variety(transitions: Sequence[Transition]) -> float:
    """Return the mean absolute intensity step, so a flat level scores zero."""
    if not transitions:
        return 0.0
    return statistics.fmean(abs(step.delta) for step in transitions)


def _relief_after_peak(sequence: Sequence[ZoneBeat], peak: int) -> bool:
    """Return whether any recovery beat follows the last beat at peak intensity.

    The last peak is the one that matters: a level that relieves in the middle
    and then ends on its loudest note still never lets the player down.
    """
    if not sequence:
        return False
    last_peak = max(beat.order for beat in sequence if beat.intensity == peak)
    return any(
        beat.is_relief for beat in sequence if beat.order > last_peak
    )
