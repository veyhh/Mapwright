"""Scoring, scorecards, comparison, and the structured visual review.

The scoring tests build findings directly, so each one states exactly what the
score is supposed to answer for. The visual tests build a scene whose mass is
deliberately lopsided and check the percentage against the geometry.
"""

from __future__ import annotations

import json
import math

import pytest

from mapwright.core.config import MapwrightConfig
from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.issues import Category, Issue, Severity
from mapwright.core.scene_ir import ObjectType, SceneIR, SceneObject
from mapwright.core.scoring import compare, score_issues
from mapwright.review.visual import (
    VISUAL_ISSUE_CODES,
    VisualReviewError,
    ingest_observations,
    review_visual,
)


CONFIG = MapwrightConfig()
WARNING_PENALTY = CONFIG.scoring.penalty(Severity.WARNING)


def finding(
    code: str,
    category: Category = Category.FLOW,
    severity: Severity = Severity.WARNING,
    subject: str = "a",
    advisory: bool = False,
) -> Issue:
    return Issue(
        code=code,
        category=category,
        severity=severity,
        summary=f"{code} was found",
        evidence="measured",
        recommendation="change it",
        subjects=(subject,),
        advisory=advisory,
    )


def repeats(code: str, count: int) -> list[Issue]:
    """Return ``count`` separately actionable findings sharing one code."""
    return [finding(code, subject=f"subject_{index}") for index in range(count)]


def flow_score(issues) -> float:
    return score_issues(issues, CONFIG).category(Category.FLOW).score


def ground(width: float = 20.0, depth: float = 12.0) -> SceneObject:
    return SceneObject(
        id="ground",
        name="Ground",
        type=ObjectType.GROUND,
        position=Vec3(0.0, 0.0, 0.0),
        bounds=Bounds(
            Vec3(-width / 2, -0.1, -depth / 2), Vec3(width / 2, 0.0, depth / 2)
        ),
    )


def pillar(identifier: str, x: float, z: float, height: float) -> SceneObject:
    """A one-metre-square prop of the given height, standing on the ground."""
    return SceneObject(
        id=identifier,
        name=identifier,
        type=ObjectType.PROP,
        position=Vec3(x, 0.0, z),
        bounds=Bounds.from_size(Vec3(1.0, height, 1.0)),
    )


TOWER_HEIGHT = 4.0
STUB_HEIGHT = 0.5
TOWERS = ((-4.0,), (-2.0,), (0.0,), (2.0,), (4.0,))
STUBS = (-3.0, 3.0)


def lopsided_scene() -> SceneIR:
    """A 20x12 level whose five tall props all stand on the east side."""
    towers = [
        pillar(f"tower_{index}", 6.0, z, TOWER_HEIGHT)
        for index, (z,) in enumerate(TOWERS)
    ]
    stubs = [
        pillar(f"stub_{index}", -6.0, z, STUB_HEIGHT) for index, z in enumerate(STUBS)
    ]
    return SceneIR(scene="lopsided", objects=(ground(), *towers, *stubs))


def context(scene: SceneIR) -> AnalysisContext:
    return AnalysisContext(scene=scene, config=MapwrightConfig())


def visual_finding(report, code: str) -> Issue:
    return next(issue for issue in report.issues if issue.code == code)


# -- Scoring arithmetic ------------------------------------------------------


def test_a_scene_with_no_findings_scores_one_hundred():
    card = score_issues([], CONFIG)
    assert card.overall == pytest.approx(100.0)
    assert all(item.score == pytest.approx(100.0) for item in card.categories)
    assert all(item.applicable for item in card.categories)


def test_a_finding_costs_its_own_category_and_no_other():
    card = score_issues([finding("chokepoint", Category.FLOW)], CONFIG)
    assert card.category(Category.FLOW).score == pytest.approx(
        100.0 - WARNING_PENALTY
    )
    assert all(
        item.score == pytest.approx(100.0)
        for item in card.categories
        if item.category is not Category.FLOW
    )
    assert card.overall < 100.0


def test_repeats_of_one_code_cost_less_than_the_same_number_of_distinct_codes():
    repeated = flow_score(repeats("chokepoint", 10))
    distinct = flow_score([finding(f"code_{index}") for index in range(10)])
    assert repeated > distinct
    assert repeated == pytest.approx(100.0 - WARNING_PENALTY * (1.0 + math.log(10)))
    assert distinct == pytest.approx(100.0 - WARNING_PENALTY * 10)


def test_more_repeats_still_score_lower_than_fewer():
    scores = [flow_score(repeats("chokepoint", count)) for count in (1, 2, 5, 10)]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)


def test_an_advisory_finding_costs_less_than_a_proven_one():
    advisory = flow_score([finding("chokepoint", advisory=True)])
    proven = flow_score([finding("chokepoint")])
    assert advisory > proven
    assert advisory == pytest.approx(
        100.0 - WARNING_PENALTY * CONFIG.scoring.advisory_factor
    )


def test_a_category_that_does_not_apply_is_left_out_of_the_overall():
    issues = [
        finding("fairness_not_applicable", Category.FAIRNESS, Severity.INFO),
        finding("chokepoint", Category.FLOW),
    ]
    excluded = score_issues(issues, CONFIG, {Category.FAIRNESS: False})
    counted = score_issues(issues, CONFIG)
    fairness = excluded.category(Category.FAIRNESS)

    assert not fairness.applicable
    assert fairness.to_dict()["reason"]
    assert len(excluded.applicable) == len(Category) - 1
    assert excluded.overall == pytest.approx(
        sum(item.score for item in excluded.applicable) / len(excluded.applicable)
    )
    # Fairness scored 98 rather than 100, so counting it would drag the level down.
    assert excluded.overall > counted.overall
    assert "n/a" in excluded.format_table()


def test_a_scene_with_nothing_scorable_has_no_overall():
    card = score_issues([], CONFIG, {category: False for category in Category})
    assert not card.scorable
    assert card.to_dict()["overall"] is None


# -- Comparing two runs ------------------------------------------------------


def test_compare_renders_the_movement_in_each_category():
    before = score_issues([finding("chokepoint", Category.FLOW)], CONFIG)
    after = score_issues([], CONFIG)
    text = compare(before, after)
    assert f"{100 - int(WARNING_PENALTY)} -> 100 (+{int(WARNING_PENALTY)})" in text
    assert "100 -> 100 (+0)" in text
    assert text.rstrip().endswith("99 -> 100 (+1)")


def test_compare_marks_a_category_that_becomes_scorable():
    before = score_issues([], CONFIG, {Category.FAIRNESS: False})
    after = score_issues([], CONFIG)
    text = compare(before, after)
    assert "n/a -> 100 (newly scorable)" in text
    assert "100 -> n/a (no longer scorable)" in compare(after, before)


# -- Visual review -----------------------------------------------------------


def test_tall_props_massed_on_one_side_raise_a_mass_imbalance():
    report = review_visual(context(lopsided_scene()))
    issue = visual_finding(report, "visual_mass_imbalance")
    east_volume = len(TOWERS) * TOWER_HEIGHT
    west_volume = len(STUBS) * STUB_HEIGHT
    expected = 100.0 * east_volume / (east_volume + west_volume)

    assert issue.metric("volume_share_percent") == pytest.approx(expected, abs=0.01)
    assert issue.metric("tall_share_percent") == pytest.approx(100.0)
    assert issue.metric("tall_objects") == float(len(TOWERS))
    assert f"{expected:.0f}%" in issue.evidence


def test_visual_findings_are_advisory_and_name_the_view_that_would_show_them():
    analysed = context(lopsided_scene())
    report = review_visual(analysed)
    issue = visual_finding(report, "visual_mass_imbalance")
    assert report.issues
    assert all(item.advisory for item in report.issues)
    assert issue.view == "iso_ne"
    assert issue.view in analysed.config.capture_views


def test_an_observation_from_a_capture_becomes_a_reviewed_finding():
    analysed = context(lopsided_scene())
    (issue,) = ingest_observations(
        [
            {
                "issue": "poor_silhouette",
                "evidence": "Every tower reads as one 4 m band in iso_ne.",
                "suggestion": "Drop two towers to half height.",
                "view": "iso_ne",
                "severity": "medium",
            }
        ],
        analysed,
    )
    assert issue.code == "poor_silhouette"
    assert issue.category is Category.VISUAL
    assert issue.severity is Severity.WARNING
    assert issue.view == "iso_ne"
    assert issue.metric("observed_in_capture") == 1.0
    # Somebody looked at the render, so this is not a geometric proxy.
    assert not issue.advisory


def test_an_unknown_observed_code_is_rejected_with_the_valid_list():
    analysed = context(lopsided_scene())
    with pytest.raises(VisualReviewError) as error:
        ingest_observations(
            [{"issue": "vibes_are_off", "evidence": "seen", "suggestion": "fix"}],
            analysed,
        )
    message = str(error.value)
    assert "vibes_are_off" in message
    assert all(code in message for code in VISUAL_ISSUE_CODES)


def test_an_observation_missing_its_evidence_is_rejected():
    analysed = context(lopsided_scene())
    with pytest.raises(VisualReviewError, match="evidence"):
        ingest_observations(
            [{"issue": "poor_silhouette", "suggestion": "fix"}], analysed
        )


def test_a_scorecard_is_serializable():
    card = score_issues(
        [finding("chokepoint"), finding("no_flank_route", Category.ENCOUNTER)],
        CONFIG,
        {Category.FAIRNESS: False},
    )
    data = card.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["overall"] == pytest.approx(card.overall, abs=0.01)
