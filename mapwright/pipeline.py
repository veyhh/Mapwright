"""The analysis and improvement pipeline.

This is the layer that runs every check against one scene, scores the result,
and — when asked — corrects the scene and measures again. It is the only place
that knows the full set of analyzers, which keeps each analyzer independent
and keeps the CLI thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from mapwright.core.config import MapwrightConfig
from mapwright.core.context import AnalysisContext
from mapwright.core.issues import Category, Issue, count_by_severity, sort_issues
from mapwright.core.scene_ir import COMBAT_ZONE_TYPES, SceneIR
from mapwright.core.scoring import Scorecard, compare, score_issues
from mapwright.design.correction import (
    CorrectionPlan,
    apply_corrections,
    plan_corrections,
)
from mapwright.ecosystem.asset_director import SceneAssetProvider
from mapwright.review import visual as visual_review
from mapwright.validators import density as density_validator
from mapwright.validators import encounter as encounter_validator
from mapwright.validators import fairness as fairness_validator
from mapwright.validators import flow as flow_validator
from mapwright.validators import landmarks as landmark_validator
from mapwright.validators import navigation as navigation_validator
from mapwright.validators import pacing as pacing_validator
from mapwright.validators import repetition as repetition_validator
from mapwright.validators import sightlines as sightline_validator
from mapwright.validators import spacing as spacing_validator


#: Analyzer name to the callable that runs it, in report order.
ANALYZERS = (
    ("repetition", repetition_validator.analyze_repetition),
    ("spacing", spacing_validator.analyze_spacing),
    ("density", density_validator.analyze_density),
    ("landmarks", landmark_validator.analyze_landmarks),
    ("navigation", navigation_validator.analyze_navigation),
    ("sightlines", sightline_validator.analyze_sightlines),
    ("flow", flow_validator.validate_flow),
    ("pacing", pacing_validator.validate_pacing),
    ("encounter", encounter_validator.validate_encounter),
    ("fairness", fairness_validator.validate_fairness),
    ("visual", visual_review.review_visual),
)

#: How each analyzer's terminal output is rendered.
FORMATTERS = {
    "repetition": repetition_validator.format_report,
    "spacing": spacing_validator.format_report,
    "density": density_validator.format_report,
    "landmarks": landmark_validator.format_report,
    "navigation": navigation_validator.format_report,
    "sightlines": sightline_validator.format_report,
    "flow": flow_validator.format_report,
    "pacing": pacing_validator.format_report,
    "encounter": encounter_validator.format_report,
    "fairness": fairness_validator.format_report,
    "visual": visual_review.format_report,
}


@dataclass(frozen=True)
class SceneAnalysis:
    """Every measurement taken of one scene, with its score."""

    scene: SceneIR
    config: MapwrightConfig
    context: AnalysisContext
    reports: Mapping[str, Any]
    issues: tuple[Issue, ...]
    scorecard: Scorecard
    applicable: Mapping[Category, bool] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        """Return the weighted overall score."""
        return self.scorecard.overall

    def report(self, name: str) -> Any:
        """Return one analyzer's report by name."""
        return self.reports[name]

    def format_section(self, name: str) -> str:
        """Return one analyzer's terminal output."""
        formatter = FORMATTERS.get(name)
        if formatter is None:
            raise KeyError(f"No formatter registered for analyzer '{name}'.")
        return formatter(self.reports[name])

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of the whole analysis."""
        return {
            "scene": self.scene.scene,
            "source": self.scene.source_path,
            "engine": self.scene.source_engine,
            "profile": self.config.profile,
            "objects": len(self.scene.objects),
            "zones": len(self.scene.zones),
            "severity_counts": {
                severity.value: count
                for severity, count in count_by_severity(self.issues).items()
            },
            "score": self.scorecard.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
            "sections": {
                name: report.to_dict() for name, report in self.reports.items()
            },
        }


@dataclass(frozen=True)
class Iteration:
    """One pass of the improvement loop."""

    index: int
    before: float
    after: float
    issue_count_before: int
    issue_count_after: int
    plan: CorrectionPlan
    applied: tuple[Any, ...]
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def improved(self) -> bool:
        """Return whether this pass raised the overall score."""
        return self.after > self.before

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "iteration": self.index,
            "score_before": round(self.before, 2),
            "score_after": round(self.after, 2),
            "issues_before": self.issue_count_before,
            "issues_after": self.issue_count_after,
            "applied": [item.to_dict() for item in self.applied],
            "rejected": [
                {"object": object_id, "reason": reason}
                for object_id, reason in self.rejected
            ],
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True)
class ImprovementResult:
    """The outcome of analysing, correcting, and re-analysing a scene."""

    initial: SceneAnalysis
    final: SceneAnalysis
    iterations: tuple[Iteration, ...]

    @property
    def scene(self) -> SceneIR:
        """Return the corrected scene."""
        return self.final.scene

    @property
    def changed(self) -> bool:
        """Return whether any correction was actually applied."""
        return any(iteration.applied for iteration in self.iterations)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "score_before": round(self.initial.overall, 2),
            "score_after": round(self.final.overall, 2),
            "iterations": [iteration.to_dict() for iteration in self.iterations],
            "comparison": compare(self.initial.scorecard, self.final.scorecard),
        }


def analyze(scene: SceneIR, config: MapwrightConfig) -> SceneAnalysis:
    """Run every analyzer against a scene and score the findings."""
    context = AnalysisContext(scene=scene, config=config)
    reports: dict[str, Any] = {}
    issues: list[Issue] = []
    for name, analyzer in ANALYZERS:
        report = analyzer(context)
        reports[name] = report
        issues.extend(report.issues)
    applicable = applicable_categories(context)
    ordered = sort_issues(issues)
    return SceneAnalysis(
        scene=scene,
        config=config,
        context=context,
        reports=reports,
        issues=ordered,
        scorecard=score_issues(ordered, config, applicable),
        applicable=applicable,
    )


def improve(
    scene: SceneIR,
    config: MapwrightConfig,
    max_iterations: int | None = None,
    trials_per_pass: int = 8,
) -> ImprovementResult:
    """Analyse, correct, and re-analyse, keeping only changes that verifiably help.

    Corrections are applied and measured one at a time rather than as a batch.
    A batch is planned against a scene that no longer exists by the time the
    last edit lands: separating one crowded pair moves a prop into the space
    the next correction assumed was free. Verifying each edge case individually
    costs one analysis per candidate and makes the loop monotone — every kept
    change provably raised the score, and every rejected one is reported.
    """
    limit = max_iterations if max_iterations is not None else config.max_iterations
    initial = analyze(scene, config)
    current = initial
    iterations: list[Iteration] = []

    for index in range(1, max(limit, 0) + 1):
        plan = plan_corrections(
            current.context,
            current.issues,
            asset_provider=SceneAssetProvider(current.scene),
            limit=trials_per_pass,
        )
        if not plan.automatic:
            break
        start = current
        accepted: list[Any] = []
        rejected: list[tuple[str, str]] = []
        for correction in plan.automatic:
            candidate_scene, applied = apply_corrections(
                current.scene, CorrectionPlan((correction,))
            )
            if not applied:
                continue
            candidate = analyze(candidate_scene, config)
            if _is_better(candidate, current):
                current = candidate
                accepted.extend(applied)
            else:
                rejected.append(
                    (
                        correction.object_id or correction.issue_code,
                        f"measured at {candidate.overall:.1f} against "
                        f"{current.overall:.1f}; the move traded one finding for "
                        "another rather than resolving it",
                    )
                )
        iterations.append(
            Iteration(
                index=index,
                before=start.overall,
                after=current.overall,
                issue_count_before=len(start.issues),
                issue_count_after=len(current.issues),
                plan=plan,
                applied=tuple(accepted),
                rejected=tuple(rejected),
            )
        )
        if not accepted:
            break

    return ImprovementResult(
        initial=initial, final=current, iterations=tuple(iterations)
    )


def _is_better(candidate: SceneAnalysis, current: SceneAnalysis) -> bool:
    """Return whether a candidate scene is a real improvement on the current one."""
    if candidate.overall > current.overall + 1e-9:
        return True
    return abs(candidate.overall - current.overall) <= 1e-9 and len(
        candidate.issues
    ) < len(current.issues)


def applicable_categories(context: AnalysisContext) -> dict[Category, bool]:
    """Return which report sections this scene can actually be scored on.

    Scoring a single-player level zero for fairness would be a lie about the
    level rather than a measurement of it.
    """
    scene = context.scene
    return {
        Category.SPATIAL: bool(scene.props),
        Category.NAVIGATION: context.grid is not None,
        Category.FLOW: context.graph is not None,
        Category.PACING: bool(scene.zones),
        Category.ENCOUNTER: any(
            zone.type in COMBAT_ZONE_TYPES for zone in scene.zones
        ),
        Category.FAIRNESS: len(scene.teams) >= 2,
        Category.VISUAL: bool(scene.props),
    }
