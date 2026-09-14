"""Competitive fairness: whether both teams start the match on equal terms.

The arena below is mirrored about the objective at the centre of the map, so
any asymmetry a test reports is one the test itself introduced.
"""

from __future__ import annotations

import json

import pytest

from mapwright.core.config import MapwrightConfig, apply_profile
from mapwright.core.context import AnalysisContext
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.issues import Severity
from mapwright.core.scene_ir import (
    MarkerKind,
    MarkerPoint,
    ObjectType,
    PacingLevel,
    SceneIR,
    SceneObject,
    Zone,
    ZoneType,
)
from mapwright.validators.fairness import NOT_APPLICABLE, validate_fairness


def ground(width: float = 24.0, depth: float = 12.0) -> SceneObject:
    return SceneObject(
        id="ground",
        name="Ground",
        type=ObjectType.GROUND,
        position=Vec3(0.0, 0.0, 0.0),
        bounds=Bounds(
            Vec3(-width / 2, -0.1, -depth / 2), Vec3(width / 2, 0.0, depth / 2)
        ),
    )


def lane(name: str, x0: float, x1: float, z0: float, z1: float) -> Zone:
    return Zone(
        name,
        ZoneType.TRAVERSAL,
        PacingLevel.MEDIUM,
        Bounds(Vec3(x0, 0.0, z0), Vec3(x1, 3.0, z1)),
    )


def arena(red_setback: float = 0.0) -> SceneIR:
    """A 24x12 arena, mirrored east to west about a central objective.

    ``red_setback`` pushes the red spawn that many metres further west, which
    is the only thing that can make the two teams' routes differ.
    """
    return SceneIR(
        scene="clash",
        objects=(ground(),),
        zones=(
            lane("west_lane", -12.0, -4.0, -6.0, 6.0),
            lane("east_lane", 4.0, 12.0, -6.0, 6.0),
            lane("mid_north", -4.0, 4.0, 0.5, 6.0),
            lane("mid_south", -4.0, 4.0, -6.0, -0.5),
        ),
        spawn_points=(
            MarkerPoint(
                "red_spawn",
                MarkerKind.SPAWN,
                Vec3(-9.0 - red_setback, 0.0, 0.0),
                team="red",
            ),
            MarkerPoint("blue_spawn", MarkerKind.SPAWN, Vec3(9.0, 0.0, 0.0), team="blue"),
        ),
        objectives=(MarkerPoint("core", MarkerKind.OBJECTIVE, Vec3(0.0, 0.0, 0.0)),),
    )


def solo_scene(spawns: tuple[MarkerPoint, ...] = ()) -> SceneIR:
    """The same arena with no contested teams: single-player space."""
    return SceneIR(
        scene="solo",
        objects=(ground(),),
        spawn_points=spawns,
        objectives=(MarkerPoint("core", MarkerKind.OBJECTIVE, Vec3(0.0, 0.0, 0.0)),),
    )


def context(scene: SceneIR, profile: str | None = None) -> AnalysisContext:
    config = MapwrightConfig()
    if profile is not None:
        config = apply_profile(config, profile)
    return AnalysisContext(scene=scene, config=config)


def access_for(report, team: str):
    return report.for_team(team)[0]


def travel_comparison(report):
    return next(item for item in report.comparisons if item.metric == "travel_time")


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_a_mirrored_map_gives_both_teams_the_same_route():
    report = validate_fairness(context(arena()))
    red, blue = access_for(report, "red"), access_for(report, "blue")
    assert report.applicable
    assert report.teams == ("blue", "red")
    assert red.distance == pytest.approx(blue.distance)
    assert report.worst_asymmetry == pytest.approx(0.0)
    assert report.issues == ()


def test_setting_one_spawn_back_raises_a_travel_time_imbalance():
    setback = 2.0
    report = validate_fairness(context(arena(red_setback=setback)))
    red, blue = access_for(report, "red"), access_for(report, "blue")
    assert red.distance - blue.distance == pytest.approx(setback, abs=0.3)
    assert "travel_time_imbalance" in codes(report.issues)


def test_the_reported_asymmetry_matches_the_distances_measured():
    report = validate_fairness(context(arena(red_setback=2.0)))
    red, blue = access_for(report, "red"), access_for(report, "blue")
    expected = 100.0 * (red.travel_time - blue.travel_time) / blue.travel_time
    comparison = travel_comparison(report)
    finding = next(
        issue for issue in report.issues if issue.code == "travel_time_imbalance"
    )
    assert comparison.worst_team == "red"
    assert comparison.best_team == "blue"
    assert comparison.asymmetry_percent == pytest.approx(expected)
    assert finding.metric("asymmetry_percent") == pytest.approx(expected, abs=0.01)
    assert f"{expected:.1f}%" in finding.evidence
    assert "TEAM RED" in finding.evidence


def test_a_scene_with_no_teams_is_not_applicable_rather_than_unfair():
    report = validate_fairness(context(solo_scene()))
    assert not report.applicable
    assert report.reason is not None
    assert len(report.issues) == 1
    assert report.issues[0].code == NOT_APPLICABLE
    assert report.issues[0].severity is Severity.INFO


def test_one_team_alone_is_not_applicable_either():
    scene = solo_scene(
        spawns=(
            MarkerPoint("red_a", MarkerKind.SPAWN, Vec3(-9, 0, 0), team="red"),
            MarkerPoint("red_b", MarkerKind.SPAWN, Vec3(-9, 0, 3), team="red"),
        )
    )
    report = validate_fairness(context(scene))
    assert not report.applicable
    assert codes(report.issues) == {NOT_APPLICABLE}
    assert report.issues[0].metric("teams") == 1.0


def test_the_moba_profile_flags_a_gap_the_default_profile_accepts():
    scene = arena(red_setback=0.5)
    default = validate_fairness(context(scene))
    moba = validate_fairness(context(scene, profile="moba"))
    gap = travel_comparison(default).asymmetry_percent

    assert MapwrightConfig().thresholds.travel_time_asymmetry == 10.0
    assert apply_profile(MapwrightConfig(), "moba").thresholds.travel_time_asymmetry == 5.0
    assert 5.0 < gap < 10.0
    assert "travel_time_imbalance" not in codes(default.issues)
    assert "travel_time_imbalance" in codes(moba.issues)
    flagged = next(
        issue for issue in moba.issues if issue.code == "travel_time_imbalance"
    )
    assert flagged.severity is Severity.CRITICAL
    assert flagged.metric("threshold_percent") == 5.0


def test_a_fairness_report_is_deterministic_and_serializable():
    scene = arena(red_setback=2.0)
    first = validate_fairness(context(scene)).to_dict()
    second = validate_fairness(context(scene)).to_dict()
    assert first == second
    assert json.loads(json.dumps(first)) == first
