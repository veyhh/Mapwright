import tempfile
import unittest
from pathlib import Path

from tools.landmark_analyzer import analyze_landmarks
from tools.spacing_analyzer import Vector3


class LandmarkAnalyzerTest(unittest.TestCase):
    def _write_asset(self, root: Path, name: str, dimensions: str) -> None:
        (root / f"{name}.tscn").write_text(
            "\n".join(
                [
                    "[gd_scene format=3]",
                    f'[node name="{name}" type="Node3D"]',
                    f"dimensions = Vector3({dimensions})",
                ]
            ),
            encoding="utf-8",
        )

    def test_similarly_sized_large_objects_compete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project.godot").write_text("[application]\n", encoding="utf-8")
            self._write_asset(root, "small", "1, 1, 1")
            self._write_asset(root, "tower_a", "2, 5, 2")
            self._write_asset(root, "tower_b", "2.1, 4.9, 2")
            scene = root / "zone.tscn"
            scene.write_text(
                """[gd_scene format=3]
[ext_resource type="PackedScene" path="res://small.tscn" id="1"]
[ext_resource type="PackedScene" path="res://tower_a.tscn" id="2"]
[ext_resource type="PackedScene" path="res://tower_b.tscn" id="3"]
[node name="Zone" type="Node3D"]
[node name="CrateA" parent="." instance=ExtResource("1")]
[node name="CrateB" parent="." instance=ExtResource("1")]
[node name="CrateC" parent="." instance=ExtResource("1")]
[node name="TowerA" parent="." instance=ExtResource("2")]
[node name="TowerB" parent="." instance=ExtResource("3")]
""",
                encoding="utf-8",
            )

            report = analyze_landmarks(str(scene))

        self.assertFalse(report.no_clear_hierarchy)
        self.assertTrue(report.competing_landmarks)
        self.assertEqual({obj.name for obj in report.candidates}, {"TowerA", "TowerB"})
        self.assertAlmostEqual(report.median_size_score, 3**0.5)
        self.assertGreater(report.objects[0].relative_score, 3.0)

    def test_equal_objects_have_no_clear_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project.godot").write_text("[application]\n", encoding="utf-8")
            self._write_asset(root, "crate", "1, 1, 1")
            scene = root / "flat_zone.tscn"
            scene.write_text(
                """[gd_scene format=3]
[ext_resource type="PackedScene" path="res://crate.tscn" id="1"]
[node name="Zone" type="Node3D"]
[node name="CrateA" parent="." instance=ExtResource("1")]
[node name="CrateB" parent="." instance=ExtResource("1")]
[node name="CrateC" parent="." instance=ExtResource("1")]
""",
                encoding="utf-8",
            )

            report = analyze_landmarks(str(scene))

        self.assertTrue(report.no_clear_hierarchy)
        self.assertFalse(report.competing_landmarks)

    def test_parent_and_instance_scale_affect_world_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project.godot").write_text("[application]\n", encoding="utf-8")
            self._write_asset(root, "statue", "1, 2, 3")
            scene = root / "scaled_zone.tscn"
            scene.write_text(
                """[gd_scene format=3]
[ext_resource type="PackedScene" path="res://statue.tscn" id="1"]
[node name="Zone" type="Node3D"]
scale = Vector3(2, 2, 2)
[node name="Statue" parent="." instance=ExtResource("1")]
scale = Vector3(3, 1, 0.5)
""",
                encoding="utf-8",
            )

            report = analyze_landmarks(str(scene))

        statue = report.objects[0]
        self.assertEqual(statue.world_scale, Vector3(6, 2, 1))
        self.assertEqual(statue.dimensions, Vector3(6, 4, 3))
        self.assertAlmostEqual(statue.size_score, 61**0.5)


if __name__ == "__main__":
    unittest.main()
