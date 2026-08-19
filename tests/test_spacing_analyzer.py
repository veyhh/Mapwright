import tempfile
import unittest
from pathlib import Path

from tools.spacing_analyzer import analyze_spacing


class SpacingAnalyzerTest(unittest.TestCase):
    def test_too_close_pair_is_flagged_with_global_positions(self) -> None:
        scene_text = """[gd_scene load_steps=2 format=3]

[ext_resource type="PackedScene" path="res://crate.tscn" id="1_crate"]

[node name="Root" type="Node3D"]
position = Vector3(10, 0, 0)

[node name="CrateA" parent="." instance=ExtResource("1_crate")]
position = Vector3(0, 0, 0)

[node name="CrateB" parent="." instance=ExtResource("1_crate")]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0.75, 0, 0)

[node name="Camera" type="Camera3D" parent="."]
position = Vector3(0.2, 0, 0)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            scene_path = Path(temp_dir) / "spacing_test.tscn"
            scene_path.write_text(scene_text, encoding="utf-8")

            report = analyze_spacing(str(scene_path), threshold=1.0)

        self.assertEqual(report.analyzed_count, 2)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual({issue.first.name, issue.second.name}, {"CrateA", "CrateB"})
        self.assertAlmostEqual(issue.distance, 0.75)
        self.assertTrue(issue.too_close)
        positions = {node.name: node.position for node in report.nodes}
        self.assertAlmostEqual(positions["CrateA"].x, 10.0)
        self.assertAlmostEqual(positions["CrateB"].x, 10.75)

    def test_distance_equal_to_threshold_is_not_flagged(self) -> None:
        scene_text = """[gd_scene format=3]

[ext_resource type="PackedScene" path="res://crate.tscn" id="1_crate"]

[node name="Root" type="Node3D"]
[node name="CrateA" parent="." instance=ExtResource("1_crate")]
[node name="CrateB" parent="." instance=ExtResource("1_crate")]
position = Vector3(0.9, 0, 1.2)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            scene_path = Path(temp_dir) / "threshold_test.tscn"
            scene_path.write_text(scene_text, encoding="utf-8")
            report = analyze_spacing(str(scene_path), threshold=1.5)

        self.assertEqual(report.issues, ())


if __name__ == "__main__":
    unittest.main()
