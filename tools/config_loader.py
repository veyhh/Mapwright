"""Load and validate Mapwright YAML configuration files."""

from pathlib import Path
from typing import Any

import yaml


REQUIRED_FIELDS = (
    "godot_project_path",
    "target_scene",
    "asset_pack_path",
    "capture_output_dir",
    "capture_angles",
)


def load_config(path: str) -> dict[str, Any]:
    """Load a Mapwright YAML config and ensure required fields are present."""
    config_path = Path(path).expanduser()

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Mapwright config file not found: {config_path}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Mapwright config file contains invalid YAML: {config_path}"
        ) from exc

    if not isinstance(config, dict):
        raise ValueError("Mapwright config must contain a YAML mapping at its root.")

    missing_fields = [
        field for field in REQUIRED_FIELDS if field not in config or config[field] is None
    ]
    if missing_fields:
        fields = ", ".join(missing_fields)
        raise ValueError(f"Missing required Mapwright config field(s): {fields}")

    return config


def validate_paths(config: dict[str, Any]) -> list[str]:
    """Return configured paths that do not exist on the local filesystem.

    ``target_scene`` is resolved relative to ``godot_project_path``. Other
    relative paths are resolved relative to the current working directory.
    The optional Godot executable is checked only when it is configured.
    """
    missing_paths: list[str] = []
    godot_project = Path(str(config["godot_project_path"])).expanduser()

    paths_to_check = [
        godot_project,
        godot_project / str(config["target_scene"]),
        Path(str(config["asset_pack_path"])).expanduser(),
        Path(str(config["capture_output_dir"])).expanduser(),
    ]

    godot_executable = config.get("godot_executable_path")
    if godot_executable:
        paths_to_check.append(Path(str(godot_executable)).expanduser())

    for configured_path in paths_to_check:
        if not configured_path.exists():
            missing_paths.append(str(configured_path))

    return missing_paths
