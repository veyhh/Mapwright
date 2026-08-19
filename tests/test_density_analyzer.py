import tempfile
import unittest
from pathlib import Path

from tools.density_analyzer import analyze_density


class DensityAnalyzerTest(unittest.TestCase):
    def test_clustered_props_are_reported_as_imbalanced(self) -> None:
        scene_text = """[gd_scene load_steps=3 format=3]

[ext_resource type="PackedScene" path="res://crate.tscn" id="1_crate"]

[sub_resource type="BoxMesh" id="GroundMesh"]
size = Vector3(12, 0.2, 12)

[node name="Root" type="Node3D"]

[node name="Ground" type="MeshInstance3D" parent="."]
mesh = SubResource("GroundMesh")

[node name="PropA" parent="." instance=ExtResource("1_crate")]
position = Vector3(-5, 0, -5)

[node name="PropB" parent="." instance=ExtResource("1_crate")]
position = Vector3(-4.5, 0, -5)

[node name="PropC" parent="." instance=ExtResource("1_crate")]
position = Vector3(-5, 0, -4.5)

[node name="PropD" parent="." instance=ExtResource("1_crate")]
position = Vector3(-4.5, 0, -4.5)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            scene_path = Path(temp_dir) / "density_test.tscn"
            scene_path.write_text(scene_text, encoding="utf-8")
            report = analyze_density(
                str(scene_path), cell_size=4.0, imbalance_threshold=60.0
            )

        self.assertEqual((report.columns, report.rows), (3, 3))
        self.assertEqual(report.prop_count, 4)
        self.assertEqual(max(cell.count for cell in report.cells), 4)
        self.assertEqual(len(report.empty_cells), 8)
        self.assertGreater(report.standard_deviation, 0)
        self.assertGreater(report.imbalance_score, 60.0)
        self.assertTrue(report.imbalanced)


if __name__ == "__main__":
    unittest.main()
