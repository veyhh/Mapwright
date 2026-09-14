"""Adapter detection, Scene IR import, and non-destructive export."""

from __future__ import annotations

from pathlib import Path

import pytest

from mapwright.adapters.base import (
    UnsupportedEngineError,
    default_registry,
)
from mapwright.adapters.generic.exporter import GenericExporter
from mapwright.adapters.godot.exporter import GodotExporter
from mapwright.adapters.godot.importer import GodotImporter
from mapwright.adapters.unity.importer import UnityImporter
from mapwright.adapters.unreal.importer import UnrealImporter
from mapwright.core.config import MapwrightConfig
from mapwright.core.geometry import Vec3
from mapwright.core.scene_ir import SceneIR

REPOSITORY = Path(__file__).resolve().parent.parent
GODOT_SCENE = REPOSITORY / "examples" / "courtyard_level.tscn"
JSON_SCENE = REPOSITORY / "examples" / "outpost_level.json"


@pytest.fixture
def config() -> MapwrightConfig:
    return MapwrightConfig()


@pytest.fixture
def registry():
    return default_registry()


def test_extension_selects_the_adapter(registry):
    assert registry.detect(GODOT_SCENE).engine == "godot"
    assert registry.detect(JSON_SCENE).engine == "generic"


def test_an_unknown_extension_lists_what_is_supported(registry):
    with pytest.raises(UnsupportedEngineError, match="Supported scene files"):
        registry.detect("level.fbx")


def test_an_engine_can_be_forced_over_detection(registry, config):
    scene = registry.load_scene(GODOT_SCENE, config, engine="godot")
    assert scene.source_engine == "godot"


def test_loading_an_unregistered_engine_names_the_available_ones(registry, config):
    with pytest.raises(UnsupportedEngineError, match="Available:"):
        registry.load_scene(JSON_SCENE, config, engine="cryengine")


def test_generic_import_round_trips_through_disk(tmp_path, config, registry):
    scene = registry.load_scene(JSON_SCENE, config)
    written = scene.write_json(tmp_path / "copy.json")
    assert SceneIR.read_json(written).to_dict() == scene.to_dict()


def test_generic_export_reports_which_objects_changed(tmp_path, config, registry):
    scene = registry.load_scene(JSON_SCENE, config)
    original = scene.write_json(tmp_path / "before.json")
    target = scene.objects[1]
    moved = scene.replace_object(target.moved_to(target.position + Vec3(1.0, 0.0, 0.0)))
    result = GenericExporter().export_scene(moved, tmp_path / "after.json", original)
    assert result.written
    assert result.changed_objects == (target.id,)


def test_godot_import_produces_world_space_objects(config):
    scene = GodotImporter().import_scene(GODOT_SCENE, config)
    assert scene.source_engine == "godot"
    assert scene.props
    assert scene.ground_bounds() is not None
    for obj in scene.props:
        assert obj.source_value("node_path") == obj.id


def test_godot_export_rewrites_only_the_moved_object(tmp_path, config):
    scene = GodotImporter().import_scene(GODOT_SCENE, config)
    target = next(obj for obj in scene.props if obj.bounds is not None)
    moved = scene.replace_object(target.moved_to(target.position + Vec3(1.0, 0.0, 0.0)))

    destination = tmp_path / "patched.tscn"
    result = GodotExporter().export_scene(moved, destination, GODOT_SCENE)
    assert result.written
    assert result.changed_objects == (target.id,)

    before = GODOT_SCENE.read_text(encoding="utf-8").splitlines()
    after = destination.read_text(encoding="utf-8").splitlines()
    differing = [
        index
        for index, (first, second) in enumerate(zip(before, after))
        if first != second
    ]
    assert len(differing) == 1, "export must patch one line, not regenerate the scene"
    assert len(before) == len(after)


def test_godot_export_survives_a_reimport(tmp_path, config):
    scene = GodotImporter().import_scene(GODOT_SCENE, config)
    target = next(obj for obj in scene.props if obj.bounds is not None)
    destination = tmp_path / "patched.tscn"
    GodotExporter().export_scene(
        scene.replace_object(target.moved_to(target.position + Vec3(1.0, 0.0, 0.0))),
        destination,
        GODOT_SCENE,
    )

    reimported = GodotImporter().import_scene(destination, config)
    assert {obj.id for obj in reimported.props} == {obj.id for obj in scene.props}
    for obj in reimported.props:
        expected = scene.object_by_id(obj.id).position
        if obj.id == target.id:
            expected = expected + Vec3(1.0, 0.0, 0.0)
        assert obj.position.x == pytest.approx(expected.x, abs=1e-6)
        assert obj.position.y == pytest.approx(expected.y, abs=1e-6)
        assert obj.position.z == pytest.approx(expected.z, abs=1e-6)


def test_godot_export_writes_nothing_when_nothing_moved(tmp_path, config):
    scene = GodotImporter().import_scene(GODOT_SCENE, config)
    destination = tmp_path / "unchanged.tscn"
    result = GodotExporter().export_scene(scene, destination, GODOT_SCENE)
    assert result.changed_objects == ()
    if destination.exists():
        assert destination.read_text(encoding="utf-8") == GODOT_SCENE.read_text(
            encoding="utf-8"
        )


def test_engine_seams_refuse_rather_than_half_work(config):
    for importer in (UnityImporter(), UnrealImporter()):
        with pytest.raises(UnsupportedEngineError, match="Scene IR"):
            importer.import_scene(f"level{importer.extensions[0]}", config)


def test_capture_adapters_report_availability_honestly(registry, config):
    for engine, capturer in registry.capturers.items():
        available, detail = capturer.available(config)
        assert isinstance(available, bool)
        assert detail, f"{engine} must explain its availability"


def test_capture_never_raises_when_the_engine_is_missing(tmp_path, registry, config):
    capturer = registry.capturer_for("blender")
    if capturer.available(config)[0]:
        pytest.skip("Blender is installed; this pins the missing-engine path")
    result = capturer.capture(JSON_SCENE, tmp_path / "views", ("top_down",), config)
    assert result.missing == ("top_down",)
    assert result.notes
