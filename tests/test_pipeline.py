"""End-to-end: analyze, report, correct, re-analyze, and prove the score moved."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mapwright.core.config import MapwrightConfig
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.issues import Category, Severity
from mapwright.core.scene_ir import ObjectType, SceneIR, SceneObject
from mapwright.design.correction import (
    CorrectionKind,
    apply_corrections,
    plan_corrections,
)
from mapwright.ecosystem.asset_director import SceneAssetProvider
from mapwright.pipeline import analyze, improve
from mapwright.review.report import build_report

REPOSITORY = Path(__file__).resolve().parent.parent
OUTPOST = REPOSITORY / "examples" / "outpost_level.json"
DUEL = REPOSITORY / "examples" / "duel_arena.json"


@pytest.fixture(scope="module")
def outpost() -> SceneIR:
    return SceneIR.read_json(OUTPOST)


@pytest.fixture(scope="module")
def baseline(outpost: SceneIR):
    return analyze(outpost, MapwrightConfig())


@pytest.fixture(scope="module")
def improved(outpost: SceneIR):
    return improve(outpost, MapwrightConfig())


def crowded_scene() -> SceneIR:
    """A small room whose props sit far closer together than the spacing line."""
    objects = [
        SceneObject(
            id="ground",
            name="Ground",
            type=ObjectType.GROUND,
            position=Vec3(0.0, 0.0, 0.0),
            bounds=Bounds(Vec3(-10, -0.1, -10), Vec3(10, 0, 10)),
        )
    ]
    for index, (x, z) in enumerate(
        [(0.0, 0.0), (0.6, 0.3), (-0.5, 0.4), (0.2, -0.6)], 1
    ):
        objects.append(
            SceneObject(
                id=f"crate_{index}",
                name=f"Crate{index}",
                type=ObjectType.PROP,
                position=Vec3(x, 0.0, z),
                bounds=Bounds.from_size(Vec3(0.8, 0.8, 0.8)),
                asset="props/crate",
            )
        )
    return SceneIR(scene="crowded", objects=tuple(objects))


# -- Analysis ---------------------------------------------------------------


def test_analysis_runs_every_section(baseline):
    expected = {
        "repetition",
        "spacing",
        "density",
        "landmarks",
        "navigation",
        "sightlines",
        "flow",
        "pacing",
        "encounter",
        "fairness",
        "visual",
    }
    assert set(baseline.reports) == expected
    for name in expected:
        assert isinstance(baseline.format_section(name), str)


def test_analysis_is_json_serializable_and_deterministic(outpost):
    first = analyze(outpost, MapwrightConfig()).to_dict()
    second = analyze(outpost, MapwrightConfig()).to_dict()
    assert first == second
    json.dumps(first)


def test_every_finding_explains_itself(baseline):
    assert baseline.issues
    for issue in baseline.issues:
        assert issue.evidence.strip(), f"{issue.code} has no evidence"
        assert issue.recommendation.strip(), f"{issue.code} has no recommendation"
        assert any(character.isdigit() for character in issue.evidence) or issue.zone, (
            f"{issue.code} cites neither a number nor a place"
        )


def test_single_player_scene_is_not_scored_on_fairness(baseline):
    fairness = baseline.scorecard.category(Category.FAIRNESS)
    assert not fairness.applicable
    assert fairness.reason
    assert 0.0 < baseline.overall <= 100.0


def test_a_team_scene_is_scored_on_fairness():
    analysis = analyze(SceneIR.read_json(DUEL), MapwrightConfig())
    assert analysis.scorecard.category(Category.FAIRNESS).applicable


def test_the_deliberate_faults_in_the_example_are_all_found(baseline):
    codes = {issue.code for issue in baseline.issues}
    for expected in (
        "density_imbalance",
        "objects_too_close",
        "chokepoint",
        "low_route_diversity",
        "missing_relief",
        "no_flank_route",
    ):
        assert expected in codes, f"{expected} was not detected in the example level"


def test_profiles_change_the_verdict_on_one_scene(outpost):
    default = {
        issue.code: issue.severity
        for issue in analyze(outpost, MapwrightConfig()).issues
    }
    horror = {
        issue.code: issue.severity
        for issue in analyze(outpost, MapwrightConfig().with_profile("horror")).issues
    }
    # A forced single route is a defect in the default profile and the point
    # of the genre in horror, which tolerates it by threshold or severity.
    assert default["low_route_diversity"] is Severity.WARNING
    assert horror.get("low_route_diversity") in (None, Severity.INFO)
    # Dread without release is the horror-specific failure, so it escalates.
    assert default["missing_relief"] is Severity.WARNING
    assert horror["missing_relief"] is Severity.ERROR


# -- Reporting --------------------------------------------------------------


def test_report_writes_both_forms_with_every_section(tmp_path, baseline):
    report = build_report(baseline)
    markdown_path, json_path = report.write(tmp_path)
    text = markdown_path.read_text(encoding="utf-8")
    for heading in (
        "## Summary",
        "## Score",
        "## Spatial Quality",
        "## Flow",
        "## Navigation",
        "## Pacing",
        "## Encounter",
        "## Fairness",
        "## Visual Readability",
        "## Corrections",
    ):
        assert heading in text
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["verdict"]
    assert data["issues"]
    assert data["structural"]["coverage"]


# -- Correction -------------------------------------------------------------


def test_corrections_never_delete_placed_content(baseline):
    plan = plan_corrections(
        baseline.context, baseline.issues, SceneAssetProvider(baseline.scene)
    )
    assert plan.corrections
    assert all(item.kind is not CorrectionKind.REMOVE for item in plan.corrections)


def test_structural_findings_are_proposed_not_faked(baseline):
    plan = plan_corrections(baseline.context, baseline.issues)
    structural = [item for item in plan.manual if item.issue_code == "chokepoint"]
    assert structural
    assert all(item.kind is CorrectionKind.STRUCTURAL for item in structural)
    assert all(item.rationale.strip() for item in structural)
    assert all(item.position is None for item in structural)


def test_asset_substitution_needs_a_person(baseline):
    plan = plan_corrections(
        baseline.context, baseline.issues, SceneAssetProvider(baseline.scene)
    )
    swaps = [item for item in plan.corrections if item.kind is CorrectionKind.SUBSTITUTE]
    assert swaps
    assert all(item.manual for item in swaps)


def test_a_separation_move_keeps_the_object_on_the_ground():
    scene = crowded_scene()
    analysis = analyze(scene, MapwrightConfig())
    plan = plan_corrections(analysis.context, analysis.issues)
    corrected, applied = apply_corrections(scene, plan)
    assert applied
    ground = scene.ground_bounds()
    for record in applied:
        moved = corrected.object_by_id(record.correction.object_id)
        assert ground.contains_xz(moved.position)
        assert moved.position != record.previous_position


def test_applying_corrections_leaves_the_original_scene_untouched():
    scene = crowded_scene()
    analysis = analyze(scene, MapwrightConfig())
    plan = plan_corrections(analysis.context, analysis.issues)
    before = scene.to_dict()
    apply_corrections(scene, plan)
    assert scene.to_dict() == before


def test_correction_planning_is_deterministic():
    scene = crowded_scene()
    analysis = analyze(scene, MapwrightConfig())
    first = plan_corrections(analysis.context, analysis.issues).to_dict()
    second = plan_corrections(analysis.context, analysis.issues).to_dict()
    assert first == second


# -- The full loop ----------------------------------------------------------


def test_improving_the_example_raises_the_score_and_clears_findings(improved):
    result = improved
    assert result.changed
    assert result.final.overall > result.initial.overall
    assert len(result.final.issues) < len(result.initial.issues)
    spatial_before = result.initial.scorecard.category(Category.SPATIAL).score
    spatial_after = result.final.scorecard.category(Category.SPATIAL).score
    assert spatial_after > spatial_before


def test_every_kept_change_was_verified_to_help(improved):
    result = improved
    for iteration in result.iterations:
        if iteration.applied:
            assert iteration.after > iteration.before
    assert all(
        reason for _, reason in
        (item for iteration in result.iterations for item in iteration.rejected)
    )


def test_the_loop_never_makes_a_scene_worse():
    scene = crowded_scene()
    result = improve(scene, MapwrightConfig(), max_iterations=2)
    assert result.final.overall >= result.initial.overall


def test_a_clean_scene_needs_no_correction():
    scene = SceneIR(
        scene="empty_room",
        objects=(
            SceneObject(
                id="ground",
                name="Ground",
                type=ObjectType.GROUND,
                position=Vec3(0.0, 0.0, 0.0),
                bounds=Bounds(Vec3(-8, -0.1, -8), Vec3(8, 0, 8)),
            ),
        ),
    )
    result = improve(scene, MapwrightConfig(), max_iterations=1)
    assert not result.changed
    assert result.final.overall == result.initial.overall


def test_improvement_is_reported_with_before_and_after(improved):
    result = improved
    data = result.to_dict()
    assert data["score_after"] > data["score_before"]
    assert "->" in data["comparison"]
    report = build_report(result.final, improvement=result)
    assert "Before and after correction" in report.markdown
    assert "### Applied" in report.markdown
