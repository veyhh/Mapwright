"""Configuration loading, genre profiles, and backward compatibility."""

from __future__ import annotations

import pytest

from mapwright.core.config import (
    ConfigError,
    MapwrightConfig,
    apply_profile,
    available_profiles,
    config_from_mapping,
    load_config,
)
from mapwright.core.issues import Category, Severity


def test_defaults_are_usable_without_any_config_file():
    config = MapwrightConfig()
    assert config.profile == "generic"
    assert config.minimum_path_width == pytest.approx(0.9)
    assert config.eye_height == pytest.approx(1.62)
    assert config.navigation_mode == "approximate"


def test_every_shipped_profile_loads():
    profiles = available_profiles()
    assert set(profiles) == {
        "exploration",
        "fps",
        "horror",
        "moba",
        "platformer",
        "stealth",
    }
    for name in profiles:
        config = apply_profile(MapwrightConfig(), name)
        assert config.profile == name
        assert config.profile_description


def test_profiles_change_thresholds_and_weights_in_the_expected_direction():
    moba = apply_profile(MapwrightConfig(), "moba")
    horror = apply_profile(MapwrightConfig(), "horror")
    assert moba.thresholds.travel_time_asymmetry < 10.0
    assert moba.scoring.weight(Category.FAIRNESS) > horror.scoring.weight(Category.FAIRNESS)
    assert horror.thresholds.traversal_concentration > moba.thresholds.traversal_concentration


def test_profiles_can_downgrade_a_finding_that_is_genre_intent():
    horror = apply_profile(MapwrightConfig(), "horror")
    assert horror.severity_for("dead_end", Severity.WARNING) is Severity.INFO
    assert horror.severity_for("missing_relief", Severity.WARNING) is Severity.ERROR


def test_unknown_severity_override_target_keeps_the_caller_default():
    config = apply_profile(MapwrightConfig(), "fps")
    assert config.severity_for("not_a_real_code", Severity.ERROR) is Severity.ERROR


def test_unknown_profile_lists_the_available_ones():
    with pytest.raises(ConfigError, match="Available:"):
        apply_profile(MapwrightConfig(), "roguelike")


def test_typos_in_config_keys_are_rejected_rather_than_ignored():
    with pytest.raises(ConfigError, match="Unknown player key"):
        config_from_mapping({"player": {"radius": 0.4, "raduis": 0.5}}, "test")
    with pytest.raises(ConfigError, match="Unknown thresholds key"):
        config_from_mapping({"thresholds": {"minimum_spacng": 2.0}}, "test")
    with pytest.raises(ConfigError, match="Unknown key"):
        config_from_mapping({"playr": {}}, "test")


def test_player_metrics_drive_derived_values():
    config = config_from_mapping({"player": {"radius": 0.6, "height": 2.0}}, "test")
    assert config.minimum_path_width == pytest.approx(1.2)
    assert config.eye_height == pytest.approx(1.8)
    assert config.player.travel_time(9.0) == pytest.approx(2.0)


def test_explicit_path_width_overrides_the_player_derived_one():
    config = config_from_mapping({"thresholds": {"minimum_path_width": 1.5}}, "test")
    assert config.minimum_path_width == pytest.approx(1.5)


def test_config_overrides_are_applied_on_top_of_a_profile():
    config = config_from_mapping(
        {"profile": "moba", "thresholds": {"travel_time_asymmetry": 3.0}}, "test"
    )
    assert config.profile == "moba"
    assert config.thresholds.travel_time_asymmetry == pytest.approx(3.0)
    assert config.scoring.weight(Category.FAIRNESS) > 1.0


def test_scoring_weights_and_penalties_are_configurable():
    config = config_from_mapping(
        {"scoring": {"weights": {"flow": 2.0}, "penalties": {"WARNING": 12.0}}}, "test"
    )
    assert config.scoring.weight(Category.FLOW) == pytest.approx(2.0)
    assert config.scoring.penalty(Severity.WARNING) == pytest.approx(12.0)


def test_v01_godot_config_still_loads(tmp_path):
    legacy = tmp_path / "mapwright.config.yaml"
    legacy.write_text(
        "\n".join(
            [
                'godot_project_path: "./project"',
                'godot_executable_path: "/usr/bin/godot"',
                'target_scene: "scenes/main.tscn"',
                'asset_pack_path: "./assets"',
                'capture_output_dir: "./captures"',
                "capture_angles:",
                '  - "top_down"',
                '  - "iso_ne"',
                '  - "iso_sw"',
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(legacy)
    assert config.engine.project_path == "./project"
    assert config.engine.executable_path == "/usr/bin/godot"
    assert config.engine.target_scene == "scenes/main.tscn"
    assert config.capture_views == ("top_down", "iso_ne", "iso_sw")
    assert config.capture_output_dir == "./captures"


def test_invalid_navigation_mode_is_rejected():
    with pytest.raises(ConfigError, match="navigation mode"):
        config_from_mapping({"navigation": {"mode": "magic"}}, "test")


def test_config_summary_is_json_serializable():
    import json

    json.dumps(apply_profile(MapwrightConfig(), "stealth").to_dict())
