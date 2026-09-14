"""Unity adapter seam — declared, not implemented.

Mapwright's analyzers are already engine-independent, so Unity support is
purely an import/export problem. It is left unimplemented rather than
half-implemented: a ``.unity`` scene is a YAML graph of GUID-referenced
components whose prefab bounds live in separate ``.meta`` and asset files, and
guessing at those bounds would feed every validator numbers that look
authoritative and are not.

To implement, satisfy this contract:

1. Parse the scene YAML (multi-document, ``!u!<classID> &<fileID>`` headers).
2. Resolve each GameObject's world transform by composing ``m_LocalPosition``,
   ``m_LocalRotation`` (a quaternion, so convert to Y-X-Z Euler) and
   ``m_LocalScale`` up the ``m_Father`` chain.
3. Resolve extents from MeshFilter/Renderer bounds, or from the referenced
   prefab, via the GUID map in the project's ``Library`` or ``.meta`` files.
4. Emit ``SceneObject`` entries with ``bounds`` set only where extents were
   genuinely resolved, and ``bounds=None`` elsewhere.
5. Unity is Y-up and left-handed; negate Z on positions and rotations to reach
   Mapwright's right-handed Y-up space.
"""

from __future__ import annotations

from pathlib import Path

from mapwright.adapters.base import SceneImporter, UnsupportedEngineError
from mapwright.core.config import MapwrightConfig
from mapwright.core.scene_ir import SceneIR


class UnityImporter(SceneImporter):
    """Placeholder importer for Unity ``.unity`` scenes."""

    engine = "unity"
    extensions = (".unity",)

    def import_scene(self, path: str | Path, config: MapwrightConfig) -> SceneIR:
        """Raise, explaining that Unity import is on the roadmap, not shipped."""
        raise UnsupportedEngineError(
            "Mapwright v0.2 does not import Unity scenes. Export the level as a "
            "Scene IR JSON document and analyze it with the generic adapter, or "
            "see mapwright/adapters/unity/importer.py for the contract an "
            "implementation must satisfy."
        )
