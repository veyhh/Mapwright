import tempfile
import unittest
from pathlib import Path

from tools.config_loader import load_config, validate_paths


class ConfigLoaderTest(unittest.TestCase):
    def test_load_config_and_validate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "godot-project"
            scene = project / "scenes" / "main.tscn"
            asset_pack = root / "asset-pack"
            captures = root / "captures"

            scene.parent.mkdir(parents=True)
            scene.touch()
            asset_pack.mkdir()
            captures.mkdir()

            config_path = root / "mapwright.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f"godot_project_path: '{project.as_posix()}'",
                        "target_scene: 'scenes/main.tscn'",
                        f"asset_pack_path: '{asset_pack.as_posix()}'",
                        f"capture_output_dir: '{captures.as_posix()}'",
                        "capture_angles:",
                        "  - top_down",
                        "  - iso_ne",
                        "  - iso_sw",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(str(config_path))

            self.assertEqual(config["capture_angles"], ["top_down", "iso_ne", "iso_sw"])
            self.assertEqual(validate_paths(config), [])

            captures.rmdir()
            self.assertEqual(validate_paths(config), [str(captures)])

    def test_missing_required_field_has_meaningful_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "mapwright.yaml"
            config_path.write_text("godot_project_path: ./project\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "target_scene"):
                load_config(str(config_path))


if __name__ == "__main__":
    unittest.main()
