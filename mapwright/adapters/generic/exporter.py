"""Write Scene IR documents back to disk."""

from __future__ import annotations

from pathlib import Path

from mapwright.adapters.base import ExportResult, SceneExporter
from mapwright.core.scene_ir import SceneIR


class GenericExporter(SceneExporter):
    """Writes a scene as a Scene IR JSON document."""

    engine = "generic"

    def export_scene(
        self,
        scene: SceneIR,
        destination: str | Path,
        original: str | Path | None = None,
    ) -> ExportResult:
        """Write the whole scene, reporting which objects differ from the original."""
        changed: tuple[str, ...] = ()
        if original is not None and Path(original).is_file():
            previous = {obj.id: obj for obj in SceneIR.read_json(original).objects}
            changed = tuple(
                obj.id
                for obj in scene.objects
                if obj.id not in previous or previous[obj.id] != obj
            )
        written = scene.write_json(destination)
        return ExportResult(path=written, changed_objects=changed, written=True)
