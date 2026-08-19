import tempfile
import unittest
from pathlib import Path

from tools.sightline_analyzer import SightPoint, analyze_sightlines
from tools.spacing_analyzer import Vector3


class SightlineAnalyzerTest(unittest.TestCase):
    def test_landmark_blocked_from_every_viewpoint_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project.godot").write_text("[application]\n", encoding="utf-8")
            (root / "landmark.tscn").write_text(
                """[gd_scene format=3]
[node name="Landmark" type="Node3D"]
dimensions = Vector3(2, 6, 2)
""",
                encoding="utf-8",
            )
            (root / "wall.tscn").write_text(
                """[gd_scene format=3]
[node name="Wall" type="Node3D"]
dimensions = Vector3(3, 4, 1)
""",
                encoding="utf-8",
            )
            scene = root / "blocked_sightline.tscn"
            scene.write_text(
                """[gd_scene format=3]
[ext_resource type="PackedScene" path="res://landmark.tscn" id="1"]
[ext_resource type="PackedScene" path="res://wall.tscn" id="2"]
[sub_resource type="BoxMesh" id="GroundMesh"]
size = Vector3(10, 0.2, 10)
[node name="Zone" type="Node3D"]
[node name="Ground" type="MeshInstance3D" parent="."]
mesh = SubResource("GroundMesh")
[node name="Tower" parent="." instance=ExtResource("1")]
position = Vector3(0, 0, 3)
[node name="BlockingWall" parent="." instance=ExtResource("2")]
position = Vector3(0, 0, 0)
""",
                encoding="utf-8",
            )
            viewpoint = SightPoint("spawn", Vector3(0, 1.6, -3), "test")

            report = analyze_sightlines(str(scene), viewpoints=(viewpoint,))
            high_viewpoint = SightPoint("high", Vector3(0, 10, -3), "test")
            high_report = analyze_sightlines(
                str(scene), viewpoints=(high_viewpoint,)
            )

        self.assertEqual([item.landmark.name for item in report.visibility], ["Tower"])
        self.assertEqual(len(report.flagged_landmarks), 1)
        ray = report.sightlines[0]
        self.assertFalse(ray.clear)
        self.assertEqual(ray.blockers, ("BlockingWall",))
        self.assertTrue(high_report.sightlines[0].clear)
        self.assertEqual(high_report.flagged_landmarks, ())


if __name__ == "__main__":
    unittest.main()
