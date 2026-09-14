"""Configuration, player metrics, and genre profiles.

Thresholds are data, not code: the same validators serve a horror corridor and
a MOBA lane because a profile shifts the numbers and the severity of specific
findings. Unknown keys are rejected so a typo never silently keeps a default.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from mapwright.core.issues import Category, Severity


PROFILE_DIRECTORY = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_CAPTURE_VIEWS = ("top_down", "iso_ne", "iso_sw")
NAVIGATION_MODES = ("approximate", "engine")


class ConfigError(ValueError):
    """Raised when configuration or a profile cannot be used as written."""


@dataclass(frozen=True)
class PlayerMetrics:
    """The moving body every clearance check is measured against."""

    radius: float = 0.45
    height: float = 1.8
    walk_speed: float = 4.5
    sprint_speed: float = 7.0
    crouch_height: float = 1.1
    jump_height: float = 1.2
    jump_distance: float = 3.8
    interaction_reach: float = 2.0

    @property
    def diameter(self) -> float:
        """Return the minimum gap a standing player fits through."""
        return self.radius * 2.0

    @property
    def eye_height(self) -> float:
        """Return the default eye height: just below standing height."""
        return self.height * 0.9

    def travel_time(self, distance: float, sprinting: bool = False) -> float:
        """Return seconds to cover a distance at walk or sprint speed."""
        speed = self.sprint_speed if sprinting else self.walk_speed
        if speed <= 0:
            raise ConfigError("Player speed must be positive to estimate travel time.")
        return distance / speed

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlayerMetrics:
        """Build player metrics from a mapping, validating every value."""
        return _build_numeric(cls, data, "player")


@dataclass(frozen=True)
class Thresholds:
    """Every numeric line a finding is measured against."""

    # Spatial
    repetition_percent: float = 5.0
    minimum_spacing: float = 1.5
    density_cell_size: float = 3.0
    density_imbalance: float = 55.0
    landmark_candidate_ratio: float = 1.15
    landmark_hierarchy_factor: float = 1.5
    # Navigation
    navigation_cell_size: float = 0.25
    navigation_minimum_region_area: float = 1.0
    minimum_path_width: float = 0.0  # 0 means "derive from player diameter"
    # Sightlines
    target_height_ratio: float = 0.6
    sightline_test_points: float = 5.0
    # Flow
    chokepoint_width: float = 2.0
    traversal_concentration: float = 55.0
    dead_end_ratio: float = 35.0
    minimum_route_diversity: float = 2.0
    # Pacing
    max_consecutive_high: float = 3.0
    abrupt_transition_delta: float = 2.0
    # Encounter
    cover_height_min: float = 0.6
    cover_height_max: float = 2.0
    high_ground_delta: float = 1.0
    minimum_encounter_entrances: float = 2.0
    minimum_flank_routes: float = 1.0
    # Fairness
    travel_time_asymmetry: float = 10.0
    cover_asymmetry: float = 25.0
    choke_width_asymmetry: float = 20.0
    # Visual readability
    visual_mass_imbalance: float = 65.0
    empty_zone_ratio: float = 50.0

    def resolved_path_width(self, player: PlayerMetrics) -> float:
        """Return the traversable diameter, defaulting to the player's."""
        return self.minimum_path_width if self.minimum_path_width > 0 else player.diameter

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Thresholds:
        """Build thresholds from a mapping, validating every value."""
        return _build_numeric(cls, data, "thresholds")

    def merged(self, data: Mapping[str, Any]) -> Thresholds:
        """Return a copy with the provided overrides applied."""
        _reject_unknown(type(self), data, "thresholds")
        return replace(self, **_numeric_values(type(self), data, "thresholds"))


@dataclass(frozen=True)
class Scoring:
    """How measured findings become 0-100 category scores.

    Each category starts at 100 and loses ``penalties[severity]`` points per
    finding, so a score is always traceable to the list above it.
    """

    weights: tuple[tuple[Category, float], ...] = ()
    penalties: tuple[tuple[Severity, float], ...] = ()
    advisory_factor: float = 0.4

    DEFAULT_WEIGHTS = {
        Category.SPATIAL: 1.0,
        Category.FLOW: 1.0,
        Category.NAVIGATION: 1.0,
        Category.PACING: 1.0,
        Category.ENCOUNTER: 1.0,
        Category.FAIRNESS: 1.0,
        Category.VISUAL: 1.0,
    }
    DEFAULT_PENALTIES = {
        Severity.INFO: 2.0,
        Severity.WARNING: 8.0,
        Severity.ERROR: 18.0,
        Severity.CRITICAL: 34.0,
    }

    def weight(self, category: Category) -> float:
        """Return the overall-score weight for one category."""
        return dict(self.weights).get(category, self.DEFAULT_WEIGHTS[category])

    def penalty(self, severity: Severity) -> float:
        """Return the points one finding of this severity costs."""
        return dict(self.penalties).get(severity, self.DEFAULT_PENALTIES[severity])

    @classmethod
    def default(cls) -> Scoring:
        """Return the unweighted default scoring model."""
        return cls(
            weights=tuple(cls.DEFAULT_WEIGHTS.items()),
            penalties=tuple(cls.DEFAULT_PENALTIES.items()),
        )

    def merged(self, data: Mapping[str, Any]) -> Scoring:
        """Return a copy with weight, penalty, and factor overrides applied."""
        known = {"weights", "penalties", "advisory_factor"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                f"Unknown scoring key(s): {', '.join(unknown)}. "
                f"Supported: {', '.join(sorted(known))}."
            )
        weights = dict(self.weights)
        for name, value in _mapping(data, "weights", "scoring.weights").items():
            weights[_parse_category(name)] = _positive(value, f"scoring.weights.{name}")
        penalties = dict(self.penalties)
        for name, value in _mapping(data, "penalties", "scoring.penalties").items():
            penalties[Severity.parse(name)] = _non_negative(
                value, f"scoring.penalties.{name}"
            )
        factor = self.advisory_factor
        if "advisory_factor" in data:
            factor = _non_negative(data["advisory_factor"], "scoring.advisory_factor")
        return Scoring(
            weights=tuple(sorted(weights.items(), key=lambda item: item[0].value)),
            penalties=tuple(sorted(penalties.items(), key=lambda item: item[0].rank)),
            advisory_factor=factor,
        )


@dataclass(frozen=True)
class EngineSettings:
    """Optional paths an engine adapter needs to reach a live project."""

    project_path: str | None = None
    executable_path: str | None = None
    target_scene: str | None = None
    asset_pack_path: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], context: str) -> EngineSettings:
        """Build engine settings from a mapping."""
        _reject_unknown(cls, data, context)
        return cls(
            **{
                key: None if data.get(key) is None else str(data[key])
                for key in (item.name for item in fields(cls))
                if key in data
            }
        )


@dataclass(frozen=True)
class MapwrightConfig:
    """The complete resolved configuration for one analysis run."""

    profile: str = "generic"
    profile_description: str = "Engine-agnostic defaults with no genre bias."
    player: PlayerMetrics = PlayerMetrics()
    thresholds: Thresholds = Thresholds()
    scoring: Scoring = Scoring.default()
    severity_overrides: tuple[tuple[str, Severity], ...] = ()
    max_iterations: int = 3
    navigation_mode: str = "approximate"
    capture_views: tuple[str, ...] = DEFAULT_CAPTURE_VIEWS
    capture_output_dir: str = "captures"
    report_dir: str = "reports"
    engine: EngineSettings = EngineSettings()

    def severity_for(self, code: str, default: Severity) -> Severity:
        """Return the severity a profile assigns to a finding code."""
        return dict(self.severity_overrides).get(code, default)

    @property
    def eye_height(self) -> float:
        """Return the sightline eye height derived from player height."""
        return self.player.eye_height

    @property
    def minimum_path_width(self) -> float:
        """Return the traversable diameter used by navigation and flow."""
        return self.thresholds.resolved_path_width(self.player)

    def with_profile(self, name: str) -> MapwrightConfig:
        """Return a copy with a genre profile applied over current values."""
        return apply_profile(self, name)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of the resolved configuration."""
        return {
            "profile": self.profile,
            "player": {
                item.name: getattr(self.player, item.name)
                for item in fields(PlayerMetrics)
            },
            "thresholds": {
                item.name: getattr(self.thresholds, item.name)
                for item in fields(Thresholds)
            },
            "scoring": {
                "weights": {
                    category.value: self.scoring.weight(category) for category in Category
                },
                "penalties": {
                    severity.value: self.scoring.penalty(severity) for severity in Severity
                },
                "advisory_factor": self.scoring.advisory_factor,
            },
            "severity_overrides": {
                code: severity.value for code, severity in self.severity_overrides
            },
            "navigation_mode": self.navigation_mode,
            "max_iterations": self.max_iterations,
            "capture_views": list(self.capture_views),
        }


def available_profiles() -> tuple[str, ...]:
    """Return the built-in genre profile names."""
    if not PROFILE_DIRECTORY.is_dir():
        return ()
    return tuple(sorted(path.stem for path in PROFILE_DIRECTORY.glob("*.yaml")))


def apply_profile(config: MapwrightConfig, name: str) -> MapwrightConfig:
    """Apply a built-in genre profile on top of a configuration."""
    profile_path = PROFILE_DIRECTORY / f"{name}.yaml"
    if not profile_path.is_file():
        supported = ", ".join(available_profiles()) or "none installed"
        raise ConfigError(f"Unknown profile '{name}'. Available: {supported}.")
    data = _read_yaml_mapping(profile_path)
    known = {"name", "description", "thresholds", "weights", "severity_overrides"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in profile '{name}': {', '.join(unknown)}. "
            f"Supported: {', '.join(sorted(known))}."
        )
    overrides = dict(config.severity_overrides)
    for code, severity in _mapping(
        data, "severity_overrides", f"profile '{name}' severity_overrides"
    ).items():
        overrides[str(code)] = Severity.parse(severity)
    scoring = config.scoring
    if "weights" in data:
        scoring = scoring.merged({"weights": data["weights"]})
    return replace(
        config,
        profile=str(data.get("name", name)),
        profile_description=str(data.get("description", config.profile_description)),
        thresholds=config.thresholds.merged(
            _mapping(data, "thresholds", f"profile '{name}' thresholds")
        ),
        scoring=scoring,
        severity_overrides=tuple(sorted(overrides.items())),
    )


def load_config(path: str | Path) -> MapwrightConfig:
    """Load a ``mapwright.config.yaml`` document into a resolved configuration.

    v0.1 documents keep working: the old Godot-only keys are read into
    ``engine`` and ``capture_*`` rather than rejected.
    """
    config_path = Path(path).expanduser()
    data = _read_yaml_mapping(config_path)
    return config_from_mapping(data, str(config_path))


def config_from_mapping(data: Mapping[str, Any], context: str) -> MapwrightConfig:
    """Build a configuration from an already-parsed mapping."""
    known = {
        "profile",
        "player",
        "thresholds",
        "scoring",
        "severity_overrides",
        "iterations",
        "navigation",
        "capture",
        "engine",
        "report_dir",
        # v0.1 compatibility keys.
        "godot_project_path",
        "godot_executable_path",
        "target_scene",
        "asset_pack_path",
        "capture_output_dir",
        "capture_angles",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in {context}: {', '.join(unknown)}. "
            f"Supported: {', '.join(sorted(known))}."
        )

    config = MapwrightConfig()
    if "profile" in data:
        config = apply_profile(config, str(data["profile"]))
    config = replace(
        config,
        player=PlayerMetrics.from_dict(_mapping(data, "player", f"{context} player")),
        thresholds=config.thresholds.merged(
            _mapping(data, "thresholds", f"{context} thresholds")
        ),
    )
    if "scoring" in data:
        config = replace(
            config,
            scoring=config.scoring.merged(
                _mapping(data, "scoring", f"{context} scoring")
            ),
        )
    overrides = dict(config.severity_overrides)
    for code, severity in _mapping(
        data, "severity_overrides", f"{context} severity_overrides"
    ).items():
        overrides[str(code)] = Severity.parse(severity)

    iterations = _mapping(data, "iterations", f"{context} iterations")
    max_iterations = config.max_iterations
    if "max" in iterations:
        max_iterations = int(_positive(iterations["max"], f"{context} iterations.max"))

    navigation = _mapping(data, "navigation", f"{context} navigation")
    navigation_mode = str(navigation.get("mode", config.navigation_mode))
    if navigation_mode not in NAVIGATION_MODES:
        raise ConfigError(
            f"Unknown navigation mode '{navigation_mode}' in {context}. "
            f"Supported: {', '.join(NAVIGATION_MODES)}."
        )

    capture = _mapping(data, "capture", f"{context} capture")
    views = capture.get("views", data.get("capture_angles", config.capture_views))
    if not isinstance(views, (list, tuple)) or not views:
        raise ConfigError(f"{context} capture views must be a non-empty list.")
    output_dir = str(
        capture.get(
            "output_dir", data.get("capture_output_dir", config.capture_output_dir)
        )
    )

    engine_data = dict(_mapping(data, "engine", f"{context} engine"))
    for legacy_key, engine_key in (
        ("godot_project_path", "project_path"),
        ("godot_executable_path", "executable_path"),
        ("target_scene", "target_scene"),
        ("asset_pack_path", "asset_pack_path"),
    ):
        if data.get(legacy_key) is not None and engine_key not in engine_data:
            engine_data[engine_key] = data[legacy_key]

    return replace(
        config,
        severity_overrides=tuple(sorted(overrides.items())),
        max_iterations=max_iterations,
        navigation_mode=navigation_mode,
        capture_views=tuple(str(view) for view in views),
        capture_output_dir=output_dir,
        report_dir=str(data.get("report_dir", config.report_dir)),
        engine=EngineSettings.from_dict(engine_data, f"{context} engine"),
    )


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Mapwright config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at its root.")
    return data


def _mapping(data: Mapping[str, Any], key: str, context: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a mapping.")
    return value


def _parse_category(name: str) -> Category:
    try:
        return Category(str(name))
    except ValueError as exc:
        supported = ", ".join(item.value for item in Category)
        raise ConfigError(
            f"Unknown scoring category '{name}'. Supported: {supported}."
        ) from exc


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context} must be a number, found {value!r}.")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"{context} must be finite.")
    return number


def _positive(value: Any, context: str) -> float:
    number = _number(value, context)
    if number <= 0:
        raise ConfigError(f"{context} must be greater than zero.")
    return number


def _non_negative(value: Any, context: str) -> float:
    number = _number(value, context)
    if number < 0:
        raise ConfigError(f"{context} must not be negative.")
    return number


def _reject_unknown(cls: type, data: Mapping[str, Any], context: str) -> None:
    known = {item.name for item in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"Unknown {context} key(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(sorted(known))}."
        )


def _numeric_values(
    cls: type, data: Mapping[str, Any], context: str
) -> dict[str, float]:
    return {
        key: _non_negative(value, f"{context}.{key}")
        for key, value in data.items()
        if key in {item.name for item in fields(cls)}
    }


def _build_numeric(cls: type, data: Mapping[str, Any], context: str):
    _reject_unknown(cls, data, context)
    return cls(**_numeric_values(cls, data, context))
