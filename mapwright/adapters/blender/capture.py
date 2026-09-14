"""Render Scene IR views through Blender, with no game engine involved.

Blender can stand in as a neutral renderer: Mapwright writes the scene as
boxes and drives a headless render of the standard views. That makes visual
review possible for a level authored as plain JSON.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from mapwright.adapters.base import CaptureAdapter, CaptureResult
from mapwright.core.config import MapwrightConfig
from mapwright.core.scene_ir import SceneIR


DEFAULT_TIMEOUT_SECONDS = 300

_RENDER_SCRIPT = '''
import json, math, sys
import bpy

payload = json.loads(sys.argv[sys.argv.index("--") + 1])
scene_data = json.load(open(payload["scene"], encoding="utf-8"))
output_dir = payload["output"]
views = payload["views"]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = payload["width"]
scene.render.resolution_y = payload["height"]
scene.render.film_transparent = False

minimum = [float("inf")] * 3
maximum = [float("-inf")] * 3

for item in scene_data.get("objects", []):
    bounds = item.get("bounds")
    if not bounds:
        continue
    position = item.get("position", [0.0, 0.0, 0.0])
    scale = item.get("scale", [1.0, 1.0, 1.0])
    rotation = item.get("rotation", [0.0, 0.0, 0.0])
    low, high = bounds["min"], bounds["max"]
    size = [(high[i] - low[i]) * abs(scale[i]) for i in range(3)]
    center = [position[i] + (low[i] + high[i]) * 0.5 * scale[i] for i in range(3)]
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    cube = bpy.context.active_object
    cube.name = item.get("name", item.get("id", "object"))
    # Scene IR is Y-up; Blender is Z-up, so Y and Z swap on the way in.
    cube.location = (center[0], -center[2], center[1])
    cube.scale = (max(size[0], 1e-4), max(size[2], 1e-4), max(size[1], 1e-4))
    cube.rotation_euler = (rotation[0], -rotation[2], rotation[1])
    for axis, value in enumerate((center[0], -center[2], center[1])):
        half = (size[0], size[2], size[1])[axis] * 0.5
        minimum[axis] = min(minimum[axis], value - half)
        maximum[axis] = max(maximum[axis], value + half)

if minimum[0] == float("inf"):
    minimum, maximum = [-1.0] * 3, [1.0] * 3
center = [(minimum[i] + maximum[i]) * 0.5 for i in range(3)]
span = max(max(maximum[i] - minimum[i] for i in range(3)), 1.0)

light_data = bpy.data.lights.new(name="key", type="SUN")
light_data.energy = 3.0
light = bpy.data.objects.new(name="key", object_data=light_data)
light.rotation_euler = (math.radians(50), 0.0, math.radians(35))
scene.collection.objects.link(light)

camera_data = bpy.data.cameras.new("camera")
camera = bpy.data.objects.new("camera", camera_data)
scene.collection.objects.link(camera)
scene.camera = camera

angles = {
    "top_down": (0.0, 0.0, span * 1.6),
    "iso_ne": (span * 0.9, -span * 0.9, span * 0.9),
    "iso_sw": (-span * 0.9, span * 0.9, span * 0.9),
}
for view in views:
    offset = angles.get(view)
    if offset is None:
        continue
    camera.location = (center[0] + offset[0], center[1] + offset[1], center[2] + offset[2])
    direction = [center[i] - camera.location[i] for i in range(3)]
    horizontal = math.hypot(direction[0], direction[1])
    camera.rotation_euler = (
        math.atan2(horizontal, -direction[2]),
        0.0,
        math.atan2(-direction[0], direction[1]),
    )
    scene.render.filepath = f"{output_dir}/{view}.png"
    bpy.ops.render.render(write_still=True)
'''


class BlenderCaptureAdapter(CaptureAdapter):
    """Renders the standard Mapwright views with a headless Blender."""

    engine = "blender"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def resolve_executable(self, config: MapwrightConfig) -> str | None:
        """Return the Blender executable to use, if one can be found."""
        candidate = self._executable or config.engine.executable_path
        if candidate:
            return candidate if Path(candidate).exists() else None
        return shutil.which("blender")

    def available(self, config: MapwrightConfig) -> tuple[bool, str]:
        """Return whether a Blender executable is reachable."""
        executable = self.resolve_executable(config)
        if executable is None:
            return False, "Blender executable not found on PATH or in config.engine."
        return True, f"Blender capture available via {executable}"

    def capture(
        self,
        scene_path: str | Path,
        output_dir: str | Path,
        views: Sequence[str],
        config: MapwrightConfig,
    ) -> CaptureResult:
        """Render each requested view of a Scene IR document to a PNG."""
        destination = Path(output_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        executable = self.resolve_executable(config)
        if executable is None:
            return CaptureResult(
                output_dir=destination,
                missing=tuple(views),
                notes=(self.available(config)[1],),
            )

        source = Path(scene_path).expanduser()
        if source.suffix.lower() != ".json":
            return CaptureResult(
                output_dir=destination,
                missing=tuple(views),
                notes=(
                    "Blender capture renders Scene IR documents; export the scene "
                    f"to JSON first (received '{source.suffix or source.name}').",
                ),
            )

        with tempfile.TemporaryDirectory() as workspace:
            script_path = Path(workspace) / "mapwright_render.py"
            script_path.write_text(_RENDER_SCRIPT, encoding="utf-8")
            payload = json.dumps(
                {
                    "scene": str(source),
                    "output": str(destination),
                    "views": list(views),
                    "width": 1280,
                    "height": 1024,
                }
            )
            command = [
                executable,
                "--background",
                "--factory-startup",
                "--python",
                str(script_path),
                "--",
                payload,
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
                return CaptureResult(
                    output_dir=destination,
                    missing=tuple(views),
                    notes=(f"Blender capture failed to run: {error}",),
                )

        notes: list[str] = []
        if completed.returncode != 0:
            notes.append(
                f"Blender exited with status {completed.returncode}: "
                f"{completed.stderr.strip()[:500]}"
            )
        images = tuple(
            path for view in views if (path := destination / f"{view}.png").is_file()
        )
        missing = tuple(
            view for view in views if not (destination / f"{view}.png").is_file()
        )
        return CaptureResult(
            output_dir=destination,
            images=images,
            missing=missing,
            notes=tuple(notes),
        )
