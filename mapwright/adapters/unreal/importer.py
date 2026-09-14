"""Unreal adapter seam — declared, not implemented.

Unreal levels ship as binary ``.umap`` assets. There is no supported way to
read actor transforms and bounds from them without the engine or a Python
plugin running inside it, so Mapwright does not pretend to. The analyzers are
engine-independent already; only this import step is missing.

To implement, satisfy this contract:

1. Run inside the editor (``unreal`` Python API) or drive a commandlet, and
   walk ``EditorLevelLibrary.get_all_level_actors()``.
2. For each actor, read ``get_actor_location``, ``get_actor_rotation`` and the
   component bounds from ``get_actor_bounds(only_colliding_components=False)``.
3. Convert units: Unreal works in centimetres, Mapwright in metres, so divide
   by 100.
4. Convert space: Unreal is Z-up and left-handed. Map ``(x, y, z)`` to
   ``(x, z, y)`` after the unit conversion, and convert the rotator's yaw to a
   rotation about Mapwright's +Y.
5. Emit Scene IR, setting ``bounds=None`` for any actor whose extents the
   editor could not report.
"""

from __future__ import annotations

from pathlib import Path

from mapwright.adapters.base import SceneImporter, UnsupportedEngineError
from mapwright.core.config import MapwrightConfig
from mapwright.core.scene_ir import SceneIR


class UnrealImporter(SceneImporter):
    """Placeholder importer for Unreal ``.umap`` levels."""

    engine = "unreal"
    extensions = (".umap",)

    def import_scene(self, path: str | Path, config: MapwrightConfig) -> SceneIR:
        """Raise, explaining that Unreal import is on the roadmap, not shipped."""
        raise UnsupportedEngineError(
            "Mapwright v0.2 does not import Unreal levels. Export the level as a "
            "Scene IR JSON document and analyze it with the generic adapter, or "
            "see mapwright/adapters/unreal/importer.py for the contract an "
            "implementation must satisfy."
        )
