"""Import engine-independent Scene IR documents.

This is the adapter that makes Mapwright usable with no game engine installed:
write the level as JSON and every spatial, flow, pacing, encounter, and
fairness check runs against it.
"""

from __future__ import annotations

from pathlib import Path

from mapwright.adapters.base import SceneImporter
from mapwright.core.config import MapwrightConfig
from mapwright.core.scene_ir import SceneIR


class GenericImporter(SceneImporter):
    """Reads a ``.json`` Scene IR document."""

    engine = "generic"
    extensions = (".json",)

    def import_scene(self, path: str | Path, config: MapwrightConfig) -> SceneIR:
        """Read and validate a Scene IR document."""
        return SceneIR.read_json(path)
