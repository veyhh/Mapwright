"""v0.1 parity: the Scene IR port must not change what the validators measure.

The v0.1 tools parsed Godot scenes directly. v0.2 parses once into Scene IR and
every validator reads that instead. These tests pin the two together on the
repository's own example scenes: given the same thresholds, the new pipeline
must produce the same numbers, not merely similar ones.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "tools"))

from density_analyzer import analyze_density as v01_density  # noqa: E402
from landmark_analyzer import analyze_landmarks as v01_landmarks  # noqa: E402
from navigation_analyzer import analyze_navigation as v01_navigation  # noqa: E402
from repetition_detector import analyze_scene as v01_repetition  # noqa: E402
from spacing_analyzer import analyze_spacing as v01_spacing  # noqa: E402
from spacing_analyzer import parse_scene_positions  # noqa: E402

from mapwright.adapters.godot.importer import GodotImporter  # noqa: E402
from mapwright.core.config import MapwrightConfig  # noqa: E402
from mapwright.core.context import AnalysisContext  # noqa: E402
from mapwright.validators import density as density_validator  # noqa: E402
from mapwright.validators import landmarks as landmark_validator  # noqa: E402
from mapwright.validators import navigation as navigation_validator  # noqa: E402
from mapwright.validators import repetition as repetition_validator  # noqa: E402
from mapwright.validators import spacing as spacing_validator  # noqa: E402


SCENES = (
    str(REPOSITORY / "examples" / "courtyard_level.tscn"),
    str(REPOSITORY / "examples" / "repetitive_level.tscn"),
)


def v01_equivalent_config() -> MapwrightConfig:
    """Return a config whose thresholds match the v0.1 command-line defaults."""
    config = MapwrightConfig()
    return replace(
        config,
        thresholds=replace(config.thresholds, minimum_path_width=1.0),
    )


@pytest.fixture(params=SCENES, ids=lambda path: Path(path).stem)
def scene_path(request) -> str:
    return request.param


@pytest.fixture
def context(scene_path: str) -> AnalysisContext:
    config = v01_equivalent_config()
    scene = GodotImporter().import_scene(scene_path, config)
    return AnalysisContext(scene=scene, config=config)


def test_importer_resolves_the_same_props_at_the_same_positions(
    scene_path: str, context: AnalysisContext
):
    legacy = {
        node.node_path: node
        for node in parse_scene_positions(Path(scene_path).read_text(encoding="utf-8"))
        if node.analyzed
    }
    current = {obj.id: obj for obj in context.scene.props}
    assert set(legacy) == set(current)
    for path, node in legacy.items():
        assert current[path].position.x == pytest.approx(node.position.x, abs=1e-9)
        assert current[path].position.y == pytest.approx(node.position.y, abs=1e-9)
        assert current[path].position.z == pytest.approx(node.position.z, abs=1e-9)


def test_landmark_sizes_and_candidates_are_unchanged(
    scene_path: str, context: AnalysisContext
):
    legacy = v01_landmarks(scene_path)
    current = landmark_validator.analyze_landmarks(context)
    assert current.median_size_score == pytest.approx(legacy.median_size_score, abs=1e-9)
    by_id = {obj.id: obj for obj in current.objects}
    for item in legacy.objects:
        assert by_id[item.node_path].size_score == pytest.approx(
            item.size_score, abs=1e-9
        )
    assert {obj.name for obj in current.candidates} == {
        obj.name for obj in legacy.candidates
    }


def test_repetition_counts_are_unchanged(scene_path: str, context: AnalysisContext):
    legacy = v01_repetition(scene_path, 5.0)
    current = repetition_validator.analyze_repetition(context)
    assert current.total_placements == legacy.total_references
    legacy_counts = {asset.name: asset.count for asset in legacy.assets}
    for asset in current.assets:
        assert asset.count == legacy_counts[asset.label]
        assert asset.percentage == pytest.approx(
            next(
                item.percentage for item in legacy.assets if item.name == asset.label
            ),
            abs=1e-9,
        )
    assert {asset.label for asset in current.assets if asset.flagged} == {
        asset.name for asset in legacy.assets if asset.flagged
    }


def test_spacing_pairs_are_unchanged(scene_path: str, context: AnalysisContext):
    legacy = v01_spacing(scene_path, 1.5)
    current = spacing_validator.analyze_spacing(context)
    legacy_pairs = {
        frozenset((issue.first.node_path, issue.second.node_path))
        for issue in legacy.issues
    }
    current_pairs = {
        frozenset(issue.subjects)
        for issue in current.issues
        if issue.code == "objects_too_close"
    }
    assert current_pairs == legacy_pairs


def test_density_statistics_are_unchanged(scene_path: str, context: AnalysisContext):
    legacy = v01_density(scene_path, 3.0, 55.0)
    current = density_validator.analyze_density(context)
    assert current.prop_count == legacy.prop_count
    assert len(current.cells) == len(legacy.cells)
    assert current.mean == pytest.approx(legacy.mean, abs=1e-9)
    assert current.variance == pytest.approx(legacy.variance, abs=1e-9)
    assert current.imbalance == pytest.approx(legacy.imbalance_score, abs=1e-9)


def test_navigation_occupancy_is_unchanged(scene_path: str, context: AnalysisContext):
    legacy = v01_navigation(scene_path, 1.0, 0.25, 1.0)
    current = navigation_validator.analyze_navigation(context)
    assert current.free_cell_count == legacy.free_cell_count
    assert current.blocked_cell_count == legacy.blocked_cell_count
    assert current.reachable_ratio == pytest.approx(legacy.reachable_ratio, abs=1e-9)
    assert len([region for region in current.regions if region.significant]) == len(
        legacy.significant_regions
    )


def test_ground_bounds_match_the_v01_detector(scene_path: str, context: AnalysisContext):
    legacy = v01_density(scene_path, 3.0, 55.0).ground
    ground = context.scene.ground_bounds()
    assert ground.min.x == pytest.approx(legacy.min_x, abs=1e-9)
    assert ground.max.x == pytest.approx(legacy.max_x, abs=1e-9)
    assert ground.min.z == pytest.approx(legacy.min_z, abs=1e-9)
    assert ground.max.z == pytest.approx(legacy.max_z, abs=1e-9)


def test_v01_command_line_entry_points_still_run(scene_path: str):
    import subprocess

    for tool, extra in (
        ("repetition_detector.py", ["--threshold", "5"]),
        ("spacing_analyzer.py", ["--threshold", "1.5"]),
        ("density_analyzer.py", ["--cell-size", "3"]),
        ("landmark_analyzer.py", []),
    ):
        completed = subprocess.run(
            [sys.executable, str(REPOSITORY / "tools" / tool), scene_path, *extra],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, f"{tool} failed: {completed.stderr[:400]}"
        assert "Mapwright" in completed.stdout
