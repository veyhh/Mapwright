import unittest
from pathlib import Path

from tools.repetition_detector import analyze_scene, parse_tscn


EXAMPLE_SCENE = Path(__file__).parents[1] / "examples" / "repetitive_level.tscn"


class RepetitionDetectorTest(unittest.TestCase):
    def test_overused_asset_is_flagged_and_percentage_is_correct(self) -> None:
        report = analyze_scene(str(EXAMPLE_SCENE))
        assets = {asset.name: asset for asset in report.assets}

        crate = assets["wooden_crate"]
        self.assertEqual(report.total_references, 32)
        self.assertEqual(crate.count, 8)
        self.assertAlmostEqual(crate.percentage, 25.0)
        self.assertTrue(crate.flagged)

        self.assertEqual(assets["barrel"].count, 1)
        self.assertAlmostEqual(assets["barrel"].percentage, 3.125)
        self.assertFalse(assets["barrel"].flagged)

    def test_parser_ignores_comments_and_string_literals(self) -> None:
        scene_text = """[gd_scene format=3]

[ext_resource type="PackedScene" path="res://crate.tscn" id="crate"]

[node name="Root" type="Node3D"]
metadata/example = "ExtResource(\\"crate\\")"
; ExtResource("crate")
[node name="Crate" parent="." instance=ExtResource("crate")]
"""

        resources, references = parse_tscn(scene_text)

        self.assertEqual(list(resources), ["crate"])
        self.assertEqual(references, ["crate"])


if __name__ == "__main__":
    unittest.main()
