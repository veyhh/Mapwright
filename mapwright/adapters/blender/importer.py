"""Import a Blender blockout into Scene IR.

Blender is where a lot of level geometry starts. Reading a ``.blend`` needs
Blender itself, so this adapter drives a headless instance that dumps world
bounds as JSON, then converts Z-up Blender space into Mapwright's Y-up space.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from mapwright.adapters.base import AdapterError, SceneImporter
from mapwright.core.config import MapwrightConfig
from mapwright.core.geometry import Bounds, Vec3
from mapwright.core.scene_ir import ObjectType, SceneIR, SceneObject


DEFAULT_TIMEOUT_SECONDS = 180

_GROUND_TOKENS = ("ground", "floor", "terrain")
_STRUCTURE_TOKENS = ("wall", "building", "structure", "arch", "ruin", "platform")

_DUMP_SCRIPT = '''
import json, sys
import bpy

destination = sys.argv[sys.argv.index("--") + 1]
records = []
for obj in bpy.data.objects:
    if obj.type != "MESH":
        continue
    corners = [obj.matrix_world @ corner.to_4d().to_3d() for corner in
               [__import__("mathutils").Vector(point) for point in obj.bound_box]]
    xs = [point.x for point in corners]
    ys = [point.y for point in corners]
    zs = [point.z for point in corners]
    records.append({
        "name": obj.name,
        "min": [min(xs), min(ys), min(zs)],
        "max": [max(xs), max(ys), max(zs)],
    })
json.dump({"objects": records}, open(destination, "w", encoding="utf-8"))
'''


class BlenderImporter(SceneImporter):
    """Reads a ``.blend`` file by asking Blender for world bounds.

    Orientation is baked into the reported world bounds rather than carried as
    a rotation: every analyzer works from the world AABB anyway, so this is
    exact for spatial analysis and avoids guessing Blender's rotation modes.
    """

    engine = "blender"
    extensions = (".blend",)

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def resolve_executable(self, config: MapwrightConfig) -> str | None:
        """Return the Blender executable to use, if one can be found."""
        candidate = self._executable or config.engine.executable_path
        if candidate:
            return candidate if Path(candidate).exists() else None
        return shutil.which("blender")

    def import_scene(self, path: str | Path, config: MapwrightConfig) -> SceneIR:
        """Read a Blender file into Scene IR via a headless bounds dump."""
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Blender scene not found: {source}")
        executable = self.resolve_executable(config)
        if executable is None:
            raise AdapterError(
                "Importing a .blend needs a Blender executable; none was found on "
                "PATH or in config.engine.executable_path."
            )

        with tempfile.TemporaryDirectory() as workspace:
            script_path = Path(workspace) / "mapwright_dump.py"
            dump_path = Path(workspace) / "scene.json"
            script_path.write_text(_DUMP_SCRIPT, encoding="utf-8")
            command = [
                executable,
                "--background",
                str(source),
                "--factory-startup",
                "--python",
                str(script_path),
                "--",
                str(dump_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AdapterError(f"Blender import failed to run: {error}") from error
            if not dump_path.is_file():
                raise AdapterError(
                    f"Blender produced no scene dump (status {completed.returncode}): "
                    f"{completed.stderr.strip()[:500]}"
                )
            payload = json.loads(dump_path.read_text(encoding="utf-8"))

        objects = tuple(
            _to_scene_object(record, index)
            for index, record in enumerate(payload.get("objects", []))
        )
        return SceneIR(
            scene=source.stem,
            objects=objects,
            source_engine=self.engine,
            source_path=str(source),
        )


def _to_scene_object(record: dict, index: int) -> SceneObject:
    """Convert one Z-up world-bounds record into a Y-up Scene IR object."""
    name = str(record.get("name", f"object_{index}"))
    low, high = record["min"], record["max"]
    # Blender Z-up (x, y, z) maps to Mapwright Y-up (x, z, -y).
    corners = [
        Vec3(low[0], low[2], -high[1]),
        Vec3(high[0], high[2], -low[1]),
    ]
    minimum = Vec3(
        min(corners[0].x, corners[1].x),
        min(corners[0].y, corners[1].y),
        min(corners[0].z, corners[1].z),
    )
    maximum = Vec3(
        max(corners[0].x, corners[1].x),
        max(corners[0].y, corners[1].y),
        max(corners[0].z, corners[1].z),
    )
    position = Vec3((minimum.x + maximum.x) * 0.5, minimum.y, (minimum.z + maximum.z) * 0.5)
    lowered = name.lower()
    if any(token in lowered for token in _GROUND_TOKENS):
        object_type = ObjectType.GROUND
    elif any(token in lowered for token in _STRUCTURE_TOKENS):
        object_type = ObjectType.STRUCTURE
    else:
        object_type = ObjectType.PROP
    return SceneObject(
        id=name,
        name=name,
        type=object_type,
        position=position,
        bounds=Bounds(minimum - position, maximum - position),
        tags=("blender:mesh",),
        source=(("engine", "blender"), ("object_name", name)),
    )
