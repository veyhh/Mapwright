import tempfile
import unittest
from pathlib import Path

from tools.navigation_analyzer import analyze_navigation


class NavigationAnalyzerTest(unittest.TestCase):
    def test_full_width_barrier_creates_unreachable_region(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project.godot").write_text("[application]\n", encoding="utf-8")
            (root / "barrier.tscn").write_text(
                """[gd_scene format=3]
[node name="Barrier" type="Node3D"]
dimensions = Vector3(1, 2, 10)
""",
                encoding="utf-8",
            )
            scene = root / "blocked_zone.tscn"
            scene.write_text(
                """[gd_scene format=3]
[ext_resource type="PackedScene" path="res://barrier.tscn" id="1"]
[sub_resource type="BoxMesh" id="GroundMesh"]
size = Vector3(10, 0.2, 10)
[node name="Zone" type="Node3D"]
[node name="Ground" type="MeshInstance3D" parent="."]
mesh = SubResource("GroundMesh")
[node name="CenterBarrier" parent="." instance=ExtResource("1")]
""",
                encoding="utf-8",
            )

            report = analyze_navigation(
                str(scene),
                minimum_path_width=1.0,
                cell_size=0.25,
                minimum_region_area=1.0,
            )

        self.assertTrue(report.disconnected)
        self.assertFalse(report.no_navigable_area)
        self.assertEqual(len(report.significant_regions), 2)
        self.assertLess(report.reachable_ratio, 0.6)


if __name__ == "__main__":
    unittest.main()
