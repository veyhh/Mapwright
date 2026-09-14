"""Scene IR construction, validation, and serialization."""

from __future__ import annotations

import math

import pytest

from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.scene_ir import (
    MarkerKind,
    MarkerPoint,
    ObjectType,
    PacingLevel,
    SceneIR,
    SceneIRError,
    SceneObject,
    Zone,
    ZoneType,
)


def make_object(identifier: str = "crate", **overrides) -> SceneObject:
    defaults = {
        "id": identifier,
        "name": identifier.title(),
        "type": ObjectType.PROP,
        "position": Vec3(1.0, 0.0, 2.0),
        "bounds": Bounds.from_size(Vec3(1.0, 2.0, 1.0)),
    }
    defaults.update(overrides)
    return SceneObject(**defaults)


def test_round_trips_through_json_unchanged():
    scene = SceneIR(
        scene="courtyard",
        objects=(
            make_object("well", asset="props/well", tags=("landmark_candidate",)),
            make_object("ground", type=ObjectType.GROUND, bounds=Bounds(Vec3(-5, -1, -5), Vec3(5, 0, 5))),
        ),
        zones=(
            Zone("plaza", ZoneType.EXPLORATION, PacingLevel.MEDIUM, Bounds(Vec3(-5, 0, -5), Vec3(5, 3, 5))),
        ),
        entry_points=(MarkerPoint("gate", MarkerKind.ENTRY, Vec3(-4, 0, 0), zone="plaza"),),
    )
    restored = SceneIR.from_json(scene.to_json())
    assert restored.to_dict() == scene.to_dict()
    assert restored.objects[0].asset == "props/well"
    assert restored.entry_points[0].kind is MarkerKind.ENTRY


def test_rejects_duplicate_object_ids():
    with pytest.raises(SceneIRError, match="Duplicate object id"):
        SceneIR(scene="s", objects=(make_object("a"), make_object("a")))


def test_rejects_object_referencing_unknown_zone():
    with pytest.raises(SceneIRError, match="undefined zone"):
        SceneIR(scene="s", objects=(make_object("a", zone="nowhere"),))


def test_rejects_unknown_object_type_with_a_listing():
    with pytest.raises(SceneIRError, match="Supported:"):
        SceneIR.from_dict({"scene": "s", "objects": [{"id": "a", "type": "sasquatch"}]})


def test_rejects_non_finite_coordinates():
    with pytest.raises(ValueError, match="Non-finite"):
        SceneIR.from_dict(
            {"scene": "s", "objects": [{"id": "a", "position": [0, float("inf"), 0]}]}
        )


def test_world_bounds_apply_rotation_and_scale():
    obj = make_object(
        "pillar",
        position=Vec3(10.0, 0.0, 0.0),
        rotation=Vec3(0.0, math.pi / 2, 0.0),
        bounds=Bounds.from_size(Vec3(4.0, 2.0, 1.0)),
    )
    box = obj.world_bounds()
    assert box.width == pytest.approx(1.0)
    assert box.depth == pytest.approx(4.0)
    assert box.height == pytest.approx(2.0)
    assert box.center.x == pytest.approx(10.0)


def test_ground_bounds_prefer_declared_surface_over_props():
    scene = SceneIR(
        scene="s",
        objects=(
            make_object("prop", position=Vec3(0, 0, 0)),
            make_object(
                "floor",
                type=ObjectType.GROUND,
                position=Vec3(0, 0, 0),
                bounds=Bounds(Vec3(-20, -1, -12), Vec3(20, 0, 12)),
            ),
        ),
    )
    ground = scene.ground_bounds()
    assert ground.width == pytest.approx(40.0)
    assert ground.depth == pytest.approx(24.0)


def test_unmeasured_objects_are_kept_but_excluded_from_obstacles():
    scene = SceneIR(scene="s", objects=(make_object("mystery", bounds=None),))
    assert len(scene.objects) == 1
    assert scene.obstacles == ()
    assert scene.props[0].measured is False


def test_z_up_documents_convert_to_canonical_y_up():
    scene = SceneIR.from_dict(
        {
            "scene": "s",
            "coordinate_system": "Z_UP",
            "objects": [
                {
                    "id": "a",
                    "position": [1.0, 5.0, 3.0],
                    "rotation": [0.0, 0.0, 1.5],
                    "bounds": {"min": [-1, -1, 0], "max": [1, 1, 2]},
                }
            ],
        }
    )
    obj = scene.objects[0]
    assert (obj.position.x, obj.position.y, obj.position.z) == (1.0, 3.0, -5.0)
    assert obj.rotation.y == pytest.approx(1.5)


def test_z_up_rejects_rotations_it_cannot_remap():
    with pytest.raises(SceneIRError, match="yaw-only"):
        SceneIR.from_dict(
            {
                "scene": "s",
                "coordinate_system": "Z_UP",
                "objects": [{"id": "a", "rotation": [0.4, 0.0, 0.0]}],
            }
        )


def test_replace_and_remove_reject_unknown_objects():
    scene = SceneIR(scene="s", objects=(make_object("a"),))
    with pytest.raises(SceneIRError, match="unknown object"):
        scene.remove_object("b")
    with pytest.raises(SceneIRError, match="unknown object"):
        scene.replace_object(make_object("b"))


def test_moved_to_leaves_the_original_untouched():
    scene = SceneIR(scene="s", objects=(make_object("a", position=Vec3(0, 0, 0)),))
    moved = scene.replace_object(scene.objects[0].moved_to(Vec3(5, 0, 5)))
    assert scene.objects[0].position == Vec3(0, 0, 0)
    assert moved.objects[0].position == Vec3(5, 0, 5)


def test_teams_are_reported_in_deterministic_order():
    scene = SceneIR(
        scene="s",
        spawn_points=(
            MarkerPoint("s2", MarkerKind.SPAWN, Vec3(1, 0, 0), team="B"),
            MarkerPoint("s1", MarkerKind.SPAWN, Vec3(-1, 0, 0), team="A"),
        ),
    )
    assert scene.teams == ("A", "B")
